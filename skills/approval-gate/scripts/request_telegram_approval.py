#!/usr/bin/env python3
"""
request_telegram_approval — human-in-the-loop approval gate over Telegram.

Sends a Markdown approval request with inline Approve/Reject buttons to a
Telegram chat, then long-polls for a decision matching the request's unique ID.

Exit codes (deny-by-default):
    0 — approved
    1 — rejected by user
    2 — timed out (treated as rejection by callers)
    3 — configuration error (missing/invalid env vars or args)
    4 — Telegram API or network error

Required environment:
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat ID (user, group, or channel) to send the request to

Optional environment:
    TELEGRAM_API_BASE    override API host (default: https://api.telegram.org)
    TELEGRAM_APPROVER_IDS
                         comma-separated numeric Telegram user IDs allowed to
                         approve/reject. Decisions from any other user are
                         silently ignored. If unset:
                           - DM chats (positive TELEGRAM_CHAT_ID) default to
                             [TELEGRAM_CHAT_ID] — the chat owner only.
                           - Group/channel chats (negative TELEGRAM_CHAT_ID)
                             require this variable explicitly; otherwise the
                             gate refuses to start (exit 3).

The script uses only the Python standard library — no `requests` dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

# Force UTF-8 on stdio so emoji and dashes in messages don't blow up under
# Windows' default cp1252 console. .reconfigure exists on Python 3.7+.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

# Exit codes — kept as module constants so callers (hooks, wrappers) can import.
EXIT_APPROVED = 0
EXIT_REJECTED = 1
EXIT_TIMEOUT = 2
EXIT_CONFIG_ERROR = 3
EXIT_API_ERROR = 4

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_COMMENT_TIMEOUT_SECONDS = 90
LONG_POLL_MAX = 25  # Telegram caps long-poll at 50s; 25s keeps responsiveness

# Telegram caps a text message at 4096 chars. We reserve room for the title,
# details, risk, request_id, instructions, and Markdown overhead — leaves
# this much for the optional --command code block.
_CODE_BLOCK_MAX = 3500

# Same Telegram 4096-char cap applies to --details. Truncate explicitly with
# a visible note rather than letting sendMessage fail with HTTP 400.
_DETAILS_MAX = 3500

# Auto-inject default for picker mode: every picker call must offer a way to
# type a free-form answer. If the caller hasn't included one, we append this
# option automatically (suppressible via --no-custom-option).
_AUTO_CUSTOM_VALUE = "custom"
_CUSTOM_LABEL = "Своё предложение"

# Russian display labels for the four --risk levels. The CLI value stays a
# latin identifier (it's what callers pass); only what the user reads in the
# Telegram message is localized.
_RISK_LABELS = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
    "critical": "критический",
}

# Recommended-option marker for picker mode. Every picker must mark at least
# one option as the model's recommended choice (enforced in main()); the
# chosen button is wrapped with these so it stands out on the phone.
_RECOMMEND_PREFIX = "⭐ "
_RECOMMEND_SUFFIX = " (рекомендую)"

# Telegram's callback_data limit is 64 bytes after UTF-8 encoding. We embed
# "opt:{value}:{request_id}", so the value field has roughly 47 ASCII bytes of
# headroom. Keep option values short and ASCII-ish — the label is what users
# read, the value is just an identifier.
_OPTION_VALUE_MAX = 40
_OPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------


def _api_call(token: str, method: str, params: dict | None = None,
              http_timeout: int = 30, api_base: str = DEFAULT_API_BASE) -> dict:
    """POST to the Telegram Bot API. Returns parsed JSON. Raises RuntimeError on failure.

    Note: `token` never appears in error messages — only the method name does.
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


def _escape_md(text: str) -> str:
    """Conservative escaping for Telegram Markdown (legacy parse_mode='Markdown').

    We use legacy Markdown rather than MarkdownV2 because it has fewer reserved
    characters and is forgiving with arbitrary user-supplied detail strings.
    Just neutralize the four characters that can break a message.
    """
    if text is None:
        return ""
    out = str(text)
    for ch, repl in (("\\", "\\\\"), ("`", "'"), ("*", "·"), ("_", " ")):
        out = out.replace(ch, repl)
    return out


