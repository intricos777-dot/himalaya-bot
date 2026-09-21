#!/usr/bin/env python3
"""
Himalaya Email Bot — delegates AI agent work from email.

Watches inbox for emails with [AGENT] or [TASK] in the subject.
Parses the command from the email body, spawns a Hermes agent,
and replies to the sender with results.

Usage:
  Systemd: himalaya-bot.service (runs continuously)
  Manual:  python3 himalaya_bot.py --once    (single poll)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import email
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────
BOT_DIR = Path(__file__).parent
STATE_FILE = BOT_DIR / "bot_state.json"
LOG_FILE = Path.home() / ".local" / "share" / "himalaya-bot" / "bot.log"
PID_FILE = Path.home() / ".local" / "share" / "himalaya-bot" / "bot.pid"

HIMALAYA_ACCOUNT = "intricos777"
INBOX_FOLDER = "INBOX"
AGENT_COMMAND = ["opencode", "run"]
POLL_INTERVAL = 30  # seconds between inbox checks when running as daemon
PROCESSED_FOLDER = "Processed"  # folder to move processed emails to
ERROR_FOLDER = "Errors"  # folder to move emails that failed processing

# ── Logging setup ──────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("himalaya-bot")

# ── Subject pattern matching ───────────────────────────────
AGENT_PATTERN = re.compile(r"^\[(AGENT|TASK|AI)\]", re.IGNORECASE)
COMMAND_PATTERN = re.compile(
    r"(?:^|\n)(?:command|task|prompt|instruction)s?[:\s]*(.+?)(?=\n\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# ── State management ───────────────────────────────────────
def load_state() -> dict:
    """Load processed email IDs to avoid reprocessing."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_state(state: dict) -> None:
    """Persist processed email IDs."""
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Himalaya helpers ────────────────────────────────────────
def run_himalaya(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a himalaya command and return the result."""
    cmd = ["himalaya"] + list(args)
    log.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        log.error("Himalaya timed out after %ds: %s", timeout, " ".join(cmd))
        raise

def ensure_folder(folder: str) -> bool:
    """Create a folder if it doesn't exist (best-effort)."""
    result = run_himalaya("mailbox", "create", folder)
    if result.returncode != 0 and "already exists" not in result.stderr.lower():
        log.warning("Could not create folder %s: %s", folder, result.stderr.strip())
        return False
    return True

def list_unread_envelopes() -> list[dict]:
    """List unread (flagged as unseen) envelopes from the inbox."""
    result = run_himalaya("envelope", "list", "--json", "--page-size", "50")
    if result.returncode != 0:
        log.error("Failed to list envelopes: %s", result.stderr.strip())
        return []
    try:
        data = json.loads(result.stdout)
        envelopes = data if isinstance(data, list) else data.get("envelopes", [])
        return envelopes
    except json.JSONDecodeError:
        log.error("Failed to parse envelope list JSON: %s", result.stdout[:200])
        return []

def is_unseen(envelope: dict) -> bool:
    """Check if an envelope is unread (not seen)."""
    flags = envelope.get("flags", [])
    if isinstance(flags, list):
        return "seen" not in flags and "Seen" not in flags
    return not envelope.get("seen", False)

def read_message(envelope_id: int) -> str:
    """Read the full text body of a message by envelope ID."""
    result = run_himalaya("message", "read", str(envelope_id))
    if result.returncode != 0:
        log.error("Failed to read message %d: %s", envelope_id, result.stderr.strip())
        return ""
    return result.stdout.strip()

def read_message_headers(envelope_id: int) -> dict:
    """Extract relevant headers from a message."""
    result = run_himalaya("message", "read", str(envelope_id))
    if result.returncode != 0:
        return {}
    
    text = result.stdout
    headers = {}
    for line in text.split("\n"):
        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key in ("from", "to", "subject", "message-id", "in-reply-to", "date", "content-type"):
                headers[key] = value
    return headers

def move_message(folder: str, envelope_id: int) -> bool:
    """Move a message to a folder."""
    result = run_himalaya("message", "move", folder, str(envelope_id))
    if result.returncode != 0:
        log.error("Failed to move message %d to %s: %s", envelope_id, folder, result.stderr.strip())
        return False
    return True

def delete_message(envelope_id: int) -> bool:
    """Delete a message."""
    result = run_himalaya("message", "delete", str(envelope_id))
    if result.returncode != 0:
        log.error("Failed to delete message %d: %s", envelope_id, result.stderr.strip())
        return False
    return True

def send_reply(to: str, subject: str, body: str, in_reply_to: str = None) -> bool:
    """Send an email reply via himalaya template send."""
    msg = f"From: {HIMALAYA_ACCOUNT}@gmail.com\nTo: {to}\nSubject: {subject}\n"
    if in_reply_to:
        msg += f"In-Reply-To: <{in_reply_to}>\n"
    msg += f"\n{body}\n"
    
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=msg,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error("Failed to send reply: %s", result.stderr.strip())
        return False
    log.info("Reply sent to %s (subject: %s)", to, subject)
    return True

# ── Agent delegation ────────────────────────────────────────
def extract_command(email_body: str) -> str:
    """Extract the agent command from the email body."""
    # Try structured command pattern first
    match = COMMAND_PATTERN.search(email_body)
    if match:
        return match.group(1).strip()
    
    # Otherwise use the whole body (minus common email artifacts)
    body = email_body.strip()
    
    # Remove quoted reply sections
    body = re.sub(r"\n>.*$", "", body, flags=re.MULTILINE | re.DOTALL)
    
    # Remove common email footer patterns
    body = re.sub(r"\n-- \n.*$", "", body, flags=re.DOTALL)
    body = re.sub(r"\n_sent from my .*$", "", body, flags=re.DOTALL | re.IGNORECASE)
    
    return body.strip()

def run_agent(command: str, timeout: int = 300) -> str:
    """Spawn an OpenCode agent to execute the command. Returns the output."""
    log.info("Delegating to agent: %s", command[:200])

    try:
        result = subprocess.run(
            AGENT_COMMAND + [command],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "TERM": "dumb"},
        )
        
        output = result.stdout.strip()
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            log.error("Agent exited with code %d: %s", result.returncode, error_msg)
            return f"[Agent Error] Exit code {result.returncode}\n{error_msg}\n\nPartial output:\n{output}"
        
        return output or "[Agent completed with no output]"
    except subprocess.TimeoutExpired:
        log.error("Agent timed out after %ds", timeout)
        return f"[Agent Timeout] Task timed out after {timeout} seconds."


