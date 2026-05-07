"""
telegram_client — shared Telegram Bot API helpers for the telegram-notify plugin.

Stdlib-only: no `requests`, no pip dependencies. Reused by:
- skills/approval-gate/scripts/request_telegram_approval.py
- skills/send-completion-message/scripts/send_completion_message.py
- skills/send-completion-message/hooks/stop-hook.py

Public surface:
    DEFAULT_API_BASE
    EXIT_OK, EXIT_CONFIG_ERROR, EXIT_API_ERROR
    reconfigure_stdio_utf8()
    escape_md(text)
    format_code_block(content, max_chars=...)
    api_call(token, method, params=None, http_timeout=30, api_base=...)
    load_telegram_env() -> (token, chat_id, api_base)
    send_text_message(token, chat_id, text, api_base, parse_mode="Markdown", ...)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_API_BASE = "https://api.telegram.org"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 3
EXIT_API_ERROR = 4

CODE_BLOCK_MAX_DEFAULT = 3500


class TelegramConfigError(Exception):
    """Raised when required Telegram env vars are missing or invalid."""


def reconfigure_stdio_utf8() -> None:
    """Force UTF-8 on stdout/stderr so emoji and dashes survive on Windows cp1252.

    Idempotent and safe to call multiple times. Older Python (<3.7) silently no-ops.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def escape_md(text) -> str:
    """Conservative escaping for Telegram legacy Markdown (parse_mode='Markdown').

    Legacy Markdown is more forgiving than MarkdownV2; we only neutralize the
    four characters that can break a message: backslash, backtick, asterisk,
    underscore.
    """
    if text is None:
        return ""
    out = str(text)
    for ch, repl in (("\\", "\\\\"), ("`", "'"), ("*", "·"), ("_", " ")):
        out = out.replace(ch, repl)
    return out


def format_code_block(content: str, max_chars: int = CODE_BLOCK_MAX_DEFAULT) -> str:
    """Wrap content in a Markdown triple-backtick block.

    Inner triple-backticks are neutralized so the block can't close early.
    Long content is truncated with a visible "(truncated; full length …)" note.
    """
    if not content:
        return ""
    safe = content.replace("```", "` ` `")
    if len(safe) > max_chars:
        truncated = safe[:max_chars]
        safe = truncated + f"\n... (truncated; full length {len(content)} chars)"
    return f"```\n{safe}\n```"


def api_call(token: str, method: str, params: dict | None = None,
             http_timeout: int = 30, api_base: str = DEFAULT_API_BASE) -> dict:
    """POST to the Telegram Bot API. Returns parsed JSON. Raises RuntimeError on failure.

    The `token` never appears in error messages — only the method name does.
    """
    url = f"{api_base}/bot{token}/{method}"
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err_payload = json.loads(raw)
            description = err_payload.get("description", raw)
        except json.JSONDecodeError:
            description = raw
        raise RuntimeError(f"Telegram API HTTP {e.code} on {method}: {description}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram API network error on {method}: {e.reason}") from None
    except (TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Telegram API protocol error on {method}: {e}") from None

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected {method}: {payload.get('description', payload)}")
    return payload


def load_telegram_env() -> tuple[str, str, str]:
    """Load TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_API_BASE from env.

    Returns (token, chat_id, api_base). Raises TelegramConfigError if either of
    the required vars is missing/empty. api_base falls back to DEFAULT_API_BASE.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    api_base = os.environ.get("TELEGRAM_API_BASE", DEFAULT_API_BASE).rstrip("/")
    if not token:
        raise TelegramConfigError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise TelegramConfigError("TELEGRAM_CHAT_ID is not set")
    return token, chat_id, api_base


def send_text_message(token: str, chat_id: str, text: str,
                      api_base: str = DEFAULT_API_BASE,
                      parse_mode: str = "Markdown",
                      disable_web_page_preview: bool = True,
                      disable_notification: bool = False) -> dict:
    """Send a plain text message to a chat. Convenience wrapper around api_call.

    Caller is responsible for any Markdown escaping (use `escape_md`). The
    returned dict is the full Telegram API payload (`ok`, `result`, etc.).
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
        "disable_notification": disable_notification,
    }
    return api_call(token, "sendMessage", payload, api_base=api_base)
