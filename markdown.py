"""Minimal Markdown -> Telegram HTML converter.

Telegram's HTML subset supports: <b> <i> <u> <s> <code> <pre> <a> <blockquote>.
We support the common Markdown that LLM responses use:

    **bold**, __bold__       -> <b>...</b>
    *italic*, _italic_       -> <i>...</i>
    ~~strike~~               -> <s>...</s>
    `inline code`            -> <code>...</code>
    ```lang\ncode\n```      -> <pre><code class="language-lang">...</code></pre>
    [text](url)              -> <a href="url">text</a>

Everything inside code blocks/spans is HTML-escaped and left untouched
otherwise. Outside code we escape <, >, & to keep Telegram happy.

Not bullet-proof — but robust enough for streamed partial markdown.
"""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"```([\w+-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?!\w)|(?<![\w_])_([^_\n]+)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def md_to_html(text: str) -> str:
    """Convert a markdown string into Telegram-safe HTML."""
    if not text:
        return ""

    placeholders: list[str] = []

    def stash(html_snippet: str) -> str:
        token = f"\x00P{len(placeholders)}\x00"
        placeholders.append(html_snippet)
        return token

    # 1. Fenced code blocks first.
    def _fence(m: re.Match) -> str:
        lang = m.group(1) or ""
        body = m.group(2)
        escaped = html.escape(body, quote=False).rstrip()
        if lang:
            snippet = f'<pre><code class="language-{html.escape(lang, quote=True)}">{escaped}</code></pre>'
        else:
            snippet = f"<pre>{escaped}</pre>"
        return stash(snippet)

    text = _FENCE_RE.sub(_fence, text)

    # 2. Inline code.
    text = _INLINE_CODE_RE.sub(
        lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
        text,
    )

    # 3. Escape remaining HTML special chars.
    text = html.escape(text, quote=False)

    # 4. Inline markdown (operates on the escaped plaintext).
    text = _BOLD_RE.sub(
        lambda m: f"<b>{m.group(1) or m.group(2)}</b>",
        text,
    )
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_RE.sub(
        lambda m: f"<i>{m.group(1) or m.group(2)}</i>",
        text,
    )
    text = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )

    # 5. Restore stashed code blocks.
    for i, snippet in enumerate(placeholders):
        text = text.replace(f"\x00P{i}\x00", snippet)

    return text