def _truncate_details(text: str) -> str:
    """Cap --details at `_DETAILS_MAX` so the assembled message stays under
    Telegram's 4096-char limit. Appends a visible note so the user knows
    there's more than what's shown.
    """
    if not text or len(text) <= _DETAILS_MAX:
        return text or ""
    return text[:_DETAILS_MAX] + f"\n… (truncated; full length {len(text)} chars)"


def _risk_label(risk: str) -> str:
    """Localized display label for a --risk value (falls back to the raw value)."""
    return _RISK_LABELS.get(risk, risk)


def _button_text(opt: dict) -> str:
    """Telegram button caption for a picker option, with the recommended marker."""
    if opt.get("recommended"):
        return f"{_RECOMMEND_PREFIX}{opt['label']}{_RECOMMEND_SUFFIX}"
    return opt["label"]


def _format_code_block(content: str) -> str:
    """Wrap content in a Markdown triple-backtick block.

    `language` is intentionally omitted — Telegram clients auto-detect for
    common shells/scripts, and legacy Markdown doesn't support a language hint
    anyway (that's MarkdownV2). Inner triple-backticks are neutralized so the
    block can't close early on the user; very long content is truncated with a
    visible note so the user knows there's more.
    """
    if not content:
        return ""
    safe = content.replace("```", "` ` `")
    if len(safe) > _CODE_BLOCK_MAX:
        truncated = safe[:_CODE_BLOCK_MAX]
        safe = truncated + f"\n... (truncated; full length {len(content)} chars)"
    return f"```\n{safe}\n```"


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------


def send_approval_message(token: str, chat_id: str, title: str, details: str,
                          risk: str, request_id: str, api_base: str,
                          command: str | None = None) -> dict:
    """Send the approval request with inline Approve/Reject buttons.

    If `command` is non-empty it's rendered as a Markdown code block under
    the details — this is where the actual command/payload to be approved
    lives, untruncated up to ~3500 chars.
    """
    parts = [
        "🔐 *Требуется подтверждение*",
        "",
        f"*Действие:* {_escape_md(title)}",
    ]
    if details:
        parts.append(f"*Детали:* {_escape_md(_truncate_details(details))}")
    if command:
        parts.append(_format_code_block(command))
    parts.extend([
        f"*Риск:* {_escape_md(_risk_label(risk))}",
        f"*ID запроса:* `{request_id}`",
        "",
        "Нажми кнопку ниже или ответь сообщением:",
        f"`APPROVE {request_id}`",
        f"`REJECT {request_id} <причина>`",
    ])
    payload = {
        "chat_id": chat_id,
        "text": "\n".join(parts),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Одобрить", "callback_data": f"approve:{request_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{request_id}"},
            ]]
        },
    }
    return _api_call(token, "sendMessage", payload, api_base=api_base)


def _baseline_offset(token: str, api_base: str) -> int | None:
    """Get the current update_id+1 so we ignore messages from before our request.

    Without this, an old "APPROVE xyz" sitting in the queue could match a fresh
    request if the IDs collided. Request IDs are UUID-based so collisions are
    nil, but draining is still cleaner and avoids surprise behavior when the
    queue is full of stale callback_query updates from prior runs.
    """
    try:
        resp = _api_call(token, "getUpdates",
                         {"offset": -1, "limit": 1, "timeout": 0},
                         api_base=api_base)
    except RuntimeError:
        return None
    results = resp.get("result", [])
    if not results:
        return None
    return results[-1]["update_id"] + 1


def wait_for_decision(token: str, request_id: str, timeout_seconds: int,
                      sent_at: float, api_base: str,
                      approver_ids: set[int]
                      ) -> tuple[str, str, str | None] | None:
    """Long-poll Telegram until a matching decision arrives or we run out of time.

    Returns ('approve'|'reject', user_label, reason_or_None) or None on timeout.
    Decisions are accepted only from `approver_ids` (Telegram numeric user IDs);
    everything else is logged and ignored. (Stale replies from prior runs are
    already excluded by the getUpdates offset baseline plus the per-request
    UUID, so no timestamp comparison against the local clock is needed —
    comparing Telegram's server time to a drifted local clock dropped valid
    decisions.)
    """
    deadline = time.time() + timeout_seconds
    offset = _baseline_offset(token, api_base)

    while time.time() < deadline:
        remaining = deadline - time.time()
        long_poll = max(1, min(LONG_POLL_MAX, int(remaining)))
        params: dict = {"timeout": long_poll, "limit": 50,
                        "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = _api_call(token, "getUpdates", params,
                             http_timeout=long_poll + 10, api_base=api_base)
        except RuntimeError as e:
            # Transient network blip — back off briefly and try again rather
            # than failing the whole gate. We still respect the overall deadline.
            print(f"[telegram-approval-gate] transient API error: {e}", file=sys.stderr)
            time.sleep(2)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            decision = _decision_from_update(update, request_id, sent_at,
                                             token, api_base, approver_ids)
            if decision is not None:
                return decision

    return None


