import os
import re
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()

app = Flask(__name__)

# ── Environment variables ────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOWED_GROUP_ID = int(os.environ.get("ALLOWED_GROUP_ID", "0"))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── In-memory state: tracks repo selection and clarification per user ─
# { user_id: { "repo": "owner/repo", "text": "...", "awaiting_clarification": bool, ... } }
pending = {}

CLAUDE_MENTION = "@claude"

FILE_INDICATORS = [
    ".tsx", ".ts", ".js", ".jsx", ".py", ".css", ".html", ".json", ".yml", ".yaml",
    "src/", "app/", "components/", "pages/", "lib/", "utils/", "api/", "hooks/",
]

CREATE_KEYWORDS = [
    "create", "add a new", "new page", "new file", "new component", "new route",
    "build a", "make a", "generate a", "scaffold",
]

MODIFY_KEYWORDS = [
    "change", "update", "fix", "rename", "highlight", "remove", "delete", "edit",
    "modify", "refactor", "implement", "style", "replace", "move", "add",
]

# Keywords that indicate Claude has finished its task (from Claude Code Action comment format)
COMPLETION_KEYWORDS = ["claude finished", "task complete", "task completed", "completed the task"]


# ── Helpers ──────────────────────────────────────────────────────────
def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = requests.post(url, json=payload, timeout=10)
    # If Telegram rejects the markdown (e.g. stray _ in file paths), retry as plain text
    if not resp.ok and parse_mode:
        payload.pop("parse_mode")
        resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def extract_chat_id_from_issue_body(body):
    """Pull the Telegram chat_id embedded in the issue body we wrote."""
    # Matches: **Group:** Some Name (`-1234567890`)
    match = re.search(r"\*\*Group:\*\*[^\n]+\(`(-?\d+)`\)", body)
    if match:
        return int(match.group(1))
    return None


def extract_first_name_from_issue_body(body):
    """Pull the requester first name embedded in the issue body."""
    match = re.search(r"\*\*Requested by:\*\*\s+([^(\n]+)", body)
    if match:
        return match.group(1).strip()
    return "there"


def get_github_repos():
    """Fetch randomwalk-ai org repos that have Claude Code GitHub Action installed."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    search_url = "https://api.github.com/search/code"
    params = {
        "q": "anthropics/claude-code-action org:randomwalk-ai path:.github/workflows",
        "per_page": 100,
    }
    resp = requests.get(search_url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    seen = set()
    repos = []
    for item in items:
        full_name = item["repository"]["full_name"]
        if full_name not in seen:
            seen.add(full_name)
            repos.append(full_name)
    return repos


def create_github_issue(title, body, repo):
    """Create a GitHub issue labelled 'telegram' in the specified repo."""
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body, "labels": ["telegram"]}
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


def build_repo_keyboard(repos):
    """Build a Telegram inline keyboard from a list of repo names."""
    buttons = [[{"text": repo, "callback_data": f"repo:{repo}"}] for repo in repos]
    return {"inline_keyboard": buttons}


def is_code_task(content):
    """Returns True if the message is a code-related task (create or modify)."""
    has_file = any(ind in content for ind in FILE_INDICATORS)
    has_create = any(kw in content for kw in CREATE_KEYWORDS)
    has_modify = any(kw in content for kw in MODIFY_KEYWORDS)
    return has_file or has_create or has_modify


def is_create_request(content):
    """Returns True if the request is about creating a new file, page, or component."""
    return any(kw in content for kw in CREATE_KEYWORDS)


def answer_with_claude(question):
    """Call Anthropic API to answer a general question. Returns answer string."""
    if not ANTHROPIC_API_KEY:
        return None
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": question}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _convert_body_line(line):
    """Convert a single GitHub markdown line to Telegram-friendly text."""
    stripped = line.strip()
    if re.match(r"^[-*_]{3,}$", stripped):
        return ""
    m = re.match(r"^#{1,3}\s+(.+)", line)
    if m:
        return f"\n*{m.group(1).strip()}*"
    m = re.match(r"^\s*-\s*\[x\]\s*(.+)", line, re.IGNORECASE)
    if m:
        return f"✔ {_RE_BOLD.sub(r'\1', m.group(1)).strip()}"
    m = re.match(r"^\s*-\s*\[\s*\]\s*(.+)", line)
    if m:
        return f"◻ {_RE_BOLD.sub(r'\1', m.group(1)).strip()}"
    return _RE_BOLD.sub(r"*\1*", line)


def _build_link_row(first_line, comment_url):
    """Extract PR / job / branch links from Claude's first comment line."""
    pr = re.search(r"\[Create PR[^\]]*\]\(([^)]+)\)", first_line)
    job = re.search(r"\[View job\]\(([^)]+)\)", first_line)
    branch = re.search(r"\[`([^`]+)`\]\(([^)]+)\)", first_line)
    parts = []
    if pr:
        parts.append(f"[🔀 Create PR]({pr.group(1)})")
    if job:
        parts.append(f"[📋 View job]({job.group(1)})")
    if branch:
        parts.append(f"[🌿 Branch]({branch.group(2)})")
    return " · ".join(parts) if parts else f"[View on GitHub]({comment_url})"


