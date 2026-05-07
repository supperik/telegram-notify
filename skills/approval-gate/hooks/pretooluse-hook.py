#!/usr/bin/env python3
"""
Claude Code PreToolUse hook that gates risky tool calls behind Telegram approval.

How Claude Code calls hooks
---------------------------
The harness pipes a JSON payload to this script's stdin (one event per call):

    {
      "tool_name":  "Bash",
      "tool_input": {"command": "rm -rf ./dist", "description": "..."}
    }

The hook decides what happens via JSON on stdout:

    {"decision": "approve", "reason": "..."}  → run the tool, skip the
                                                  harness's own permission
                                                  prompt (this is what we
                                                  want after Telegram
                                                  approves — otherwise the
                                                  user gets prompted twice).
    {"decision": "block",   "reason": "..."}  → block the tool; reason goes
                                                  to the model.
    no JSON, exit 0                           → defer to harness's normal
                                                  permission flow (i.e. the
                                                  user may still see a
                                                  console prompt).
    no JSON, exit 2 (+stderr)                 → block, equivalent to a JSON
                                                  block decision.

Important: ANY stdout from this script that isn't JSON will confuse the
harness, so the gate subprocess's stdout/stderr must be captured and
forwarded to *this* script's stderr — never inherited.

Wire-up
-------
Point a `PreToolUse` hook at this script in `.claude/settings.json`. See
`hooks/settings.example.json` in this skill for a working snippet.

What this hook gates
--------------------
- Bash commands matching destructive / deploy / push / publish patterns.
- Write/Edit operations targeting protected paths (`.env`, `secrets/**`, etc.).

Anything that doesn't match is passed through silently. Edit RISKY_BASH_PATTERNS
and PROTECTED_PATH_PATTERNS below to match your project's threat model.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Force UTF-8 on ALL three streams. stdin matters because the harness pipes
# the event JSON as UTF-8 bytes, and on Windows the default codec is cp1252 —
# without reconfigure, Cyrillic in commands or paths becomes mojibake before
# our classifier ever sees it.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

# Path to the approval script. Default assumes the hook lives in
# `<skill>/hooks/` and the script in `<skill>/scripts/`. Override via env if
# you've vendored the hook into another location.
DEFAULT_GATE = Path(__file__).resolve().parent.parent / "scripts" / "request_telegram_approval.py"
GATE = Path(os.environ.get("TELEGRAM_GATE_SCRIPT", str(DEFAULT_GATE)))

# How long to wait for an approval. CI runners typically have a step timeout —
# set this below it so the gate fails cleanly.
TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_GATE_TIMEOUT", "600"))

# UI language for the Telegram approval message. Default English; set
# TELEGRAM_GATE_LANG=ru in your settings.json env block to switch to Russian.
# Affects only what the human sees in Telegram — the regex patterns and JSON
# decision contract are unchanged.
LANG = os.environ.get("TELEGRAM_GATE_LANG", "en").strip().lower()

# Title and details strings, keyed by stable IDs the patterns reference below.
# Adding a new language: copy the "en" dict, translate values, add the lang key.
_TITLES_BY_LANG: dict[str, dict[str, str]] = {
    "en": {
        "rm_recursive":          "Recursive deletion via rm -rf",
        "git_force_push":        "Force-push to git remote (rewrites remote history)",
        "git_push_protected":    "Push to a protected branch (main / master / prod)",
        "git_reset_hard":        "Hard reset (discards uncommitted changes)",
        "npm_deploy":            "Deploy via npm script",
        "npm_publish":           "Publish package to the npm registry",
        "terraform_change":      "Terraform apply / destroy (mutates real infrastructure)",
        "kubectl_mutation":      "Mutating kubectl call (apply / delete / drain)",
        "docker_push_prune":     "Docker push or system prune",
        "net_pipe_shell":        "Pipe a network response straight into a shell — RCE-shaped",
        "remove_item_recursive": "Recursive PowerShell Remove-Item",
        "remove_item_force":     "Forced PowerShell Remove-Item",
        "clear_content":         "Clear-Content — truncates a file's contents",
        "stop_process_force":    "Force-stop a process",
        "exec_policy":           "Change PowerShell execution policy",
        "write_protected":       "Write to a protected file",
        "edit_protected":        "Edit a protected file",
    },
    "ru": {
        "rm_recursive":          "Рекурсивное удаление через rm -rf",
        "git_force_push":        "Force-push в git (перезапишет удалённую историю)",
        "git_push_protected":    "Push в защищённую ветку (main / master / prod)",
        "git_reset_hard":        "Hard reset (потеря незакоммиченных изменений)",
        "npm_deploy":            "Деплой через npm-скрипт",
        "npm_publish":           "Публикация пакета в npm-реестр",
        "terraform_change":      "Terraform apply / destroy (изменение боевой инфраструктуры)",
        "kubectl_mutation":      "Изменяющий вызов kubectl (apply / delete / drain)",
        "docker_push_prune":     "Docker push или system prune",
        "net_pipe_shell":        "Пайп сетевого ответа прямо в shell — потенциальный RCE",
        "remove_item_recursive": "Рекурсивное Remove-Item в PowerShell",
        "remove_item_force":     "Принудительное Remove-Item в PowerShell",
        "clear_content":         "Clear-Content — обнулит содержимое файла",
        "stop_process_force":    "Принудительная остановка процесса",
        "exec_policy":           "Смена PowerShell execution policy",
        "write_protected":       "Запись в защищённый файл",
        "edit_protected":        "Правка защищённого файла",
    },
}
TITLES = _TITLES_BY_LANG.get(LANG, _TITLES_BY_LANG["en"])

_DETAILS_BY_LANG: dict[str, dict[str, str]] = {
    "en": {
        "shell_run":      "Claude wants to run a {shell} command:",
        "protected_path": "Path matches the protected-paths list — confirmation required:",
    },
    "ru": {
        "shell_run":      "Claude собирается выполнить команду в {shell}:",
        "protected_path": "Путь попадает под список защищённых файлов — нужно подтверждение:",
    },
}
DETAILS_TPL = _DETAILS_BY_LANG.get(LANG, _DETAILS_BY_LANG["en"])

# Shell commands that should require approval — checked for both `Bash`
# and `PowerShell` tool calls. Each entry is (regex, title_key, risk level).
# `title_key` looks up TITLES[lang]; that way the regex stays language-neutral
# and the human-facing string lives in _TITLES_BY_LANG above.
# IGNORECASE so PowerShell cmdlet aliasing doesn't let "REMOVE-item" slip past.
RISKY_SHELL_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # bash-style + PowerShell aliases
    (re.compile(r"\brm\s+-[rfRF]+\b", re.IGNORECASE),                    "rm_recursive",          "high"),
    (re.compile(r"\bgit\s+push\s+(--force|-f)\b", re.IGNORECASE),        "git_force_push",        "high"),
    (re.compile(r"\bgit\s+push\b.*\b(main|master|prod)\b", re.IGNORECASE),"git_push_protected",   "high"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),             "git_reset_hard",        "medium"),
    (re.compile(r"\bnpm\s+(run\s+)?deploy", re.IGNORECASE),              "npm_deploy",            "critical"),
    (re.compile(r"\bnpm\s+publish\b", re.IGNORECASE),                    "npm_publish",           "high"),
    (re.compile(r"\bterraform\s+(apply|destroy)\b", re.IGNORECASE),      "terraform_change",      "critical"),
    (re.compile(r"\bkubectl\s+(apply|delete|drain)\b", re.IGNORECASE),   "kubectl_mutation",      "high"),
    (re.compile(r"\bdocker\s+(push|system\s+prune)\b", re.IGNORECASE),   "docker_push_prune",     "medium"),
    (re.compile(r"\b(curl|wget|iwr|Invoke-WebRequest)\b.*\|\s*(sh|bash|iex|Invoke-Expression)\b",
                re.IGNORECASE),                                          "net_pipe_shell",        "critical"),

    # PowerShell-native verbs (cmdlet form — alias `rm` is already covered above).
    # Note: a `\b` before `-Flag` doesn't work — both space and hyphen are
    # non-word characters, so there's no word boundary between them. Use
    # (?<!\w) instead, which asserts "not preceded by a word char".
    (re.compile(r"\bRemove-Item\b[^|;]*(?<!\w)-Recurse\b", re.IGNORECASE), "remove_item_recursive", "high"),
    (re.compile(r"\bRemove-Item\b[^|;]*(?<!\w)-Force\b",   re.IGNORECASE), "remove_item_force",     "medium"),
    (re.compile(r"\bClear-Content\b", re.IGNORECASE),                     "clear_content",         "medium"),
    (re.compile(r"\bStop-Process\b[^|;]*(?<!\w)-Force\b",  re.IGNORECASE), "stop_process_force",    "medium"),
    (re.compile(r"\bSet-ExecutionPolicy\b", re.IGNORECASE),               "exec_policy",           "high"),
]

# Backwards-compatible alias for anything that imported the old name.
RISKY_BASH_PATTERNS = RISKY_SHELL_PATTERNS

# Globs (fnmatch-style) that should require approval before write/edit.
# Matched against the path reported by the tool input.
PROTECTED_PATH_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "secrets/**",
    "**/secrets/**",
    ".github/workflows/**",
    "infra/**",
    "terraform/**",
]


def _load_event() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Don't block on a malformed event — but log it loudly so it's noticed.
        print("[telegram-approval-gate hook] non-JSON event, allowing.", file=sys.stderr)
        return {}


def _classify(event: dict) -> tuple[str, str, str, str | None] | None:
    """Return (title, details, risk, command_or_None) if approval is required.

    The fourth element is the literal command/path to render in the Telegram
    message as a Markdown code block (separate from `details`, which is short
    human context). For shell tools it's the full command. For protected-path
    edits it's the file path.
    """
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}

    if tool in ("Bash", "PowerShell"):
        cmd = (tool_input.get("command") or "").strip()
        if not cmd:
            return None
        shell_label = "bash" if tool == "Bash" else "PowerShell"
        for pattern, title_key, risk in RISKY_SHELL_PATTERNS:
            if pattern.search(cmd):
                title = TITLES.get(title_key, title_key)
                details = DETAILS_TPL["shell_run"].format(shell=shell_label)
                return (title, details, risk, cmd)
        return None

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if path and _path_matches_protected(path):
            title_key = "write_protected" if tool == "Write" else "edit_protected"
            title = TITLES.get(title_key, title_key)
            details = DETAILS_TPL["protected_path"]
            return (title, details, "high", path)
        return None

    # Other tools — pass through. Add cases here as your threat model grows.
    return None


def _path_matches_protected(path: str) -> bool:
    from fnmatch import fnmatch
    norm = path.replace("\\", "/")
    return any(fnmatch(norm, pat) for pat in PROTECTED_PATH_PATTERNS)


def _run_gate(title: str, details: str, risk: str, command: str | None) -> int:
    """Invoke the gate script. Captures its output so it doesn't pollute our stdout
    (which the harness reads as our decision JSON). Anything the gate said is
    forwarded to *our* stderr so the user still sees it."""
    if not GATE.exists():
        # Without the gate script we can't enforce — fail closed and tell the user.
        print(f"[telegram-approval-gate hook] gate script not found at {GATE}",
              file=sys.stderr)
        return 1
    cmd = [
        sys.executable, str(GATE),
        "--title", title,
        "--details", details,
        "--risk", risk,
        "--timeout-seconds", str(TIMEOUT_SECONDS),
    ]
    if command:
        cmd += ["--command", command]
    try:
        # capture_output=True keeps the gate's stdout out of our stdout. Both
        # streams are forwarded to our stderr so the user can see what happened.
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            sys.stderr.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stderr.write("\n")
        if result.stderr:
            sys.stderr.write(result.stderr)
            if not result.stderr.endswith("\n"):
                sys.stderr.write("\n")
        return result.returncode
    except FileNotFoundError as e:
        print(f"[telegram-approval-gate hook] cannot run gate: {e}", file=sys.stderr)
        return 1


def _emit(decision: str, reason: str) -> None:
    """Print the harness's decision-JSON to stdout. Single source of truth for
    what we tell Claude Code."""
    print(json.dumps({"decision": decision, "reason": reason}))


def main() -> int:
    event = _load_event()
    classified = _classify(event)

    if classified is None:
        # Not risky. Default: defer to the harness's normal permission flow
        # (silent exit 0). If the user wants Telegram to be the SOLE prompt
        # mechanism even for safe-looking commands, set the env var below to
        # "1" — the hook will then auto-approve safe commands so the harness
        # doesn't show its own console prompt either. This is opt-in because
        # it removes the harness's own safety net for anything our patterns
        # don't match.
        if os.environ.get("TELEGRAM_GATE_AUTOAPPROVE_SAFE", "").lower() in ("1", "true", "yes"):
            _emit("approve",
                  "telegram-approval-gate: command did not match any risky pattern; "
                  "auto-approved per TELEGRAM_GATE_AUTOAPPROVE_SAFE=1.")
        return 0

    title, details, risk, command = classified
    rc = _run_gate(title, details, risk, command)

    if rc == 0:
        # Telegram-approved → tell the harness to proceed AND skip its own
        # permission prompt (that's what 'decision: approve' on stdout does;
        # without this you'd see both a Telegram message AND a console prompt).
        _emit("approve",
              f"telegram-approval-gate: approved via Telegram for action "
              f"\"{title.rstrip('?')}\" (risk={risk}).")
        return 0

    # Non-zero → block. Use the same JSON channel for symmetry; the harness
    # treats it identically to exit 2 + stderr but the message is cleaner.
    reasons = {
        1: "rejected by user via Telegram",
        2: "no decision within the approval timeout (deny by default)",
        3: "approval gate misconfigured (missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)",
        4: "Telegram API or network error while requesting approval",
    }
    reason = reasons.get(rc, f"approval gate exited {rc}")
    _emit("block",
          f"Tool call blocked by telegram-approval-gate: {reason}. "
          f"Action was: \"{title.rstrip('?')}\" (risk={risk}). "
          f"Do not retry without checking with the user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