def _decision_from_update(update: dict, request_id: str, sent_at: float,
                          token: str, api_base: str,
                          approver_ids: set[int]
                          ) -> tuple[str, str, str | None] | None:
    # Inline keyboard button press.
    cq = update.get("callback_query")
    if cq is not None:
        data = (cq.get("data") or "").strip()
        from_user = cq.get("from") or {}
        if not _is_authorized(from_user, approver_ids):
            # Don't reveal anything useful to a snooping user. Ack the spinner
            # with a generic refusal so Telegram's UI doesn't hang, and log
            # the attempt for the operator.
            _log_unauthorized(from_user, "callback", data)
            _ack_callback(token, cq["id"], "Нет доступа", api_base)
            return None
        user = _user_label(from_user)
        if data == f"approve:{request_id}":
            _ack_callback(token, cq["id"], "Одобрено ✅", api_base)
            return ("approve", user, None)
        if data == f"reject:{request_id}":
            _ack_callback(token, cq["id"], "Отклонено ❌", api_base)
            return ("reject", user, None)
        # Buttons for a different request — politely no-op so Telegram stops the spinner.
        _ack_callback(token, cq["id"], "Устаревший запрос — игнорирую", api_base)
        return None

    # Plain text reply.
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return None
    text = (msg.get("text") or "").strip()
    if not text:
        return None
    parts = text.split(None, 2)
    if len(parts) < 2:
        return None
    verb = parts[0].upper()
    if parts[1] != request_id:
        return None  # right verb, wrong request — ignore
    from_user = msg.get("from") or {}
    if not _is_authorized(from_user, approver_ids):
        # Stay silent on the wire — no Telegram reply, no acknowledgement.
        # An attacker who is in the chat shouldn't get any signal that
        # they hit a real request. Log to stderr for the operator.
        _log_unauthorized(from_user, "text", text[:80])
        return None
    user = _user_label(from_user)
    if verb == "APPROVE":
        return ("approve", user, None)
    if verb == "REJECT":
        reason = parts[2].strip() if len(parts) > 2 else None
        return ("reject", user, reason)
    return None


def _is_authorized(from_user: dict, approver_ids: set[int]) -> bool:
    user_id = from_user.get("id") if from_user else None
    return isinstance(user_id, int) and user_id in approver_ids


def _log_unauthorized(from_user: dict, channel: str, payload: str) -> None:
    uid = (from_user or {}).get("id")
    label = _user_label(from_user)
    print(f"[telegram-approval-gate] ignored {channel} decision from "
          f"unauthorized user_id={uid} ({label}): {payload!r}",
          file=sys.stderr)


def _ack_callback(token: str, callback_id: str, text: str, api_base: str) -> None:
    """Acknowledge an inline button press so Telegram dismisses the loading spinner.

    Failures here are non-fatal — the decision has already been made.
    """
    try:
        _api_call(token, "answerCallbackQuery",
                  {"callback_query_id": callback_id, "text": text},
                  api_base=api_base)
    except RuntimeError:
        pass


def _user_label(user: dict | None) -> str:
    if not user:
        return "unknown"
    return (user.get("username")
            or user.get("first_name")
            or str(user.get("id") or "unknown"))


def _send_followup(token: str, chat_id: str, request_id: str, text: str,
                   api_base: str) -> None:
    """Best-effort confirmation back to the chat. Failure is non-fatal."""
    try:
        _api_call(token, "sendMessage",
                  {"chat_id": chat_id,
                   "text": f"`{request_id}` — {text}",
                   "parse_mode": "Markdown"},
                  api_base=api_base)
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Multi-option mode (--option / --comment-timeout-seconds)
# ---------------------------------------------------------------------------