def format_claude_comment(body, first_name, issue_title, comment_url):
    """Convert Claude's GitHub comment into a clean Telegram message — no content dropped."""
    raw_lines = body.split("\n")
    first_line = raw_lines[0] if raw_lines else ""

    time_match = re.search(r"in (\d+m\s*\d+s|\d+s|\d+m)", first_line)
    time_str = f" in {time_match.group(1)}" if time_match else ""

    display_title = re.sub(r"^Telegram from @\w+:\s*", "", issue_title).strip()

    out = [f"⚡ *Claude finished{time_str}, {first_name}!*"]
    if display_title:
        out.append(f"📌 {display_title}")
    out.append("")

    for line in raw_lines[1:]:
        out.append(_convert_body_line(line))

    while out and out[-1].strip() == "":
        out.pop()

    out.extend(["", _build_link_row(first_line, comment_url)])

    full = "\n".join(out)
    if len(full) > 3800:
        full = full[:3800] + f"\n\n...[Full response]({comment_url})"
    return full


def get_missing_details(text):
    """Return clarifying questions for a vague @claude request. Empty list = specific enough."""
    content = text.replace(CLAUDE_MENTION, "").strip().lower()
    missing = []

    if is_create_request(content):
        has_route = bool(re.search(r"/\w+", content))
        words = content.split()
        if not has_route and len(words) < 6:
            missing.append("📁 *Where should it go?* Provide the route or folder (e.g. `/users` or `app/users/page.tsx`)")
        if len(words) < 5:
            missing.append("🎯 *What should it contain?* Describe the content or functionality of the new page/file")
    else:
        has_file = any(ind in content for ind in FILE_INDICATORS) or bool(re.search(r"/\w+[\w/]*\.\w+", content))
        if not has_file:
            missing.append("📁 *Which file?* Please provide the full path (e.g. `app/page.tsx`)")

        has_line = bool(re.search(r"line\s*\d+|#l\d+|:\d+", content))
        has_quote = '"' in content or "'" in content or "`" in content
        if not has_line and not has_quote:
            missing.append("📍 *Which exact location?* Give the line number or quote a few words from the text to change")

        if len(content.split()) < 5:
            missing.append("🎯 *What should the result look like?* Describe the expected change in more detail")

    return missing


# ── Routes ───────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "running", "bot": "telegram-to-github-issues"}), 200


