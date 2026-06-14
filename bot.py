"""Telegram bot fronting the unlimited.surf gateway.

Features:
    /start, /help                - greeting + command list
    /models                      - pick a model (provider -> model)
    /effort                      - pick reasoning effort
    /me                          - show your current settings
    /new                         - wipe conversation history for this chat
    /stop                        - cancel an in-flight stream
    /system <prompt>             - set custom system prompt
    /system clear                - drop system prompt
    /stats                       - per-chat stats
    @<botname> <query>           - inline mode: top-N models as quick answers
    in groups: only replies to @mentions, /commands, or direct replies to bot

Behaviour:
    - History is kept locally (last N pairs) and packed into the API's `message`
      field on each call, because the upstream API is stateless.
    - One scope = one DM (per user) or one group chat (shared).
    - Spam guard: if a scope already has a running request, *new* messages are
      ignored silently (no "hold on" spam back).
    - Per-user sliding-window rate limit prevents abuse.
    - Streaming edits a single Telegram message, throttled to respect Telegram's
      edit rate limit. Output is rendered as Telegram HTML (md_to_html).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

import api
import config
import storage
from markdown import md_to_html

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

dp = Dispatcher()

# Cache for /api/models
_models_cache: dict = {"ts": 0.0, "data": []}
_MODELS_TTL = 300

# In-flight tracking: scope -> cancel_event. Existence == busy.
_active_streams: dict[str, asyncio.Event] = {}
_active_lock = asyncio.Lock()

# Optional resolver for callbacks initiated via inline mode (no scope yet).
_inline_jobs: dict[str, dict] = {}


async def fetch_models() -> list[dict]:
    now = time.time()
    if _models_cache["data"] and now - _models_cache["ts"] < _MODELS_TTL:
        return _models_cache["data"]
    models = await api.fetch_models()
    _models_cache["data"] = models
    _models_cache["ts"] = now
    return models


def scope_for_message(message: Message) -> str:
    """Pick the right scope key.

    Groups share a single conversation per chat. DMs are per-user (chat.type=='private',
    chat.id == user.id anyway, but using user.id keeps things explicit).
    """
    if message.chat.type == "private":
        return storage.user_scope(message.from_user.id)
    return storage.chat_scope(message.chat.id)


# ---------- keyboards ----------

TIER_EMOJI = {
    "flagship": "\U0001F451",
    "reasoning": "\U0001F9E0",
    "standard": "\U00002728",
    "fast": "\U000026A1",
}


def main_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="\U0001F916 Модель"), KeyboardButton(text="\u26A1 Усиление")],
        [KeyboardButton(text="\U0001F4CB Настройки"), KeyboardButton(text="\U0001F4CA Статистика")],
        [KeyboardButton(text="\U0001F9F9 Новый чат"), KeyboardButton(text="\u2139\ufe0f Помощь")],
    ]
    if config.WEBAPP_URL:
        rows.insert(0, [KeyboardButton(text="\U0001F310 Открыть чат", web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def provider_kb(providers: list[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for p in providers:
        row.append(InlineKeyboardButton(text=p.capitalize(), callback_data=f"prov:{p}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_kb(models: list[dict], provider: str) -> InlineKeyboardMarkup:
    rows = []
    for m in models:
        if m.get("provider") != provider:
            continue
        label = f"{TIER_EMOJI.get(m.get('tier',''), '')} {m['name']}".strip()
        rows.append([InlineKeyboardButton(text=label, callback_data=f"model:{m['id']}")])
    rows.append([InlineKeyboardButton(text="← Back", callback_data="prov:_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def effort_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚡ Low", callback_data="eff:low"),
        InlineKeyboardButton(text="⚖ Medium", callback_data="eff:medium"),
        InlineKeyboardButton(text="\U0001F9E0 High", callback_data="eff:high"),
    ]])


# ---------- commands ----------

HELP_TEXT = (
    "<b>Unlimited.surf bot</b>\n\n"
    "Just send a message and I'll reply.\n"
    "In groups, mention me or reply to my messages.\n\n"
    "Use the buttons below or type commands manually."
)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


@dp.message(Command("models"))
async def cmd_models(message: Message) -> None:
    try:
        models = await fetch_models()
    except Exception as e:
        await message.answer(f"Couldn't load models: {e}")
        return
    providers = sorted({m.get("provider", "other") for m in models})
    await message.answer("Choose provider:", reply_markup=provider_kb(providers))


@dp.message(Command("effort"))
async def cmd_effort(message: Message) -> None:
    await message.answer("Choose reasoning effort:", reply_markup=effort_kb())


@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    scope = scope_for_message(message)
    entry = await storage.get_scope(scope)
    model = entry.get("model") or config.DEFAULT_MODEL
    effort = entry.get("effort") or config.DEFAULT_EFFORT
    system = entry.get("system")
    msg_count = entry.get("msg_count", 0)
    hist = entry.get("history") or []
    pairs = len(hist) // 2
    sys_line = f"<code>{system[:200]}</code>" if system else "<i>(none)</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Меню", callback_data="menu:back")]
    ])
    await message.answer(
        f"<b>Model:</b> <code>{model}</code>\n"
        f"<b>Effort:</b> {effort}\n"
        f"<b>System prompt:</b> {sys_line}\n"
        f"<b>History:</b> {pairs} turn(s)\n"
        f"<b>Messages sent:</b> {msg_count}",
        reply_markup=kb,
    )


@dp.message(Command("new"))
async def cmd_new(message: Message) -> None:
    scope = scope_for_message(message)
    await storage.clear_history(scope)
    await message.answer("\U0001F9F9 History cleared.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    scope = scope_for_message(message)
    async with _active_lock:
        ev = _active_streams.get(scope)
    if ev is None:
        await message.answer("Nothing to stop.")
        return
    ev.set()
    await message.answer("\U0001F6D1 Stopping...")


@dp.message(Command("system"))
async def cmd_system(message: Message) -> None:
    scope = scope_for_message(message)
    # Strip the leading "/system" (and bot-username suffix, if present)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        current = await storage.get_system(scope)
        if current:
            await message.answer(f"Current system prompt:\n<code>{current}</code>\n\nUse <code>/system clear</code> to remove.")
        else:
            await message.answer("No system prompt set. Use <code>/system your prompt here</code>.")
        return
    if arg.lower() == "clear":
        await storage.update_scope(scope, system=None)
        await message.answer("System prompt cleared.")
        return
    await storage.update_scope(scope, system=arg)
    await message.answer("✅ System prompt set.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    scope = scope_for_message(message)
    entry = await storage.get_scope(scope)
    msg_count = entry.get("msg_count", 0)
    last_used = entry.get("last_used")
    last_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_used)) if last_used else "never"
    await message.answer(
        f"<b>Messages:</b> {msg_count}\n"
        f"<b>Last used:</b> {last_str}"
    )


# ---------- callbacks ----------

@dp.callback_query(F.data.startswith("prov:"))
async def cb_provider(cq: CallbackQuery) -> None:
    provider = cq.data.split(":", 1)[1]
    try:
        models = await fetch_models()
    except Exception as e:
        await cq.answer(f"Error: {e}", show_alert=True)
        return
    if provider == "_back":
        providers = sorted({m.get("provider", "other") for m in models})
        await cq.message.edit_text("Choose provider:", reply_markup=provider_kb(providers))
        await cq.answer()
        return
    await cq.message.edit_text(
        f"Models from <b>{provider}</b>:",
        reply_markup=models_kb(models, provider),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("model:"))
async def cb_model(cq: CallbackQuery) -> None:
    model_id = cq.data.split(":", 1)[1]
    scope = (
        storage.user_scope(cq.from_user.id)
        if cq.message.chat.type == "private"
        else storage.chat_scope(cq.message.chat.id)
    )
    await storage.update_scope(scope, model=model_id)
    await cq.message.edit_text(f"✅ Model set to <code>{model_id}</code>")
    await cq.answer("Saved")


@dp.callback_query(F.data.startswith("eff:"))
async def cb_effort(cq: CallbackQuery) -> None:
    effort = cq.data.split(":", 1)[1]
    scope = (
        storage.user_scope(cq.from_user.id)
        if cq.message.chat.type == "private"
        else storage.chat_scope(cq.message.chat.id)
    )
    await storage.update_scope(scope, effort=effort)
    await cq.message.edit_text(f"✅ Effort set to <b>{effort}</b>")
    await cq.answer("Saved")


# ---------- reply keyboard button handlers ----------

BUTTON_TEXTS = {
    "\U0001F916 Модель": "models",
    "\u26A1 Усиление": "effort",
    "\U0001F4CB Настройки": "me",
    "\U0001F4CA Статистика": "stats",
    "\U0001F9F9 Новый чат": "new",
    "\u2139\ufe0f Помощь": "help",
}


@dp.message(F.text.in_(BUTTON_TEXTS))
async def on_button(message: Message) -> None:
    action = BUTTON_TEXTS[message.text]
    if action == "models":
        try:
            models = await fetch_models()
        except Exception as e:
            await message.answer(f"Couldn't load models: {e}", reply_markup=main_menu_kb())
            return
        providers = sorted({m.get("provider", "other") for m in models})
        await message.answer("Choose provider:", reply_markup=provider_kb(providers))
    elif action == "effort":
        await message.answer("Choose reasoning effort:", reply_markup=effort_kb())
    elif action == "me":
        scope = scope_for_message(message)
        entry = await storage.get_scope(scope)
        model = entry.get("model") or config.DEFAULT_MODEL
        effort = entry.get("effort") or config.DEFAULT_EFFORT
        system = entry.get("system")
        msg_count = entry.get("msg_count", 0)
        hist = entry.get("history") or []
        pairs = len(hist) // 2
        sys_line = f"<code>{system[:200]}</code>" if system else "<i>(none)</i>"
        await message.answer(
            f"<b>Model:</b> <code>{model}</code>\n"
            f"<b>Effort:</b> {effort}\n"
            f"<b>System prompt:</b> {sys_line}\n"
            f"<b>History:</b> {pairs} turn(s)\n"
            f"<b>Messages sent:</b> {msg_count}",
            reply_markup=main_menu_kb(),
        )
    elif action == "stats":
        scope = scope_for_message(message)
        entry = await storage.get_scope(scope)
        msg_count = entry.get("msg_count", 0)
        last_used = entry.get("last_used")
        last_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_used)) if last_used else "never"
        await message.answer(
            f"<b>Messages:</b> {msg_count}\n<b>Last used:</b> {last_str}",
            reply_markup=main_menu_kb(),
        )
    elif action == "new":
        scope = scope_for_message(message)
        await storage.clear_history(scope)
        await message.answer("\U0001F9F9 History cleared.", reply_markup=main_menu_kb())
    elif action == "help":
        await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


# ---------- inline mode ----------
#
# Flow:
#   1. User types `@botname some question` in any chat.
#   2. on_inline() returns one InlineQueryResultArticle whose initial message text
#      is a placeholder ("🤔 thinking..."). The result id encodes (user_id, query).
#   3. When the user taps the result, Telegram inserts the placeholder into the
#      chat AND (because inline feedback is enabled in @BotFather) fires a
#      ChosenInlineResult update containing `inline_message_id`.
#   4. on_chosen_inline() takes that inline_message_id, collects the full model
#      answer (no streaming — inline edits are rate-limited and the UX of one
#      clean swap is better), then does a single bot.edit_message_text(...).
#
# Requirement: in @BotFather, run /setinlinefeedback for this bot and set it to
# `Enabled`. Without it, step 3 won't deliver inline_message_id and we can't edit.

_INLINE_PLACEHOLDER = "🤔 thinking…"


@dp.inline_query()
async def on_inline(query: InlineQuery) -> None:
    q = (query.query or "").strip()
    log.info("inline_query from %s: %r", query.from_user.id, q)
    if not q:
        # Telegram requires at least one result, or the dropdown stays empty.
        await query.answer([], cache_time=1, is_personal=True)
        return

    # Stash the prompt so on_chosen_inline can recover it from the result id alone.
    # We can't fit arbitrary text in the id (64-byte limit), so we use a uuid key.
    result_id = uuid.uuid4().hex
    _inline_jobs[result_id] = {
        "user_id": query.from_user.id,
        "query": q,
        "ts": time.time(),
    }
    _gc_inline_jobs()

    # The model name shown in the suggestion uses the user's current setting.
    scope = storage.user_scope(query.from_user.id)
    entry = await storage.get_scope(scope)
    model = entry.get("model") or config.DEFAULT_MODEL

    results = [
        InlineQueryResultArticle(
            id=result_id,
            title=f"Ask {model}",
            description=q[:120],
            input_message_content=InputTextMessageContent(
                message_text=f"<b>Q:</b> {q}\n\n{_INLINE_PLACEHOLDER}",
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏳ thinking…", callback_data="noop")]
            ]),
        )
    ]
    await query.answer(results, cache_time=0, is_personal=True)


@dp.chosen_inline_result()
async def on_chosen_inline(chosen: ChosenInlineResult, bot: Bot) -> None:
    log.warning(
        "CHOSEN_INLINE fired! user=%s result_id=%s inline_message_id=%r query=%r",
        chosen.from_user.id, chosen.result_id, chosen.inline_message_id, chosen.query,
    )
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        log.error(
            "inline_message_id is None! Set /setinlinefeedback to 100%% in @BotFather. "
            "Current value: %s", chosen.inline_message_id,
        )
        return

    job = _inline_jobs.pop(chosen.result_id, None)
    if not job:
        log.warning("chosen_inline_result for unknown id %s (restart? gc?)", chosen.result_id)
        if chosen.query:
            job = {"user_id": chosen.from_user.id, "query": chosen.query, "ts": time.time()}
        else:
            return

    user_id = chosen.from_user.id
    user_text = job["query"]

    # Per-user rate limit applies here too.
    allowed, wait = await storage.check_rate(user_id)
    if not allowed:
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"<b>Q:</b> {user_text}\n\n⏳ Rate limit hit. Try again in {wait}s.",
            )
        except TelegramBadRequest:
            pass
        return

    scope = storage.user_scope(user_id)
    entry = await storage.get_scope(scope)
    model = entry.get("model") or config.DEFAULT_MODEL
    effort = entry.get("effort") or config.DEFAULT_EFFORT
    system = entry.get("system")
    history = list(entry.get("history") or [])

    packed = api.build_message(user_text, history=history, system=system)
    header = f"<b>Q:</b> {user_text}\n\n"

    buffer = ""
    errored: str | None = None
    try:
        async for evt in api.stream_chat(packed, model, effort):
            if "error" in evt:
                errored = evt["error"]
                break
            if "delta" in evt:
                buffer += evt["delta"]
            if evt.get("done") or evt.get("finish"):
                break
    except Exception as e:
        log.exception("inline chat failed")
        errored = f"internal error: {e}"

    if errored and not buffer.strip():
        final_text = header + f"⚠️ {errored}"
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=final_text[:4090],
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        return

    body = buffer.strip() or "(empty response)"
    rendered = header + md_to_html(body)
    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=rendered[:4090],
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=(header + body)[:4090],
                parse_mode=None,
                reply_markup=None,
            )
        except TelegramBadRequest as e:
            log.error("Failed to edit inline message: %s", e)

    if buffer.strip() and not errored:
        await storage.append_history(scope, user_text, buffer.strip())


def _gc_inline_jobs() -> None:
    cutoff = time.time() - 300
    stale = [k for k, v in _inline_jobs.items() if v.get("ts", 0) < cutoff]
    for k in stale:
        _inline_jobs.pop(k, None)


# ---------- main chat handler ----------
# ---------- main chat handler ----------

def should_handle_in_group(message: Message, bot_username: str) -> bool:
    """Decide whether a non-command group message is addressed to us."""
    if message.chat.type == "private":
        return True
    text = message.text or message.caption or ""
    mention = f"@{bot_username}".lower()
    if mention in text.lower():
        return True
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.username and (
            message.reply_to_message.from_user.username.lower() == bot_username.lower()
        ):
            return True
    return False


def strip_mention(text: str, bot_username: str) -> str:
    mention = f"@{bot_username}"
    return text.replace(mention, "").strip()


@dp.message(F.text & ~F.text.startswith("/"))
async def on_message(message: Message, bot: Bot) -> None:
    me = await bot.me()
    bot_username = me.username or ""
    if not should_handle_in_group(message, bot_username):
        return

    user_id = message.from_user.id
    scope = scope_for_message(message)

    # Rate limit (per user)
    allowed, wait = await storage.check_rate(user_id)
    if not allowed:
        await message.reply(f"⏳ Rate limit hit. Try again in {wait}s.")
        return

    # Drop concurrent requests on the same scope silently.
    async with _active_lock:
        if scope in _active_streams:
            return  # already busy
        cancel_event = asyncio.Event()
        _active_streams[scope] = cancel_event

    try:
        user_text = strip_mention(message.text, bot_username)
        if not user_text:
            await message.reply("Yes? Send a question along with the mention.")
            return

        entry = await storage.get_scope(scope)
        model = entry.get("model") or config.DEFAULT_MODEL
        effort = entry.get("effort") or config.DEFAULT_EFFORT
        system = entry.get("system")
        history = list(entry.get("history") or [])

        packed = api.build_message(user_text, history=history, system=system)

        # Send placeholder, then stream edits into it.
        placeholder = await message.reply("…")
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        buffer = ""
        last_edit = 0.0
        last_rendered = ""
        EDIT_INTERVAL = 1.2  # seconds; Telegram allows ~1 edit/sec safely
        cancelled = False
        errored: str | None = None

        async for evt in api.stream_chat(packed, model, effort, cancel_event=cancel_event):
            if "error" in evt:
                errored = evt["error"]
                break
            if evt.get("cancelled"):
                cancelled = True
                break
            if "delta" in evt:
                buffer += evt["delta"]
                now = time.monotonic()
                if now - last_edit >= EDIT_INTERVAL and buffer != last_rendered:
                    try:
                        await placeholder.edit_text(md_to_html(buffer) + " ▌")
                        last_rendered = buffer
                        last_edit = now
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                    except TelegramBadRequest:
                        # message-not-modified or formatting glitch; ignore
                        pass
            if evt.get("done") or evt.get("finish"):
                break

        # Final render
        final_text = buffer.strip() or ("⚠️ " + errored if errored else "(no response)")
        if cancelled:
            final_text = (buffer.strip() + "\n\n<i>— stopped</i>") if buffer.strip() else "<i>Stopped.</i>"
        try:
            rendered = md_to_html(final_text) if not cancelled else final_text
            # Telegram message limit is 4096 chars; chunk if needed.
            if len(rendered) <= 4000:
                await placeholder.edit_text(rendered)
            else:
                await placeholder.edit_text(rendered[:4000])
                for i in range(4000, len(rendered), 4000):
                    await message.reply(rendered[i:i + 4000])
        except TelegramBadRequest:
            # Fallback to plain text if HTML rendering blows up.
            await placeholder.edit_text(final_text[:4000], parse_mode=None)

        # Persist history only on successful, non-empty completion.
        if buffer.strip() and not errored and not cancelled:
            await storage.append_history(scope, user_text, buffer.strip())

    except Exception:
        log.exception("chat handler failed")
        try:
            await message.reply("⚠️ Internal error. Try again.")
        except Exception:
            pass
    finally:
        async with _active_lock:
            _active_streams.pop(scope, None)


# ---------- entrypoint ----------

async def main() -> None:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("Bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