def parse_option(spec: str) -> dict:
    """Parse a single --option string.

    Format:
        "label:value"                  → simple button
        "label:value:prompt_comment"   → after click, wait for a follow-up text

    `value` is what gets returned to the caller and embedded in callback_data,
    so it must be short and ASCII-ish. `label` is the Telegram button text and
    can be any unicode string (incl. emoji, Cyrillic, etc.).
    """
    parts = spec.split(":", 2)
    if len(parts) < 2:
        raise ValueError(
            f"--option must be 'label:value' or 'label:value:prompt_comment', got: {spec!r}"
        )
    label = parts[0].strip()
    value = parts[1].strip()
    flag = parts[2].strip() if len(parts) == 3 else ""

    if not label:
        raise ValueError(f"--option label cannot be empty: {spec!r}")
    if not value:
        raise ValueError(f"--option value cannot be empty: {spec!r}")
    if len(value) > _OPTION_VALUE_MAX:
        raise ValueError(
            f"--option value '{value}' is {len(value)} chars; "
            f"max {_OPTION_VALUE_MAX} (keeps callback_data under Telegram's 64-byte cap)"
        )
    if not _OPTION_VALUE_RE.match(value):
        raise ValueError(
            f"--option value '{value}' must match [A-Za-z0-9_-]+ "
            f"(it's an identifier, not display text — use 'label' for that)"
        )
    if flag and flag != "prompt_comment":
        raise ValueError(
            f"--option flag must be 'prompt_comment' or omitted, got: {flag!r}"
        )
    return {"label": label, "value": value,
            "prompt_comment": flag == "prompt_comment", "recommended": False}


def send_options_message(token: str, chat_id: str, title: str, details: str,
                         risk: str, request_id: str, options: list[dict],
                         api_base: str, command: str | None = None) -> dict:
    """Send a request whose inline keyboard is built from `options`.

    `command`, if provided, is rendered as a Markdown code block — useful
    when the picker is wrapping a shell command or a code snippet whose
    full text the user needs to see before deciding.
    """
    parts = [
        "🔐 *Требуется решение*",
        "",
        f"*Действие:* {_escape_md(title)}",
    ]
    if details:
        parts.append(f"*Детали:* {_escape_md(_truncate_details(details))}")
    if command:
        parts.append(_format_code_block(command))
    parts.extend([
        f"*Риск:* {_escape_md(_risk_label(risk))}",
        f"*ID запроса:* `{request_id}`",
        "",
        "Нажми одну из кнопок ниже.",
    ])
    if any(opt.get("recommended") for opt in options):
        parts.append("⭐ — рекомендуемый вариант")
    inline_keyboard = [
        [{"text": _button_text(opt),
          "callback_data": f"opt:{opt['value']}:{request_id}"}]
        for opt in options
    ]
    payload = {
        "chat_id": chat_id,
        "text": "\n".join(parts),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": inline_keyboard},
    }
    return _api_call(token, "sendMessage", payload, api_base=api_base)


def wait_for_options_decision(token: str, chat_id: str, request_id: str,
                              timeout_seconds: int, sent_at: float,
                              api_base: str, approver_ids: set[int],
                              options: list[dict],
                              comment_timeout_seconds: int
                              ) -> dict | None:
    """Long-poll for a click on one of the option buttons.

    On match, if the chosen option has `prompt_comment=True`, follow up with
    a text-reply wait so the approver can attach a comment. Returns
    {"value": ..., "user": ..., "comment": str|None} or None on timeout.
    """
    options_by_value = {opt["value"]: opt for opt in options}
    deadline = time.time() + timeout_seconds
    offset = _baseline_offset(token, api_base)

    while time.time() < deadline:
        remaining = deadline - time.time()
        long_poll = max(1, min(LONG_POLL_MAX, int(remaining)))
        params: dict = {"timeout": long_poll, "limit": 50,
                        "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = _api_call(token, "getUpdates", params,
                             http_timeout=long_poll + 10, api_base=api_base)
        except RuntimeError as e:
            print(f"[telegram-approval-gate] transient API error: {e}", file=sys.stderr)
            time.sleep(2)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq is None:
                continue

            data = (cq.get("data") or "").strip()
            from_user = cq.get("from") or {}

            if not _is_authorized(from_user, approver_ids):
                _log_unauthorized(from_user, "callback", data)
                _ack_callback(token, cq["id"], "Нет доступа", api_base)
                continue

            if not data.startswith("opt:"):
                _ack_callback(token, cq["id"], "Устаревший запрос — игнорирую", api_base)
                continue
            try:
                _, value, rid = data.split(":", 2)
            except ValueError:
                _ack_callback(token, cq["id"], "Некорректный запрос", api_base)
                continue
            if rid != request_id:
                _ack_callback(token, cq["id"], "Устаревший запрос — игнорирую", api_base)
                continue

            opt = options_by_value.get(value)
            if opt is None:
                _ack_callback(token, cq["id"], "Неизвестный вариант", api_base)
                continue

            user_label = _user_label(from_user)
            user_id = from_user.get("id")

            if opt["prompt_comment"]:
                _ack_callback(token, cq["id"],
                              f"Выбрано: {opt['label']} — пришли комментарий",
                              api_base)
                # Tell the chat what to do next so the user isn't guessing.
                try:
                    _api_call(token, "sendMessage", {
                        "chat_id": chat_id,
                        "text": (f"`{request_id}`: выбрано *{_escape_md(opt['label'])}*.\n"
                                 f"Ответь комментарием следующим сообщением "
                                 f"(или подожди {comment_timeout_seconds} c, чтобы пропустить)."),
                        "parse_mode": "Markdown",
                    }, api_base=api_base)
                except RuntimeError:
                    pass
                comment = _wait_for_comment(token, offset, time.time(),
                                            comment_timeout_seconds, api_base, user_id)
                return {"value": value, "user": user_label, "comment": comment}

            _ack_callback(token, cq["id"], f"Выбрано: {opt['label']}", api_base)
            return {"value": value, "user": user_label, "comment": None}

    return None


