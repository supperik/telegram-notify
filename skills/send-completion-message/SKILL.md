---
name: send-completion-message
description: Sends a Telegram notification when Claude Code finishes work in a project. Wired as a Stop hook in the telegram-notify plugin, so it fires automatically at the end of every assistant turn (with a per-session cooldown to avoid spam). The model does not normally need to invoke this skill — it runs unattended. The bundled CLI scripts/send_completion_message.py is also available for explicit mid-session pings ("step 1 done, starting deploy").
---

# Send Completion Message

A drop-in Telegram notifier for Claude Code completion events. Lives inside the **telegram-notify** plugin and is wired up as a `Stop` hook by `<plugin_root>/hooks.json`.

## How it fires

Claude Code emits a `Stop` event after each assistant turn ends. The bundled `hooks/stop-hook.py`:

1. Reads the event JSON from stdin (`session_id`, `transcript_path`, `cwd`, …).
2. Checks a per-session cooldown (default 600s) — skips if we already sent within the window.
3. Composes a headline `✅ Claude Code finished in \`<project>\` at HH:MM` and pulls the last assistant message from the transcript as a short summary (≤300 chars).
4. Shells out to `scripts/send_completion_message.py`, which POSTs to the Telegram Bot API.
5. **Always exits 0** — a failed notification never blocks Claude Code.

## Configuration

Reads from environment:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | yes | — | Where to send the notification |
| `TELEGRAM_API_BASE` | no | `https://api.telegram.org` | Override for users behind a proxy / DPI block |
| `TELEGRAM_COMPLETION_COOLDOWN_SECONDS` | no | `600` | Per-session cooldown between sends. `0` disables cooldown. |
| `TELEGRAM_COMPLETION_DISABLED` | no | — | Set to `1`/`true`/`yes` to make the hook a silent no-op |

The same `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` used by the **approval-gate** skill works here — no separate bot needed.

## Manual / mid-session use

The model can also call the CLI directly to ping the user without waiting for Stop:

```bash
python "$CLAUDE_PLUGIN_ROOT/skills/send-completion-message/scripts/send_completion_message.py" \
  --text "Step 1 of 3 done — starting deploy" \
  --summary "All tests green; about to run \`npm run deploy:prod\`."
```

Exit codes:
- `0` — sent successfully
- `3` — config error (missing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`)
- `4` — Telegram API or network error

The CLI **does not block** waiting for any reply — it's strictly fire-and-forget. If you need a yes/no decision, use `approval-gate` instead.

## Safety / privacy

- The transcript snippet is taken verbatim from the last assistant turn and Markdown-escaped before sending. Don't put secrets in your final assistant message; if you must, set `TELEGRAM_COMPLETION_COOLDOWN_SECONDS=0` and pre-emptively call the CLI with a redacted `--summary`.
- Notifications go to whatever `TELEGRAM_CHAT_ID` resolves to. Verify it's a private chat before enabling on a shared/work bot.
- `TELEGRAM_COMPLETION_DISABLED=1` is the cleanest off-switch when you don't want the harness to talk to Telegram (e.g. on flaky network).

See `README.md` for the one-time setup walk-through and the `hooks/settings.example.json` snippet for installing without the plugin layer.
