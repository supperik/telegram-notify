#!/usr/bin/env python3
"""
stop-hook — Claude Code Stop event handler for the send-completion-message skill.

Reads the Stop event JSON from stdin, composes a short Markdown summary
("✅ Claude Code finished in `<project>` at HH:MM" + last assistant snippet),
and shells out to scripts/send_completion_message.py.

Behaviour rules:
- **Never block Claude Code**: any error here is logged to stderr and we exit 0.
  A failed Telegram notification is not worth blocking the user's prompt cycle.
- **Per-session cooldown**: Stop fires after every assistant turn in interactive
  mode, which would spam Telegram. We track the last-send timestamp in a small
  cache file keyed by session_id and skip if it's within the cooldown window.
  Configurable via TELEGRAM_COMPLETION_COOLDOWN_SECONDS (default 600; 0 = off).
- **Opt-out**: set TELEGRAM_COMPLETION_DISABLED=1 to make the hook a no-op
  without removing it from settings.json.

Stdin schema (Claude Code Stop event):
    {
      "session_id": "...",
      "transcript_path": "/abs/path/transcript.jsonl",
      "cwd": "/abs/working/dir",
      "hook_event_name": "Stop",
      "stop_hook_active": false
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Bootstrap path to the plugin's lib/ — only needed for reconfigure_stdio_utf8.
# This file lives at <plugin_root>/skills/send-completion-message/hooks/stop-hook.py,
# so plugin root is parents[3].
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PLUGIN_ROOT / "lib"))

try:
    from telegram_client import reconfigure_stdio_utf8
    reconfigure_stdio_utf8()
except Exception:
    # If lib isn't reachable, the CLI invocation later will also fail — but
    # we still want to exit 0 cleanly rather than abort the harness.
    pass

CLI_SCRIPT = _PLUGIN_ROOT / "skills" / "send-completion-message" / "scripts" / "send_completion_message.py"

DEFAULT_COOLDOWN_SECONDS = 600
SUMMARY_MAX_CHARS = 300
CACHE_DIR_NAME = "telegram-notify-completion"


def _log(msg: str) -> None:
    """Diagnostics on stderr; harness logs them but doesn't surface to the user."""
    print(f"[telegram-notify/stop-hook] {msg}", file=sys.stderr)


def _read_event() -> dict:
    """Best-effort parse of the Stop event JSON. Returns {} on any failure."""
    try:
        raw = sys.stdin.read()
    except Exception as e:
        _log(f"could not read stdin: {e}")
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        _log(f"stdin was not valid JSON: {e}")
        return {}


def _cache_dir() -> Path:
    """Where we keep per-session 'last sent' timestamps."""
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    p = Path(base) / CACHE_DIR_NAME
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log(f"could not create cache dir {p}: {e}")
    return p


def _within_cooldown(session_id: str, cooldown_seconds: int) -> bool:
    """True if we already sent a completion for this session within the window."""
    if cooldown_seconds <= 0 or not session_id:
        return False
    marker = _cache_dir() / f"{session_id}.ts"
    if not marker.exists():
        return False
    try:
        last = float(marker.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    return (time.time() - last) < cooldown_seconds


def _record_sent(session_id: str) -> None:
    if not session_id:
        return
    marker = _cache_dir() / f"{session_id}.ts"
    try:
        marker.write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError as e:
        _log(f"could not write cooldown marker {marker}: {e}")


def _last_assistant_text(transcript_path: str) -> str | None:
    """Pull the text of the most recent assistant message from a Claude Code
    transcript (JSONL). Returns None if no assistant turn is found or the file
    can't be read.

    Transcript records vary across Claude Code versions, so we accept either:
      - {"type": "assistant", "message": {"content": [{"type":"text","text":"..."}, ...]}}
      - {"role": "assistant", "content": "..."}
      - or the same content shape with `content` already a plain string
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        _log(f"could not read transcript {p}: {e}")
        return None

    # Walk from the end for efficiency on long transcripts.
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue

        role = rec.get("role") or (rec.get("message") or {}).get("role") or rec.get("type")
        if role != "assistant":
            continue

        content = (rec.get("message") or {}).get("content")
        if content is None:
            content = rec.get("content")
        text = _content_to_text(content)
        if text:
            return text
    return None


def _content_to_text(content) -> str:
    """Flatten Claude Code's `content` field (string OR list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(c for c in chunks if c).strip()
    return ""


def _shorten(text: str, max_chars: int) -> str:
    """Trim to `max_chars`, breaking on a paragraph/word boundary if possible."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    # Prefer cutting at the last whitespace so we don't slice mid-word.
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _build_headline(cwd: str) -> str:
    project = Path(cwd).name if cwd else "Claude Code"
    when = datetime.now().strftime("%H:%M")
    return f"✅ Claude Code finished in `{project}` at {when}"


def _invoke_cli(headline: str, summary: str | None) -> int:
    """Run send_completion_message.py via subprocess. Returns its exit code."""
    if not CLI_SCRIPT.is_file():
        _log(f"CLI script not found at {CLI_SCRIPT} — skipping send")
        return 1
    cmd = [sys.executable, str(CLI_SCRIPT), "--text", headline, "--quiet"]
    if summary:
        cmd.extend(["--summary", summary])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        _log("send_completion_message.py timed out after 30s — skipping")
        return 1
    except OSError as e:
        _log(f"could not launch send_completion_message.py: {e}")
        return 1
    if result.returncode != 0:
        # Surface the CLI's own stderr to ours so the user can debug from logs.
        err = (result.stderr or "").strip()
        _log(f"send_completion_message.py exited {result.returncode}: {err}")
    return result.returncode


def main() -> int:
    if os.environ.get("TELEGRAM_COMPLETION_DISABLED", "").strip() in ("1", "true", "yes"):
        return 0  # opt-out — silent no-op

    event = _read_event()
    session_id = (event.get("session_id") or "").strip()
    transcript_path = event.get("transcript_path") or ""
    cwd = event.get("cwd") or os.getcwd()

    try:
        cooldown = int(os.environ.get("TELEGRAM_COMPLETION_COOLDOWN_SECONDS",
                                      str(DEFAULT_COOLDOWN_SECONDS)))
    except ValueError:
        cooldown = DEFAULT_COOLDOWN_SECONDS

    if _within_cooldown(session_id, cooldown):
        return 0

    headline = _build_headline(cwd)
    last_text = _last_assistant_text(transcript_path)
    summary = _shorten(last_text, SUMMARY_MAX_CHARS) if last_text else None

    rc = _invoke_cli(headline, summary)
    if rc == 0:
        _record_sent(session_id)
    # Always exit 0 — Stop hook never blocks Claude Code on notification failure.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Last-ditch defence: never let an unhandled exception block the harness.
        _log(f"unhandled exception: {e!r}")
        sys.exit(0)