def _wait_for_comment(token: str, offset: int | None, click_time: float,
                      timeout_seconds: int, api_base: str,
                      expected_user_id: int | None) -> str | None:
    """After a prompt_comment click, wait for the next text from the same user.

    We deliberately accept ONLY `expected_user_id` here, not the broader
    approver allow-list — the comment must come from the person who actually
    clicked, otherwise it's not their justification.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining = deadline - time.time()
        long_poll = max(1, min(LONG_POLL_MAX, int(remaining)))
        params: dict = {"timeout": long_poll, "limit": 20,
                        "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = _api_call(token, "getUpdates", params,
                             http_timeout=long_poll + 10, api_base=api_base)
        except RuntimeError:
            time.sleep(2)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            from_user = msg.get("from") or {}
            if from_user.get("id") != expected_user_id:
                continue
            text = (msg.get("text") or "").strip()
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="request_telegram_approval",
        description="Send a Telegram approval request and block until decided.",
    )
    p.add_argument("--title", required=True,
                   help="Short question, e.g. 'Deploy to production?'")
    p.add_argument("--details", required=True,
                   help="One-line description of what will happen if approved.")
    p.add_argument("--risk", default="medium",
                   choices=["low", "medium", "high", "critical"],
                   help="Risk level shown to the user (default: medium).")
    p.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                   help=f"Max seconds to wait for a decision (default: {DEFAULT_TIMEOUT_SECONDS}).")
    p.add_argument("--request-id", default=None,
                   help="Override the auto-generated request ID (use a UUID).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress informational stdout (decision still printed).")
    p.add_argument("--option", action="append", default=None, metavar="LABEL:VALUE[:prompt_comment]",
                   help="Replace the default Approve/Reject UI with custom buttons. "
                        "Repeat for each option. Append ':prompt_comment' to a value to "
                        "request a follow-up text comment after the click. When --option "
                        "is used, the script prints a JSON {decision,user,comment,request_id} "
                        "to stdout and exits 0 on any choice (instead of 0/1 for approve/reject). "
                        "If no provided option has the 'prompt_comment' flag, a free-text "
                        "fallback button is auto-injected so the approver always has a way to "
                        "answer 'none of the above'. Disable with --no-custom-option.")
    p.add_argument("--no-custom-option", action="store_true",
                   help="Suppress the auto-injected free-text fallback option in picker mode. "
                        "Use only when the set of choices is genuinely exhaustive and a "
                        "free-form answer would be meaningless.")
    p.add_argument("--recommend", action="append", default=None, metavar="VALUE",
                   help="Mark an --option VALUE as the model's recommended choice. Its "
                        "button gets a ⭐ marker and a '(рекомендую)' suffix so it stands "
                        "out on the phone. In picker mode at least one --recommend is "
                        "REQUIRED (exit 3 otherwise) — the model must always recommend at "
                        "least one option. Repeat to recommend several. The auto-injected "
                        "free-text option can never be recommended.")
    p.add_argument("--comment-timeout-seconds", type=int, default=DEFAULT_COMMENT_TIMEOUT_SECONDS,
                   help=f"Seconds to wait for a follow-up comment after a 'prompt_comment' "
                        f"button is clicked (default: {DEFAULT_COMMENT_TIMEOUT_SECONDS}). "
                        f"If no comment arrives in time, the choice still stands; "
                        f"comment in the JSON output will be null.")
    p.add_argument("--command", default=None, metavar="TEXT",
                   help="Shell command (or any payload) to render verbatim as a Markdown "
                        "code block in the Telegram message, separately from --details. "
                        "Use this when --details is short human context and --command is "
                        f"the literal thing the user is approving. Truncated to "
                        f"{_CODE_BLOCK_MAX} chars with a visible note if longer.")
    return p.parse_args(argv)


def _resolve_approver_ids(raw: str, chat_id: str) -> set[int]:
    """Parse the approver allow-list. Raises ValueError on misconfiguration.

    - If `raw` is non-empty: parse comma-separated ints.
    - Else if `chat_id` is a positive int (DM): default to {chat_id}, since
      in a private chat the chat_id and the owner's user_id coincide.
    - Else: refuse — group/channel chats need an explicit allow-list, otherwise
      anyone in the chat could approve.
    """
    raw = (raw or "").strip()
    if raw:
        ids: set[int] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.add(int(chunk))
            except ValueError:
                raise ValueError(
                    f"TELEGRAM_APPROVER_IDS: '{chunk}' is not a numeric Telegram user ID"
                )
        if not ids:
            raise ValueError("TELEGRAM_APPROVER_IDS is set but contains no valid IDs")
        return ids

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        raise ValueError(
            f"TELEGRAM_CHAT_ID '{chat_id}' is not numeric, and TELEGRAM_APPROVER_IDS "
            f"is unset — cannot determine who is allowed to approve."
        )
    if chat_id_int <= 0:
        raise ValueError(
            "TELEGRAM_CHAT_ID is for a group/channel (non-positive ID), so the "
            "approver allow-list cannot be inferred. Set TELEGRAM_APPROVER_IDS "
            "to a comma-separated list of numeric user IDs."
        )
    return {chat_id_int}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    api_base = os.environ.get("TELEGRAM_API_BASE", DEFAULT_API_BASE).rstrip("/")
    approver_raw = os.environ.get("TELEGRAM_APPROVER_IDS", "")

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID is not set.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.timeout_seconds <= 0:
        print("ERROR: --timeout-seconds must be positive.", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        approver_ids = _resolve_approver_ids(approver_raw, chat_id)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    request_id = args.request_id or uuid.uuid4().hex[:12]

    # Multi-option mode: --option flag(s) supplied. Different message format,
    # different output contract (JSON to stdout, exit 0 on any choice).
    if args.option:
        try:
            options = [parse_option(s) for s in args.option]
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        seen: set[str] = set()
        for opt in options:
            if opt["value"] in seen:
                print(f"ERROR: duplicate --option value '{opt['value']}'.", file=sys.stderr)
                return EXIT_CONFIG_ERROR
            seen.add(opt["value"])

        # Picker rule: the model MUST recommend at least one option. Enforced
        # here so it can't be skipped — the caller points --recommend at one of
        # its own --option values. Validated against caller options only, before
        # the free-text escape hatch is injected, so that auto-option can never
        # be (and never needs to be) the recommendation.
        recommend_values = [r.strip() for r in (args.recommend or []) if r.strip()]
        for rv in recommend_values:
            if rv not in seen:
                print(f"ERROR: --recommend '{rv}' does not match any --option value.",
                      file=sys.stderr)
                return EXIT_CONFIG_ERROR
        if not recommend_values:
            print("ERROR: picker mode requires at least one --recommend VALUE — the "
                  "model must mark at least one option as recommended.", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        recommend_set = set(recommend_values)
        for opt in options:
            opt["recommended"] = opt["value"] in recommend_set

        # Picker contract: there must always be a free-text escape hatch unless
        # the caller explicitly opted out. If none of the supplied options has
        # prompt_comment, append a localized "custom answer" option.
        if not args.no_custom_option and not any(o["prompt_comment"] for o in options):
            value = _AUTO_CUSTOM_VALUE
            suffix = 1
            while value in seen:
                value = f"{_AUTO_CUSTOM_VALUE}_{suffix}"
                suffix += 1
            options.append({
                "label": _CUSTOM_LABEL,
                "value": value,
                "prompt_comment": True,
                "recommended": False,
            })
            seen.add(value)

        if not args.quiet:
            approvers_preview = ",".join(str(i) for i in sorted(approver_ids))
            opt_preview = ",".join(
                o["value"] + ("*" if o["prompt_comment"] else "")
                + ("!" if o.get("recommended") else "")
                for o in options)
            print(f"[telegram-approval-gate] requesting decision (id={request_id}, "
                  f"risk={args.risk}, timeout={args.timeout_seconds}s, "
                  f"options=[{opt_preview}], approvers=[{approvers_preview}])",
                  file=sys.stderr)

        sent_at = time.time()
        try:
            send_options_message(token, chat_id, args.title, args.details,
                                 args.risk, request_id, options, api_base,
                                 command=args.command)
        except RuntimeError as e:
            print(f"ERROR: failed to send approval request: {e}", file=sys.stderr)
            return EXIT_API_ERROR

        try:
            decision = wait_for_options_decision(
                token, chat_id, request_id, args.timeout_seconds, sent_at,
                api_base, approver_ids, options, args.comment_timeout_seconds)
        except KeyboardInterrupt:
            print("CANCELLED: interrupted while waiting for decision.", file=sys.stderr)
            _send_followup(token, chat_id, request_id,
                           "запрос отменён (прерывание)", api_base)
            return EXIT_REJECTED

        if decision is None:
            print(f"TIMEOUT: no decision received within {args.timeout_seconds}s "
                  f"(request_id={request_id}).", file=sys.stderr)
            _send_followup(token, chat_id, request_id,
                           "⌛ таймаут — решение не зафиксировано", api_base)
            return EXIT_TIMEOUT

        out = {
            "decision": decision["value"],
            "user": decision["user"],
            "comment": decision["comment"],
            "request_id": request_id,
        }
        print(json.dumps(out, ensure_ascii=False))
        comment_tag = (f" — “{decision['comment']}”" if decision["comment"]
                       else "" if not any(o["prompt_comment"] for o in options)
                       else " (без комментария)")
        _send_followup(token, chat_id, request_id,
                       f"✅ выбрано `{decision['value']}` ({decision['user']}){comment_tag}",
                       api_base)
        return EXIT_APPROVED

    # ----- binary Approve/Reject mode (back-compat, unchanged) -----

    if not args.quiet:
        approvers_preview = ",".join(str(i) for i in sorted(approver_ids))
        print(f"[telegram-approval-gate] requesting approval (id={request_id}, "
              f"risk={args.risk}, timeout={args.timeout_seconds}s, "
              f"approvers=[{approvers_preview}])", file=sys.stderr)

    sent_at = time.time()
    try:
        send_approval_message(token, chat_id, args.title, args.details,
                              args.risk, request_id, api_base,
                              command=args.command)
    except RuntimeError as e:
        print(f"ERROR: failed to send approval request: {e}", file=sys.stderr)
        return EXIT_API_ERROR

    try:
        decision = wait_for_decision(token, request_id, args.timeout_seconds,
                                     sent_at, api_base, approver_ids)
    except KeyboardInterrupt:
        print("REJECTED: interrupted while waiting for approval.", file=sys.stderr)
        _send_followup(token, chat_id, request_id, "запрос отменён (прерывание)", api_base)
        return EXIT_REJECTED

    if decision is None:
        print(f"TIMEOUT: no decision received within {args.timeout_seconds}s "
              f"(request_id={request_id}). Treating as rejection.", file=sys.stderr)
        _send_followup(token, chat_id, request_id, "⌛ таймаут — засчитано как отказ", api_base)
        return EXIT_TIMEOUT

    verb, user, reason = decision
    if verb == "approve":
        print(f"APPROVED by {user} (request_id={request_id})")
        _send_followup(token, chat_id, request_id, f"✅ одобрено ({user})", api_base)
        return EXIT_APPROVED

    reason_str = f": {reason}" if reason else ""
    print(f"REJECTED by {user} (request_id={request_id}){reason_str}", file=sys.stderr)
    _send_followup(token, chat_id, request_id, f"❌ отклонено ({user}){reason_str}", api_base)
    return EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