# ── Email processing ────────────────────────────────────────
def process_email(envelope: dict, state: dict) -> bool:
    """Process a single agent-command email. Returns True on success."""
    envelope_id = envelope.get("id")
    subject = envelope.get("subject", "")
    from_addr = envelope.get("from", "")
    
    if not AGENT_PATTERN.match(subject):
        return False  # Not an agent command, skip
    
    msg_key = f"{envelope_id}-{subject}"
    if msg_key in state.get("processed", []):
        log.debug("Already processed: %s", msg_key)
        return False
    
    log.info("Processing agent email from %s (ID %s): %s", from_addr, envelope_id, subject)
    
    # Read message body
    body = read_message(envelope_id)
    headers = read_message_headers(envelope_id)
    
    if not body:
        log.error("Empty body from %s, marking as processed anyway", from_addr)
        state.setdefault("processed", []).append(msg_key)
        save_state(state)
        return False
    
    # Extract command and run agent
    command = extract_command(body)
    log.info("Extracted command: %s", command[:200])
    
    result = run_agent(command)
    
    # Reply to sender
    reply_subject = f"Re: {subject}"
    reply_body = textwrap.dedent(f"""\
        🤖 Agent Task Completed
        
        Your command:
        ────────────────────────────────────
        {command}
        ────────────────────────────────────
        
        Result:
        ────────
        {result}
        ────────
        
        — Himalaya Email Bot
        Sent at {datetime.now(timezone.utc).isoformat()}
    """)
    
    message_id = headers.get("message-id", "")
    sent_ok = send_reply(from_addr, reply_subject, reply_body, in_reply_to=message_id)
    
    if sent_ok:
        log.info("Reply sent for %s", msg_key)
    else:
        log.error("Failed to send reply for %s", msg_key)
    
    # Move to Processed folder (or delete as fallback)
    if ensure_folder(PROCESSED_FOLDER):
        move_message(PROCESSED_FOLDER, envelope_id)
    else:
        delete_message(envelope_id)
    
    # Mark as processed
    state.setdefault("processed", []).append(msg_key)
    # Keep only last 500 entries to avoid unbounded growth
    state["processed"] = state["processed"][-500:]
    save_state(state)
    
    return True

