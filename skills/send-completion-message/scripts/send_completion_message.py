#!/usr/bin/env python3
"""
send_completion_message — fire-and-forget Telegram notification that Claude
Code finished work in the current project.

Used in two ways:
  1. As the body of `<plugin>/skills/send-completion-message/hooks/stop-hook.py`
     (the Stop hook composes a short message and shells out to this script).
  2. As a standalone CLI for the model to invoke explicitly when it wants to
     ping the user mid-session ("step 1 done, starting tests…").

Required environment:
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat ID (user, group, or channel) to send to

Optional environment:
    TELEGRAM_API_BASE    override API host (default: https://api.telegram.org)

Exit codes:
    0 — sent (or `--quiet` no-op)
    3 — configuration error (missing required env)
    4 — Telegram API or network error

Stdlib-only; relies on the plugin's shared lib/telegram_client.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap: add the plugin's shared lib/ to sys.path. This script lives at
# <plugin_root>/skills/send-completion-message/scripts/send_completion_message.py,
# so the plugin root is parents[3].
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))

from telegram_client import (  # noqa: E402  -- sys.path adjusted above
    EXIT_OK,
    EXIT_CONFIG_ERROR,
    EXIT_API_ERROR,
    TelegramConfigError,
    escape_md,
    load_telegram_env,
    reconfigure_stdio_utf8,
    send_text_message,
)

reconfigure_stdio_utf8()

DEFAULT_TEXT = "Claude Code finished"


def build_message(text: str, summary: str | None) -> str:
    """Compose the final Markdown payload.

    `text` is the headline (rendered bold). `summary` is optional free-form
    Markdown that goes underneath, separated by a blank line.

    Both arguments are escaped — callers don't need to pre-escape, and
    accidentally passing un-sanitized assistant text won't blow up the
    message with stray asterisks.
    """
    parts = [f"*{escape_md(text)}*"]
    if summary:
        parts.extend(["", escape_md(summary)])
    return "\n".join(parts)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="send_completion_message",
        description="Send a Telegram notification that Claude Code finished work.",
    )
    p.add_argument("--text", default=DEFAULT_TEXT,
                   help=f"Headline shown bold. Default: {DEFAULT_TEXT!r}.")
    p.add_argument("--summary", default=None,
                   help="Optional body — free-form Markdown rendered under the headline.")
    p.add_argument("--silent", action="store_true",
                   help="Send with disable_notification=True (no sound/vibration on the user's phone).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress informational stdout (errors still go to stderr).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        token, chat_id, api_base = load_telegram_env()
    except TelegramConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    text = build_message(args.text, args.summary)

    try:
        send_text_message(
            token=token,
            chat_id=chat_id,
            text=text,
            api_base=api_base,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            disable_notification=args.silent,
        )
    except RuntimeError as e:
        print(f"ERROR: failed to send completion message: {e}", file=sys.stderr)
        return EXIT_API_ERROR

    if not args.quiet:
        print(f"[send-completion-message] sent ({len(text)} chars to chat {chat_id})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
