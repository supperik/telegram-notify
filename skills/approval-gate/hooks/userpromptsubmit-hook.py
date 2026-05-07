#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook that nudges the model toward picker-mode of
telegram-approval-gate when the user's prompt looks like a multi-option /
architectural decision.

Why it exists
-------------
Out of the box, Claude tends to ask clarifying or architectural questions
inline in chat. The user has asked for such decisions to come through
Telegram (via picker mode of this skill) instead. CLAUDE.md guidance covers
this — but guidance loaded once per session is easy for the model to forget
on long sessions or after compaction. This hook fires on EVERY prompt, runs a
narrow keyword check, and — only on matches — injects a short system reminder
into the model's context right before it formulates its response.

Trigger heuristic
-----------------
We match on phrases that strongly suggest a decision, NOT on every question.
False positives are expensive (wasted context, noise); false negatives are
cheap (CLAUDE.md catches the common case). When in doubt, leave a phrase out.

Output contract
---------------
On match: print JSON with hookSpecificOutput.additionalContext. The harness
appends this to the model's context for the next response.
On non-match or disabled: empty stdout, exit 0 (no-op).

Disabling
---------
Set TELEGRAM_GATE_REMINDER=0 in your settings.json env block to turn the
nudge off without removing the hook config.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Force UTF-8 on ALL three streams. stdin matters because the harness pipes
# the prompt as UTF-8 bytes, and on Windows the default codec is cp1252 — without
# reconfigure, Cyrillic text in the prompt becomes mojibake before the regex
# ever sees it, and triggers silently miss.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

REMINDER_ENABLED = os.environ.get("TELEGRAM_GATE_REMINDER", "1").strip().lower() in ("1", "true", "yes")
LANG = os.environ.get("TELEGRAM_GATE_LANG", "en").strip().lower()

# Keep these patterns narrow. We're targeting prompts that read like
# "compare and pick", "where should X go", or "is approach A better than B".
# Routine task prompts ("read this file", "fix the test", "rename foo to bar")
# should NOT trigger.
_TRIGGER_PATTERNS = [
    # English — decision / preference questions
    r"\bshould\s+i\b",
    r"\bwhich\s+(approach|pattern|option|architecture|design|solution|way|path|library|framework|stack)\b",
    r"\bhow\s+should\s+i\b",
    r"\bwhat\s+(do|would)\s+you\s+(recommend|suggest|prefer|think)\b",
    r"\b(option|approach)\s+(a|b|1|2)\s+(or|vs|versus)\s+(option|approach)?\s*(a|b|1|2)\b",
    r"\b(refactor|migrate)\b.*\b(or\s+leave|or\s+keep|or\s+rewrite)\b",
    r"\bwhere\s+should\s+(this|it|i\s+put|i\s+place)\b",
    r"\barchitect(ure|ural)\b.*\b(decision|choice|question)\b",

    # Russian — то же самое
    r"что\s+(лучше|выбрать|предпочесть|посоветуешь)",
    r"какой\s+(вариант|подход|паттерн|способ|стек|инструмент|фреймворк|библиотек)",
    r"какую\s+(структуру|архитектуру|схему|стратегию|реализацию)",
    r"какое\s+(решение|имя|название)",
    r"где\s+(разместить|лучше|расположить|хранить|положить)",
    r"стоит\s+ли\b",
    r"\bили\s+оставить\b",
    r"\bили\s+перенести\b",
    r"\bили\s+переписать\b",
    r"\bили\s+создать\s+новый\b",
    r"какой\s+из\s+вариантов",
    r"посовет(уй|уешь)",
    r"\bA\s+или\s+B\b",
    r"архитектурн(ое|ая|ый)\s+(решение|развилк|выбор)",
]
_TRIGGER_RE = re.compile("|".join(_TRIGGER_PATTERNS), re.IGNORECASE)

_REMINDER_BY_LANG = {
    "en": (
        "📡 SYSTEM REMINDER (telegram-approval-gate): The user's message looks like a "
        "multi-option / architectural decision. The user has asked for such questions to "
        "be routed through Telegram, not chat. Before answering inline, consider whether "
        "to invoke `telegram-approval-gate` in picker mode (`--option Label:value`, plus "
        "`:prompt_comment` on at least one option for free-text). See `~/.claude/CLAUDE.md` "
        "section \"Approval gate\" for criteria. If the question is trivial or answerable "
        "from context, you may ignore this reminder."
    ),
    "ru": (
        "📡 SYSTEM REMINDER (telegram-approval-gate): запрос пользователя похож на "
        "архитектурную / многовариантную развилку. Пользователь просил, чтобы такие "
        "вопросы шли через Telegram, а не через чат. Прежде чем отвечать встроенным "
        "вопросом — подумай: не стоит ли вызвать `telegram-approval-gate` в picker-режиме "
        "(`--option Label:value`, плюс `:prompt_comment` хотя бы на одном варианте для "
        "свободного текста)? Критерии — в `~/.claude/CLAUDE.md`, раздел «Approval gate». "
        "Если вопрос тривиальный или ответ уже есть в контексте — этот reminder можно "
        "проигнорировать."
    ),
}


def _load_event() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _extract_prompt(event: dict) -> str:
    """Pull the user's prompt out of the event payload.

    The exact field has varied across Claude Code versions, so we try the
    common keys. If nothing matches, we return an empty string (no trigger).
    """
    for key in ("prompt", "user_prompt", "message", "user_message", "text"):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            nested = v.get("text") or v.get("content") or v.get("prompt")
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""


def main() -> int:
    if not REMINDER_ENABLED:
        return 0
    event = _load_event()
    prompt = _extract_prompt(event)
    if not prompt:
        return 0
    if not _TRIGGER_RE.search(prompt):
        return 0

    reminder = _REMINDER_BY_LANG.get(LANG, _REMINDER_BY_LANG["en"])
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reminder,
        },
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
