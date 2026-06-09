import os
import re
import threading
import time
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

# Keywords that indicate the user wants to CREATE something new
CREATE_KEYWORDS = [
    "create", "add a new", "new page", "new file", "new component", "new route",
    "build a", "make a", "generate a", "scaffold",
]

# Keywords that indicate a code-related task (modify existing code)
MODIFY_KEYWORDS = [
    "change", "update", "fix", "rename", "highlight", "remove", "delete", "edit",
    "modify", "refactor", "implement", "style", "replace", "move", "add",
]


# ── Helpers ──────────────────────────────────────────────────────────
def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


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

    # ── Layer 6: Code task — fetch repos and show selection keyboard ───
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
        issue_number = issue.get("number")
        send_telegram_message(
            chat_id,
            f"✅ Issue created in *{repo}*!\n[View on GitHub]({issue_url})\n\n_Waiting for Claude's reply…_",
        )
        threading.Thread(
            target=poll_for_claude_reply,
            args=(repo, issue_number, chat_id),
            daemon=True,
        ).start()
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
        issue_number = issue.get("number")
        send_telegram_message(
            chat_id,
            f"✅ Issue created in *{selected_repo}*!\n[View on GitHub]({issue_url})\n\n_Waiting for Claude's reply…_",
        )
        threading.Thread(
            target=poll_for_claude_reply,
            args=(selected_repo, issue_number, chat_id),
            daemon=True,
        ).start()
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to create issue: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


def poll_for_claude_reply(repo, issue_number, chat_id, interval=30, timeout=300):
    """Poll GitHub issue comments until Claude responds, then send to Telegram."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            comments = resp.json()
            for comment in comments:
                login = comment.get("user", {}).get("login", "")
                if login in ("github-actions[bot]", "claude[bot]"):
                    body = comment.get("body", "").strip()
                    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
                    msg = f"🤖 *Claude replied on [Issue #{issue_number}]({issue_url}):*\n\n{body}"
                    if len(msg) > 4096:
                        msg = msg[:4090] + "…"
                    send_telegram_message(chat_id, msg)
                    return
        except Exception:
            pass  # keep polling on transient errors


@app.route("/api/github-webhook", methods=["POST"])
def github_webhook():
    """Receive GitHub issue comment events and forward Claude's replies to Telegram."""
    event = request.headers.get("X-GitHub-Event", "")
    if event != "issue_comment":
        return jsonify({"ok": True, "info": "ignored"}), 200

    payload = request.get_json(force=True)
    action = payload.get("action", "")
    if action != "created":
        return jsonify({"ok": True, "info": "ignored"}), 200

    comment = payload.get("comment", {})
    commenter = comment.get("user", {}).get("login", "")
    # Only forward comments from Claude Code Action bot
    if commenter not in ("github-actions[bot]", "claude[bot]"):
        return jsonify({"ok": True, "info": "not claude"}), 200

    comment_body = comment.get("body", "").strip()
    issue = payload.get("issue", {})
    issue_body = issue.get("body", "")
    issue_url = issue.get("html_url", "")
    issue_number = issue.get("number", "")

    # Extract chat_id from issue body line: **Group:** name (`-123456789`)
    chat_id_match = re.search(r"\*\*Group:\*\*[^`]*`(-?\d+)`", issue_body)
    if not chat_id_match:
        return jsonify({"ok": True, "info": "no chat_id in issue"}), 200

    chat_id = int(chat_id_match.group(1))

    msg = f"🤖 *Claude replied on [Issue #{issue_number}]({issue_url}):*\n\n{comment_body}"
    # Telegram message limit is 4096 chars
    if len(msg) > 4096:
        msg = msg[:4090] + "…"

    try:
        send_telegram_message(chat_id, msg)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
