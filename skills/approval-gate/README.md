# telegram-approval-gate

A Claude Code / agent skill that pauses a workflow before sensitive actions, sends an approval request to Telegram, and resumes only after the human approves.

```
┌─────────────┐   1. detect risky action    ┌──────────────────────┐
│  Claude /   │ ──────────────────────────▶ │ request_telegram_    │
│  agent /    │                              │ approval.py          │
│  shell flow │ ◀── exit 0 (approve)        │  → sends Telegram msg│
└─────────────┘     non-zero (reject/error) │  → polls for reply   │
                                             └──────────────────────┘
```

- **Deny by default.** Timeout, network error, ambiguous reply → non-zero exit.
- **Stdlib-only Python.** No `pip install` step. Runs anywhere with Python 3.9+.
- **Buttons or text.** Inline ✅ / ❌ keyboard, or `APPROVE <id>` / `REJECT <id> <reason>`.
- **Scoped to one decision.** Each request gets a unique ID; stale replies are ignored.

---

## One-time setup

### 1. Create a Telegram bot

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts (display name + unique username ending in `bot`).
3. BotFather replies with an HTTP API token like `123456:AA...`. Save it — that's your `TELEGRAM_BOT_TOKEN`.

### 2. Get the chat ID where approvals should land

The simplest option is your own user ID (DMs from the bot to you):

1. Open a chat with your new bot and send any message (e.g. `/start`).
2. From a terminal, fetch the chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
     | python -c "import json,sys; d=json.load(sys.stdin); \
                  print(d['result'][-1]['message']['chat']['id'])"
   ```
3. Save the printed integer as `TELEGRAM_CHAT_ID`.

For a **group chat**, add the bot to the group, send a message that mentions it, then re-run the curl above and read `chat.id` (group IDs are negative, that's normal).

### 3. Export environment variables

**bash / zsh:**
```bash
export TELEGRAM_BOT_TOKEN="123456:AA..."
export TELEGRAM_CHAT_ID="987654321"
# Optional — restrict who can approve. Defaults to TELEGRAM_CHAT_ID for DMs.
# Required (no default) for group/channel chats.
export TELEGRAM_APPROVER_IDS="987654321,111222333"
```

**PowerShell:**
```powershell
$env:TELEGRAM_BOT_TOKEN    = "123456:AA..."
$env:TELEGRAM_CHAT_ID      = "987654321"
$env:TELEGRAM_APPROVER_IDS = "987654321,111222333"   # optional; see above
```

For persistence, put them in `~/.bashrc` / `~/.zshrc` / your PowerShell profile, or — preferably — load them from a secret manager. **Never commit them.** See `references/security-guidelines.md`.

#### Why `TELEGRAM_APPROVER_IDS` matters

Anyone who finds the bot's username can `/start` it. Without an allow-list, that person could press the inline Approve button or send `APPROVE <id>` and authorize an action on your behalf the moment a request goes out. The allow-list pins approvals to specific numeric Telegram user IDs:

- **DM chats (positive `TELEGRAM_CHAT_ID`):** if `TELEGRAM_APPROVER_IDS` is unset, it defaults to `[TELEGRAM_CHAT_ID]` — only you can approve. This is what you want for personal bots.
- **Group/channel chats (negative `TELEGRAM_CHAT_ID`):** the variable is **required**. Without it, the gate refuses to start (exit 3), because in a group the chat ID and the user ID are unrelated and the script has no way to know who's allowed.

Decisions from a non-listed user are silently ignored — the attacker gets `Not authorized` on a button tap or no reply at all on a text command — and the operator's stderr logs the attempt with the offender's user ID and username.

### 4. Test it

```bash
python scripts/request_telegram_approval.py \
  --title "Test approval" \
  --details "Just verifying the gate works." \
  --risk low \
  --timeout-seconds 60
```

Open Telegram, tap ✅ Одобрить. The script should print `APPROVED by <you>` and exit 0. Tap ❌ Отклонить and it should exit 1. (The Telegram message is in Russian; the `APPROVED by …` line is the script's stdout, kept English for callers that parse it.)

---

## CLI reference

```
python scripts/request_telegram_approval.py \
  --title       <short question, e.g. "Push to main?">      [required]
  --details     <one-line summary of the action>            [required]
  --risk        low | medium | high | critical              [default: medium]
  --timeout-seconds <int>                                   [default: 300]
  --request-id  <override the auto-generated ID>            [optional]
  --quiet       suppress informational stderr               [optional]
```

### Exit codes

| Code | Meaning                                  | Caller should... |
|-----:|------------------------------------------|------------------|
| 0    | Approved                                 | Proceed          |
| 1    | Rejected by user                         | Stop, surface reason |
| 2    | Timed out (no reply within window)       | Stop — treat as rejection |
| 3    | Configuration error (missing env, etc.)  | Stop — fix setup |
| 4    | Telegram API or network error            | Stop — surface error |

Non-zero **always** means "do not proceed."

---

## Files in this skill

```
telegram-approval-gate/
├── SKILL.md                           skill instructions for Claude / agents
├── README.md                          this file
├── scripts/
│   └── request_telegram_approval.py   the gate (stdlib-only Python)
├── hooks/
│   ├── pretooluse-hook.py             example Claude Code PreToolUse hook
│   └── settings.example.json          matching settings.json snippet
└── references/
    ├── integration-examples.md        wrappers, deploy/git/email gating, agents
    └── security-guidelines.md         what not to send, threat model, hardening
```

---

## Quick examples

```bash
# Gate a deploy
python scripts/request_telegram_approval.py \
  --title "Deploy to production?" \
  --details "Claude wants to run: npm run deploy:prod" \
  --risk critical \
  && npm run deploy:prod
```

```bash
# Gate a force-push
python scripts/request_telegram_approval.py \
  --title "Force-push to main?" \
  --details "git push --force origin main (overwrites 3 commits)" \
  --risk high \
  && git push --force origin main
```

More patterns — Claude Code hook, agent-loop integration, n8n / Make / MCP wiring — in `references/integration-examples.md`.
