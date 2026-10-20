#!/usr/bin/env python3
# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
"""
Himalaya Email Bot — delegates AI agent work from email.

Watches inbox for emails with [AGENT] or [TASK] in the subject.
Parses the command from the email body, spawns a Hermes agent,
and replies to the sender with results.

Features: config validation, error handling, graceful shutdown,
          health checks, auto-restart, improved email templates.

Usage:
  Systemd: himalaya-bot.service (runs continuously)
  Manual:  python3 himalaya_bot.py --once    (single poll)
  Health:  python3 himalaya_bot.py --health   (check status)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import signal
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
STATE_FILE = BOT_DIR / "bot_state.json"
LOG_DIR = Path.home() / ".local" / "share" / "himalaya-bot"
LOG_FILE = LOG_DIR / "bot.log"
PID_FILE = LOG_DIR / "bot.pid"

HIMALAYA_ACCOUNT = "intricos777"
INBOX_FOLDER = "INBOX"
AGENT_COMMAND = ["opencode", "run"]
POLL_INTERVAL = 30  # seconds between inbox checks when running as daemon
PROCESSED_FOLDER = "Processed"
ERROR_FOLDER = "Errors"

# ── Logging setup ──────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("himalaya-bot")

# ── Graceful shutdown ──────────────────────────────────────────────────
shutdown_requested = False

def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    sig_name = signal.Signals(signum).name
    log.info("Received %s, initiating graceful shutdown...", sig_name)
    global shutdown_requested
    shutdown_requested = True

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ── Health check / self-diagnostic ──────────────────────────────────────
def health_check() -> dict:
    """
    Run self-diagnostic checks. Returns status dict.
    """
    result = {
        "timestamp": datetime.now().isoformat(),
        "healthy": True,
        "checks": {},
    }

    # State file check
    result["checks"]["state_file"] = {
        "exists": STATE_FILE.exists(),
        "path": str(STATE_FILE),
    }
    if not STATE_FILE.exists():
        result["healthy"] = False

    # Log directory check
    result["checks"]["log_dir"] = {
        "exists": LOG_DIR.exists(),
        "path": str(LOG_DIR),
    }
    if not LOG_DIR.exists():
        result["healthy"] = False

    # Himalaya binary check
    try:
        r = subprocess.run(["which", "himalaya"], capture_output=True, text=True, timeout=5)
        result["checks"]["himalaya_binary"] = r.returncode == 0
        if not result["checks"]["himalaya_binary"]:
            result["healthy"] = False
    except Exception as e:
        result["checks"]["himalaya_binary"] = False
        result["checks"]["himalaya_error"] = str(e)
        result["healthy"] = False

    # Himalaya account check
    try:
        r = subprocess.run(
            ["himalaya", "account", "list"],
            capture_output=True, text=True, timeout=15
        )
        result["checks"]["himalaya_account"] = r.returncode == 0
        if r.returncode == 0:
            result["checks"]["accounts"] = r.stdout.strip()
        else:
            result["checks"]["himalaya_error"] = r.stderr.strip()
            result["healthy"] = False
    except Exception as e:
        result["checks"]["himalaya_account"] = False
        result["checks"]["himalaya_error"] = str(e)
        result["healthy"] = False

    # Agent command check
    result["checks"]["agent_command"] = {
        "command": " ".join(AGENT_COMMAND),
        "available": False,
    }
    try:
        r = subprocess.run(
            AGENT_COMMAND + ["--version"],
            capture_output=True, text=True, timeout=10
        )
        result["checks"]["agent_command"]["available"] = r.returncode == 0
        if not result["checks"]["agent_command"]["available"]:
            result["healthy"] = False
    except Exception as e:
        result["checks"]["agent_command"]["error"] = str(e)
        result["healthy"] = False

    # State summary
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            result["checks"]["state_summary"] = {
                "processed_count": len(state.get("processed", [])),
                "last_updated": state.get("last_updated"),
            }
        except Exception as e:
            result["checks"]["state_summary"] = {"error": str(e)}

    return result

def validate_config() -> bool:
    """Validate configuration at startup."""
    log.info("Validating configuration...")
    errors = []

    if not HIMALAYA_ACCOUNT:
        errors.append("HIMALAYA_ACCOUNT not configured")

    if not (BOT_DIR).exists():
        errors.append(f"Bot directory not found: {BOT_DIR}")

    if not AGENT_COMMAND:
        errors.append("AGENT_COMMAND not configured")

    if errors:
        for e in errors:
            log.error("Config error: %s", e)
        return False

    log.info("Configuration validated OK")
    return True

# ── State management ─────────────────────────────────────────────────────
def load_state() -> dict:
    """Load processed email IDs to avoid reprocessing."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt, starting fresh")
            return {}
    return {}