@app.route("/api/github-webhook", methods=["POST"])
def github_webhook():
    """Receive org-level GitHub webhook events and forward Claude's replies to Telegram."""
    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(force=True) or {}

    # Only care about issue comment events
    if event != "issue_comment":
        return jsonify({"ok": True}), 200

    action = payload.get("action", "")
    if action not in ("created", "edited"):
        return jsonify({"ok": True}), 200

    # Only forward comments made by the Claude bot
    commenter_login = payload.get("comment", {}).get("user", {}).get("login", "").lower()
    if "claude" not in commenter_login:
        return jsonify({"ok": True}), 200

    comment_body = payload.get("comment", {}).get("body", "")

    # Only fire when Claude signals it has finished (not mid-run progress updates)
    if not any(kw in comment_body.lower() for kw in COMPLETION_KEYWORDS):
        return jsonify({"ok": True}), 200

    # Extract the Telegram chat_id from the issue body we originally wrote — no DB needed
    issue_body = payload.get("issue", {}).get("body", "")
    chat_id = extract_chat_id_from_issue_body(issue_body)
    if not chat_id:
        return jsonify({"ok": True}), 200

    first_name = extract_first_name_from_issue_body(issue_body)
    comment_url = payload.get("comment", {}).get("html_url", "")
    issue_title = payload.get("issue", {}).get("title", "your task")

    formatted = format_claude_comment(comment_body, first_name, issue_title, comment_url)
    try:
        send_telegram_message(chat_id, formatted)
    except Exception:
        pass

    return jsonify({"ok": True}), 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    # ── Handle inline keyboard button press (repo selection) ──────────
    callback = update.get("callback_query")
    if callback:
        return handle_callback(callback)

    # ── Handle regular text messages ──────────────────────────────────
    message = update.get("message")
    if not message or "text" not in message:
        return jsonify({"ok": True, "info": "skipped"}), 200

    chat_id = message["chat"]["id"]
    group_name = message["chat"].get("title", "Unknown Group")
    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username", "unknown")
    first_name = user.get("first_name", "")
    text = message["text"]

    # ── Layer 1: Only allow messages from the authorized group ─────────
    if chat_id != ALLOWED_GROUP_ID:
        return jsonify({"ok": True, "info": "unauthorized – ignored"}), 200

    # ── Layer 2: Handle /end command — cancel pending conversation ─────
    stripped = text.strip()
    command = stripped[len(CLAUDE_MENTION):].strip().lower() if stripped.startswith(CLAUDE_MENTION) else ""
    if command in ("/end", "/cancel"):
        if pending.pop(user_id, None):
            send_telegram_message(chat_id, f"✅ Conversation cancelled, *{first_name}*. Start fresh anytime with `{CLAUDE_MENTION} <your request>`.")
        else:
            send_telegram_message(chat_id, f"Nothing to cancel, *{first_name}*.")
        return jsonify({"ok": True}), 200

    # ── Layer 3: Check if user is answering a clarification question ───
    user_state = pending.get(user_id)
    if user_state and user_state.get("awaiting_clarification"):
        reply = text[len(CLAUDE_MENTION):].strip() if text.startswith(CLAUDE_MENTION) else text
        return handle_clarification_reply(user_id, user_state, reply, chat_id)

    # ── Layer 4: Only process messages starting with @claude ──────────
    if not text.startswith(CLAUDE_MENTION):
        send_telegram_message(
            chat_id,
            f"⚠️ Please start your message with {CLAUDE_MENTION} to create an issue.\n\nExample:\n`{CLAUDE_MENTION} add a footer to the homepage`",
        )
        return jsonify({"ok": True}), 200

    content = text[len(CLAUDE_MENTION):].strip()

    # ── Layer 5: Handle bare @claude with no message ───────────────────
    if not content:
        send_telegram_message(
            chat_id,
            f"👋 Hi *{first_name}*! You mentioned me but didn't include a message.\n\n"
            f"I'm set up to create GitHub issues for code tasks.\n"
            f"Try something like:\n`{CLAUDE_MENTION} fix the button in app/page.tsx line 42`"
        )
        return jsonify({"ok": True}), 200

    # ── Layer 6: Detect general question — answer directly, no issue ──
    if not is_code_task(content.lower()):
        answer = None
        try:
            answer = answer_with_claude(content)
        except Exception:
            pass

        if answer:
            send_telegram_message(chat_id, f"💬 *Claude says:*\n\n{answer}")
        else:
            send_telegram_message(
                chat_id,
                f"🤖 That looks like a general question, *{first_name}*.\n\n"
                f"I'm set up to create GitHub issues for code tasks. "
                f"Try something like:\n`{CLAUDE_MENTION} fix the button in app/page.tsx line 42`"
            )
        return jsonify({"ok": True}), 200

    # ── Layer 7: Code task — fetch repos and show selection keyboard ───
    try:
        repos = get_github_repos()
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to fetch repos: {exc}")
        return jsonify({"ok": False}), 500

    if not repos:
        send_telegram_message(chat_id, "❌ No accessible repos found.")
        return jsonify({"ok": True}), 200

    pending[user_id] = {
        "text": text,
        "username": username,
        "first_name": first_name,
        "chat_id": chat_id,
        "group_name": group_name,
    }

    keyboard = build_repo_keyboard(repos)
    send_telegram_message(
        chat_id,
        f"📁 *{first_name}*, which repo should this issue go to?",
        reply_markup=keyboard,
    )

    return jsonify({"ok": True}), 200


