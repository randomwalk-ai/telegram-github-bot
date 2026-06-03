import os
import json
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()  # Load .env file for local development

app = Flask(__name__)

# ── Environment variables ────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # e.g. "owner/repo"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ── Helpers ──────────────────────────────────────────────────────────
def send_telegram_message(chat_id, text):
    """Send a plain-text message back to a Telegram chat."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def create_github_issue(title, body):
    """Create a GitHub issue labelled 'telegram' and return the HTML URL."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body, "labels": ["telegram"]}
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Routes ───────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    """Health-check / status endpoint."""
    return jsonify({"status": "running", "bot": "telegram-to-github-issues"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive a Telegram update and create a GitHub issue from it."""
    update = request.get_json(force=True)

    # Only process messages that contain text
    message = update.get("message")
    if not message or "text" not in message:
        return jsonify({"ok": True, "info": "no text message – skipped"}), 200

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    username = user.get("username", "unknown")
    first_name = user.get("first_name", "")
    text = message["text"]

    # Build the GitHub issue
    issue_title = f"Telegram from @{username}: {text[:80]}"
    issue_body = (
        f"**From:** {first_name} (@{username})\n"
        f"**Chat ID:** `{chat_id}`\n\n"
        f"---\n\n"
        f"{text}"
    )

    try:
        issue = create_github_issue(issue_title, issue_body)
        issue_url = issue.get("html_url", "(URL unavailable)")
        send_telegram_message(
            chat_id,
            f"✅ Issue created successfully!\n[View on GitHub]({issue_url})",
        )
    except requests.RequestException as exc:
        send_telegram_message(chat_id, f"❌ Failed to create issue: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
