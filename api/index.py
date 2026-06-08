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

# Keywords that indicate the user wants to CREATE something new (not modify existing)
CREATE_KEYWORDS = [
    "create", "add a new", "new page", "new file", "new component", "new route",
    "build a", "make a", "generate a", "scaffold",
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


def is_create_request(content):
    """Returns True if the request is about creating a new file, page, or component."""
    return any(kw in content for kw in CREATE_KEYWORDS)


def get_missing_details(text):
    """Return clarifying questions for a vague @claude request. Empty list = specific enough."""
    content = text.replace(CLAUDE_MENTION, "").strip().lower()
    missing = []

    if is_create_request(content):
        # CREATE requests: only check that there's enough description of what to build.
        # Don't ask "which file?" — the route/name is part of the request.
        # A route pattern like /users or /dashboard counts as sufficient path info.
        has_route = bool(re.search(r"/\w+", content))
        words = content.split()
        if not has_route and len(words) < 6:
            missing.append("📁 *Where should it go?* Provide the route or folder (e.g. `/users` or `app/users/page.tsx`)")
        if len(words) < 5:
            missing.append("🎯 *What should it contain?* Describe the content or functionality of the new page/file")
    else:
        # MODIFY requests: need file path, exact location, and what to change.
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

    # ── Layer 2: Check if user is answering a clarification question ───
    user_state = pending.get(user_id)
    if user_state and user_state.get("awaiting_clarification"):
        # Strip @claude prefix if present so it doesn't pollute the combined text
        reply = text[len(CLAUDE_MENTION):].strip() if text.startswith(CLAUDE_MENTION) else text
        return handle_clarification_reply(user_id, user_state, reply, chat_id)

    # ── Layer 3: Only process messages starting with @claude ───────────
    if not text.startswith(CLAUDE_MENTION):
        send_telegram_message(
            chat_id,
            f"⚠️ Please start your message with {CLAUDE_MENTION} to create an issue.\n\nExample:\n`{CLAUDE_MENTION} add a footer to the homepage`",
        )
        return jsonify({"ok": True}), 200

    # ── Fetch repos and show selection keyboard ────────────────────────
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

    # Accumulate answers into the original text
    combined_text = f"{original_text} {reply_text}"

    # Still missing something? Ask again for only the remaining gaps
    still_missing = get_missing_details(combined_text)
    if still_missing:
        pending[user_id] = {**user_state, "text": combined_text}
        questions = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(still_missing))
        send_telegram_message(
            chat_id,
            f"🤔 Almost there, *{first_name}*! Still need:\n\n{questions}\n\n_Just reply with the remaining details._",
        )
        return jsonify({"ok": True}), 200

    # All details provided — create the issue
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
            f"✅ Issue created in *{repo}*!\n[View on GitHub]({issue_url})",
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

    # ── Check if the request is vague ────────────────────────────────
    missing = get_missing_details(text)
    if missing:
        pending[user_id] = {**user_state, "repo": selected_repo, "awaiting_clarification": True}
        questions = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(missing))
        send_telegram_message(
            chat_id,
            f"🤔 *{first_name}*, I need a bit more info before creating the issue:\n\n{questions}\n\n_Just reply with `@claude` followed by your answers and I'll create it right away._",
        )
        return jsonify({"ok": True}), 200

    # ── Request is specific enough — create the issue immediately ─────
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
            f"✅ Issue created in *{selected_repo}*!\n[View on GitHub]({issue_url})",
        )
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to create issue: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
