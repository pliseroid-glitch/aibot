"""Configuration for the bot.

Fill in BOT_TOKEN with your Telegram bot token from @BotFather.
API_KEY is your unlimited.surf API key.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Telegram bot token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")

# unlimited.surf API key
API_KEY = os.getenv("UNLIMITED_API_KEY")

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN env var is not set. Set it in .env or Railway Variables.", file=sys.stderr)
    sys.exit(1)

# API endpoints
API_BASE = "https://unlimited.surf/api"
MODELS_URL = f"{API_BASE}/models"
CHAT_URL = f"{API_BASE}/chat"

# Defaults
DEFAULT_MODEL = "gateway-gpt-5-nano"
DEFAULT_EFFORT = "medium"  # low | medium | high

# File where per-user settings are stored
STATE_FILE = "user_state.json"

# How often to edit the Telegram message during streaming (seconds).
# Telegram rate-limits message edits, so we batch deltas.
STREAM_EDIT_INTERVAL = 1.2

# Max length of a Telegram message; we split if the answer is longer.
TG_MAX_LEN = 4000

# Rate limit: max chat requests per user per minute.
RATE_LIMIT_PER_MIN = 15

# Bot username, auto-filled at startup from getMe(). Used to detect
# mentions in groups.
BOT_USERNAME = ""

# Inline mode: how many top models to expose as inline answers.
INLINE_MAX_RESULTS = 5

# Inline cache timeout (seconds). Telegram clients won't re-query within this window.
INLINE_CACHE_TIME = 0
