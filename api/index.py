import os
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

# ── In-memory state: tracks repo selection per user ─────────────────
# { user_id: { "repo": "owner/repo", "text": "original message" } }
pending = {}


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
    # Use code search to find repos in the org with claude-code-action in workflows
    search_url = "https://api.github.com/search/code"
    params = {
        "q": "anthropics/claude-code-action org:randomwalk-ai path:.github/workflows",
        "per_page": 100,
    }
    resp = requests.get(search_url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    # Deduplicate by repo full_name
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

    # ── Layer 2: Only process messages starting with @claude ───────────
    if not text.startswith("@claude"):
        send_telegram_message(
            chat_id,
            "⚠️ Please start your message with @claude to create an issue.\n\nExample:\n`@claude add a footer to the homepage`"
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

    # Store the message while user picks a repo
    pending[user_id] = {"text": text, "username": username, "first_name": first_name, "chat_id": chat_id, "group_name": group_name}

    keyboard = build_repo_keyboard(repos)
    send_telegram_message(
        chat_id,
        f"📁 *{first_name}*, which repo should this issue go to?",
        reply_markup=keyboard
    )

    return jsonify({"ok": True}), 200


def handle_callback(callback):
    """Handle repo selection from inline keyboard."""
    user = callback.get("from", {})
    user_id = user.get("id")
    data = callback.get("data", "")

    # Answer the callback to remove the loading spinner on the button
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                  json={"callback_query_id": callback["id"]}, timeout=10)

    if not data.startswith("repo:"):
        return jsonify({"ok": True}), 200

    selected_repo = data[len("repo:"):]

    # Retrieve the pending message for this user
    user_state = pending.pop(user_id, None)
    if not user_state:
        return jsonify({"ok": True, "info": "no pending message"}), 200

    text = user_state["text"]
    username = user_state["username"]
    first_name = user_state["first_name"]
    chat_id = user_state["chat_id"]
    group_name = user_state["group_name"]

    # Build the GitHub issue
    issue_title = f"Telegram from @{username}: {text[:80]}"
    issue_body = (
        f"**Requested by:** {first_name} (@{username})\n"
        f"**Telegram User ID:** `{user_id}`\n"
        f"**Group:** {group_name} (`{chat_id}`)\n\n"
        f"---\n\n"
        f"{text}"
    )

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