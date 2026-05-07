# send-completion-message

A Stop-hook-driven Telegram notifier that pings you when Claude Code finishes work in a project. Part of the **telegram-notify** plugin.

## Installation

### Via the telegram-notify plugin (recommended)

If you've installed the parent plugin, the Stop hook is already wired up by `<plugin_root>/hooks.json`. You only need to set the bot credentials in your environment (or in `~/.claude/settings.json`'s `env` block):

```bash
export TELEGRAM_BOT_TOKEN="..."   # from @BotFather
export TELEGRAM_CHAT_ID="..."     # your numeric Telegram user ID, or a private group ID
```

Optional knobs:

```bash
export TELEGRAM_COMPLETION_COOLDOWN_SECONDS=600   # default; 0 to disable
export TELEGRAM_COMPLETION_DISABLED=1             # silent no-op
export TELEGRAM_API_BASE="https://my-tg-proxy/"   # if api.telegram.org is blocked
```

That's it — the hook fires automatically at the end of every assistant turn, with a 10-minute per-session cooldown so active back-and-forth conversations don't spam your phone.

### Standalone (without the plugin)

Copy the skill folder somewhere stable and register the Stop hook in `~/.claude/settings.json`. A ready-made snippet is in `hooks/settings.example.json` — adjust the absolute path to wherever you put the skill, then merge into your settings.

## One-time bot setup

Same setup as `approval-gate` — if you've already configured a bot for that, **reuse it here**: the same `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` work for both skills, no second bot needed. Otherwise:

1. Open Telegram, message `@BotFather`, send `/newbot`, follow the prompts.
2. Save the token it gives you as `TELEGRAM_BOT_TOKEN`.
3. Get your chat ID: message your bot once, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Look for `"chat": {"id": ...}`. Save as `TELEGRAM_CHAT_ID`.

## Smoke test

```bash
python scripts/send_completion_message.py --text "smoke test" --summary "this is a manual ping"
```

You should see `[send-completion-message] sent (… chars to chat …)` on stdout and the message arrive in Telegram. If you get exit `3` — env vars aren't set. If exit `4` — Telegram API or network problem (try `TELEGRAM_API_BASE` if `api.telegram.org` is blocked from your network).

## What gets sent

Headline (bold): `✅ Claude Code finished in \`<project-basename>\` at HH:MM`

Optional body: up to 300 chars of the last assistant message from the transcript, Markdown-escaped. The hook reads `transcript_path` from the Stop event and walks back to the most recent assistant turn — if the transcript is missing or unparseable, you just get the headline.

## Disabling the hook temporarily

```bash
export TELEGRAM_COMPLETION_DISABLED=1
```

…or remove the corresponding entry from `~/.claude/settings.json` if you'd rather opt out permanently.