def handle_clarification_reply(user_id, user_state, reply_text, chat_id):
    """User replied to clarification questions — check if complete, ask again if not."""
    original_text = user_state["text"]
    username = user_state["username"]
    first_name = user_state["first_name"]
    group_name = user_state["group_name"]
    repo = user_state["repo"]

    combined_text = f"{original_text} {reply_text}"

    still_missing = get_missing_details(combined_text)
    if still_missing:
        pending[user_id] = {**user_state, "text": combined_text}
        questions = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(still_missing))
        send_telegram_message(
            chat_id,
            f"🤔 Almost there, *{first_name}*! Still need:\n\n{questions}\n\n_Just reply with the remaining details._",
        )
        return jsonify({"ok": True}), 200

    issue_title = f"Telegram from @{username}: {original_text[:80]}"
    issue_body = (
        f"**Requested by:** {first_name} (@{username})\n"
        f"**Telegram User ID:** `{user_id}`\n"
        f"**Group:** {group_name} (`{chat_id}`)\n\n"
        f"---\n\n"
        f"{combined_text}"
    )

    pending.pop(user_id, None)

    try:
        issue = create_github_issue(issue_title, issue_body, repo)
        issue_url = issue.get("html_url", "(URL unavailable)")
        send_telegram_message(
            chat_id,
            f"✅ Issue created in *{repo}*!\n"
            f"⏳ Claude is working on it...\n\n"
            f"[View on GitHub]({issue_url})",
        )
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to create issue: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


def handle_callback(callback):
    """Handle repo selection from inline keyboard."""
    user = callback.get("from", {})
    user_id = user.get("id")
    data = callback.get("data", "")

    requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={"callback_query_id": callback["id"]},
        timeout=10,
    )

    if not data.startswith("repo:"):
        return jsonify({"ok": True}), 200

    selected_repo = data[len("repo:"):]

    user_state = pending.get(user_id)
    if not user_state:
        return jsonify({"ok": True, "info": "no pending message"}), 200

    text = user_state["text"]
    username = user_state["username"]
    first_name = user_state["first_name"]
    chat_id = user_state["chat_id"]
    group_name = user_state["group_name"]

    missing = get_missing_details(text)
    if missing:
        pending[user_id] = {**user_state, "repo": selected_repo, "awaiting_clarification": True}
        questions = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(missing))
        send_telegram_message(
            chat_id,
            f"🤔 *{first_name}*, I need a bit more info before creating the issue:\n\n{questions}\n\n"
            f"_Just reply with `{CLAUDE_MENTION}` followed by your answers and I'll create it right away._\n\n"
            f"_Or send `{CLAUDE_MENTION} /end` to cancel._",
        )
        return jsonify({"ok": True}), 200

    issue_title = f"Telegram from @{username}: {text[:80]}"
    issue_body = (
        f"**Requested by:** {first_name} (@{username})\n"
        f"**Telegram User ID:** `{user_id}`\n"
        f"**Group:** {group_name} (`{chat_id}`)\n\n"
        f"---\n\n"
        f"{text}"
    )

    pending.pop(user_id, None)

    try:
        issue = create_github_issue(issue_title, issue_body, selected_repo)
        issue_url = issue.get("html_url", "(URL unavailable)")
        send_telegram_message(
            chat_id,
            f"✅ Issue created in *{selected_repo}*!\n"
            f"⏳ Claude is working on it...\n\n"
            f"[View on GitHub]({issue_url})",
        )
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to create issue: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