# ── Main polling loop ───────────────────────────────────────
def poll_inbox(state: dict) -> int:
    """Poll inbox for new agent emails. Returns count processed."""
    envelopes = list_unread_envelopes()
    if not envelopes:
        return 0
    
    processed = 0
    for envelope in envelopes:
        try:
            if is_unseen(envelope) and process_email(envelope, state):
                processed += 1
        except Exception as e:
            log.exception("Error processing envelope %s: %s", envelope.get("id"), e)
            # Move to error folder to avoid reprocessing
            if ensure_folder(ERROR_FOLDER):
                move_message(ERROR_FOLDER, envelope.get("id", 0))
    
    return processed

def daemon_mode() -> None:
    """Run the bot in continuous polling mode."""

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Ensure required folders exist
    ensure_folder(PROCESSED_FOLDER)
    ensure_folder(ERROR_FOLDER)

    state = load_state()
    log.info("Starting Himalaya Email Bot daemon (poll interval: %ds)", POLL_INTERVAL)

    while True:
        try:
            count = poll_inbox(state)
            if count > 0:
                log.info("Processed %d agent email(s)", count)
        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)")
            break
        except Exception as e:
            log.exception("Unexpected error in poll loop: %s", e)

        time.sleep(POLL_INTERVAL)

def single_poll() -> int:
    """Run one poll cycle and exit. Returns count processed."""
    state = load_state()
    count = poll_inbox(state)
    log.info("Single poll complete: %d email(s) processed", count)
    return count

def send_test_email(to: str, subject: str, body: str) -> bool:
    """Send a test email via himalaya (for testing the bot)."""
    msg = f"From: {HIMALAYA_ACCOUNT}@gmail.com\nTo: {to}\nSubject: {subject}\n\n{body}\n"
    result = subprocess.run(
        ["himalaya", "message", "send"],
        input=msg,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error("Failed to send test email: %s", result.stderr.strip())
        return False
    log.info("Test email sent to %s", to)
    return True

# ── CLI entry point ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Himalaya Email Bot")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (default)")
    parser.add_argument("--test-send", metavar="EMAIL", help="Send a test agent email to EMAIL")
    parser.add_argument("--status", action="store_true", help="Show bot status")
    args = parser.parse_args()

    if args.status:
        state = load_state()
        print(f"Bot state: {len(state.get('processed', []))} processed emails")
        print(f"Log file: {LOG_FILE}")
        print(f"PID file: {PID_FILE}")
        return

    if args.test_send:
        test_subject = "[AGENT] Test command from bot"
        test_body = "This is a test email from the Himalaya Email Bot.\n\nCommand: Reply with 'Hello from the agent!' if you can read this."
        ok = send_test_email(args.test_send, test_subject, test_body)
        sys.exit(0 if ok else 1)

    if args.once:
        count = single_poll()
        sys.exit(0 if count >= 0 else 1)
    else:
        daemon_mode()

if __name__ == "__main__":
    main()
