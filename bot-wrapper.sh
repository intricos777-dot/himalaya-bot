#!/bin/bash
# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Wrapper script for Himalaya Email Bot
# Sets up the Python venv PATH so the pass shim can find cryptography

export PATH="/home/livings/agents/himalaya-bot/.venv/bin:$PATH"
export HIMALAYA_CONFIG="/home/livings/.config/himalaya/config.toml"
export HOME="/home/livings"
export TERM="dumb"

# Ensure log directory exists
mkdir -p /home/livings/.local/share/himalaya-bot

# Run the bot
exec python3 /home/livings/agents/himalaya-bot/himalaya_bot.py --daemon
