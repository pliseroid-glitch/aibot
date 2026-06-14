# unlimited.surf Telegram bot

Telegram → [unlimited.surf](https://unlimited.surf) gateway. Picks any model, streams the answer back, keeps conversation history, works in groups, supports inline mode.

## Install

```bash
pip install -r requirements.txt
```

Set your tokens either via env vars:

```bash
export BOT_TOKEN="123:abc..."
export UNLIMITED_API_KEY="ua_..."
```

...or edit `config.py` directly.

## Run

```bash
python bot.py
```

In @BotFather, enable inline mode and set bot privacy off if you want it to see all group messages (otherwise only `/commands` and `@mentions` reach it — which is fine for this bot).

## Commands

| Command          | Effect                                                  |
|------------------|---------------------------------------------------------|
| `/start /help`   | Greeting + command list                                 |
| `/models`        | Pick provider → model via inline buttons                |
| `/effort`        | Low / Medium / High reasoning effort                    |
| `/me`            | Show current settings + history size                    |
| `/new`           | Clear conversation history in this chat                 |
| `/stop`          | Cancel the currently streaming reply                    |
| `/system <text>` | Set a custom system prompt (`/system clear` to remove)  |
| `/stats`         | Per-chat usage stats                                    |

## Inline mode

Type `@<botname> your question` in any chat, pick the suggestion — the message is sent into that chat and (if the bot is there) gets a streamed reply.

## Groups

- Bot reacts only to: `/commands`, `@mentions`, or direct replies to its own messages.
- The whole group shares one conversation context (one `/new` clears it for everyone).
- Each user still has their own rate limit.

## Anti-spam behaviour

- **Concurrent**: if a request is already streaming in this chat, additional messages from anyone are silently ignored until it finishes.
- **Sliding rate limit**: per-user max `RATE_LIMIT_PER_MIN` messages per 60 s (configurable in `config.py`).

## How history works

The upstream API only accepts a single `message` string and does not persist conversations server-side (we verified — passing `chatId` back doesn't continue the chat). So history is kept locally in `user_state.json`: the last 20 user/assistant pairs per scope are prepended to each new request as a labelled transcript. `/new` wipes it.

## Files

- `bot.py` — aiogram handlers, streaming loop, inline mode, group filter
- `api.py` — unlimited.surf client with `build_message()` history packer
- `storage.py` — JSON-file storage + in-memory rate limiter
- `markdown.py` — Markdown → Telegram HTML converter
- `config.py` — tokens, URLs, defaults
- `user_state.json` — created at runtime
