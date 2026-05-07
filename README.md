# telegram-notify

A Claude Code plugin that bridges Claude's runtime with Telegram. Two skills out of the box, designed to grow:

| Skill | Purpose | Triggered by |
|---|---|---|
| [`approval-gate`](skills/approval-gate/SKILL.md) | Human-in-the-loop checkpoint before risky actions; supports binary Approve/Reject and multi-option picker (with optional comment) | Model invocation + `PreToolUse` hook |
| [`send-completion-message`](skills/send-completion-message/SKILL.md) | Fire-and-forget "Claude Code finished" ping with last-message snippet | `Stop` hook (automatic, with per-session cooldown) |

Both skills share a single bot and a single chat — set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` once and you're done.

## Layout

```
telegram-notify-skill/
├── .claude-plugin/plugin.json     ← manifest
├── hooks.json                     ← plugin-level Stop hook
├── .claude-plugin/marketplace.json ← marketplace descriptor (where to fetch the plugin from)
├── lib/telegram_client.py         ← shared Telegram API helpers (stdlib-only)
└── skills/
    ├── approval-gate/             ← migrated from the standalone telegram-approval-gate
    └── send-completion-message/   ← new in this plugin
```

## Installation

```text
/plugin marketplace add https://raw.githubusercontent.com/supperik/telegram-notify/master/.claude-plugin/marketplace.json
/plugin install telegram-notify
```

The marketplace descriptor lives in this repo's `.claude-plugin/marketplace.json`; it points the loader at this same repository on GitHub. If you forked or moved it, swap the URL above for the raw URL of your `marketplace.json`, and update `source` inside `marketplace.json` to match (supported `source.type` values: `github`, `git`, `local`).

## Required environment

Both skills look for the same two variables:

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | yes | DM chat ID, group ID, or channel ID to send to |
| `TELEGRAM_API_BASE` | no | Override `https://api.telegram.org` (use a proxy if `api.telegram.org` is blocked from your network) |

Set them in your shell, in `~/.claude/settings.json`'s `env` block, or as system env vars. The plugin doesn't ship secrets.

### approval-gate-only knobs

See [`skills/approval-gate/README.md`](skills/approval-gate/README.md). Highlights:

- `TELEGRAM_APPROVER_IDS` — comma-separated user IDs allowed to approve (defaults to chat owner for DM chats).
- `TELEGRAM_GATE_TIMEOUT`, `TELEGRAM_GATE_AUTOAPPROVE_SAFE`, `TELEGRAM_GATE_LANG` — used by the bundled `PreToolUse` hook.

### send-completion-message-only knobs

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_COMPLETION_COOLDOWN_SECONDS` | `600` | Min seconds between sends per session (0 disables) |
| `TELEGRAM_COMPLETION_DISABLED` | — | Set to `1`/`true`/`yes` for a silent no-op |

## Bot setup

Once per Telegram account:

1. Message `@BotFather` → `/newbot`. Save the token as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`. Find `"chat": {"id": ...}` — that's your `TELEGRAM_CHAT_ID`.
3. (Optional, recommended) Set `TELEGRAM_APPROVER_IDS` to the same numeric ID so only you can click Approve.

## Working without the plugin

Each skill is also usable standalone — copy `skills/<skill>/` somewhere, set the env vars, and use the `hooks/settings.example.json` snippet to register the relevant hooks in your `~/.claude/settings.json`. The shared `lib/telegram_client.py` is required (the scripts add it to `sys.path` automatically when the directory layout is preserved).

## Verification after install

```bash
# JSON manifests parse
python -c "import json; [json.load(open(p)) for p in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','hooks.json']]"

# Python sources compile
python -m py_compile lib/telegram_client.py \
  skills/approval-gate/scripts/request_telegram_approval.py \
  skills/approval-gate/hooks/pretooluse-hook.py \
  skills/approval-gate/hooks/userpromptsubmit-hook.py \
  skills/send-completion-message/scripts/send_completion_message.py \
  skills/send-completion-message/hooks/stop-hook.py

# End-to-end (requires TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set)
python skills/send-completion-message/scripts/send_completion_message.py \
  --text "smoke test" --summary "from telegram-notify plugin install"
```