def save_state(state: dict) -> bool:
    """Persist processed email IDs."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
        state["last_updated"] = datetime.now().isoformat()
        return True
    except Exception as e:
        log.error("Failed to save state: %s", e)
        return False

# ── Himalaya helpers ────────────────────────────────────────────────────
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

def list_unread_envelopes() -> list:
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

    try:
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
    except subprocess.TimeoutExpired:
        log.error("Reply send timed out to %s", to)
        return False
    except Exception as e:
        log.error("send_reply exception for %s: %s", to, e)
        return False

# ── Agent delegation ────────────────────────────────────────────────────
def extract_command(email_body: str) -> str:
    """Extract the agent command from the email body."""
    # Try structured command pattern first
    COMMAND_PATTERN = re.compile(
        r"(?:^|\n)(?:command|task|prompt|instruction)s?[:\s]*(.+?)(?=\n\n|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
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
    except FileNotFoundError:
        return f"[Agent Error] Command not found: {' '.join(AGENT_COMMAND)}"
    except Exception as e:
        log.error("Agent execution exception: %s", e)
        return f"[Agent Error] {e}"

# ── Email processing ────────────────────────────────────────────────────
def process_email(envelope: dict, state: dict) -> bool:
    """Process a single agent-command email. Returns True on success."""
    envelope_id = envelope.get("id")
    subject = envelope.get("subject", "")
    from_addr = envelope.get("from", "")

    if not subject or not AGENT_PATTERN.match(subject):
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

    # Improved reply template
    reply_subject = f"Re: {subject}"
    reply_body = f"""🤖 Agent Task Completed

Your command:
{'─' * 50}
{command}
{'─' * 50}

Result:
{'─' * 25}
{result}
{'─' * 25}

---
Himalaya Email Bot
Sent at {datetime.now(timezone.utc).isoformat()}

---
If you need further assistance, reply to this email with a new [AGENT] or [TASK] command.
"""

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

# ── Main polling loop ────────────────────────────────────────────────────
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
    """Run the bot in continuous polling mode with auto-restart."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Ensure required folders exist
    ensure_folder(PROCESSED_FOLDER)
    ensure_folder(ERROR_FOLDER)

    # Write PID file
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        log.warning("Could not write PID file: %s", e)

    state = load_state()
    log.info("Starting Himalaya Email Bot daemon (poll interval: %ds)", POLL_INTERVAL)

    # Validate config
    if not validate_config():
        log.error("Configuration validation failed, exiting")
        sys.exit(1)

    # Run initial health check
    health = health_check()
    if not health["healthy"]:
        log.warning("Initial health check: DEGRADED")
    else:
        log.info("Initial health check: OK")

    consecutive_failures = 0
    max_failures = 5

    while not shutdown_requested:
        try:
            count = poll_inbox(state)
            if count > 0:
                log.info("Processed %d agent email(s)", count)

            # Reset failure counter on success
            consecutive_failures = 0

        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)")
            break
        except Exception as e:
            consecutive_failures += 1
            log.exception("Unexpected error in poll loop (failure %d/%d): %s",
                         consecutive_failures, max_failures, e)

            if consecutive_failures >= max_failures:
                log.error("Too many consecutive failures, restarting daemon...")
                # In production, this would trigger a systemctl restart
                consecutive_failures = 0

        # Sleep with shutdown check
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    # Cleanup PID file
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass

    log.info("Himalaya Email Bot shut down gracefully")

def single_poll() -> int:
    """Run one poll cycle and exit. Returns count processed."""
    state = load_state()
    count = poll_inbox(state)
    log.info("Single poll complete: %d email(s) processed", count)
    return count

def send_test_email(to: str, subject: str, body: str) -> bool:
    """Send a test email via himalaya (for testing the bot)."""
    msg = f"From: {HIMALAYA_ACCOUNT}@gmail.com\nTo: {to}\nSubject: {subject}\n\n{body}\n"
    try:
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
    except Exception as e:
        log.error("send_test_email exception: %s", e)
        return False

# ── CLI entry point ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Himalaya Email Bot - AI Agent Email Delegation")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (default)")
    parser.add_argument("--test-send", metavar="EMAIL", help="Send a test agent email to EMAIL")
    parser.add_argument("--status", action="store_true", help="Show bot status")
    parser.add_argument("--health", action="store_true", help="Run health check and exit")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.health:
        health = health_check()
        print(json.dumps(health, indent=2))
        sys.exit(0 if health["healthy"] else 1)

    if args.status:
        state = load_state()
        print(f"Bot state: {len(state.get('processed', []))} processed emails")
        print(f"Log file: {LOG_FILE}")
        print(f"PID file: {PID_FILE}")
        print(f"Poll interval: {POLL_INTERVAL}s")
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
