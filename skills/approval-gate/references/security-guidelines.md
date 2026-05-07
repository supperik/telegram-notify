# Security guidelines

The gate is a security control. Treat it like one. The threats below are listed roughly in order of how often they actually bite people.

---

## 1. Never hardcode the bot token

`TELEGRAM_BOT_TOKEN` is a bearer credential. Anyone holding it can:

- Send messages from your bot to any chat the bot is in.
- Read every message sent to the bot.
- Approve or reject pending requests **as if they were you**.

Rules:

- **Don't paste the token into source files or commit it.** If you do, rotate it immediately via @BotFather (`/revoke` → `/token`).
- **Don't hand the token to a process that doesn't need it.** A build step that just sends a Telegram message doesn't need the token — render the message text in CI, then have a *separate* gated step actually send.
- **Don't include the token in `--details` or any other CLI arg.** Args appear in `ps`, shell history, and process auditing.
- **Don't log the env in error output.** The bundled script never echoes the token in error messages — keep that contract if you fork it.

Use a real secret store: 1Password / Bitwarden / `pass`, AWS/GCP/Azure secret manager, GitHub Actions secrets, Docker secrets, your shell's `~/.config/<tool>/env` with `chmod 600`. In that order of preference.

---

## 2. Don't send sensitive content to Telegram

Anything in `--details` or `--title` is sent to Telegram's servers and lives in chat history (and on every device that's logged into that chat). That includes:

- Private file contents, source code, customer PII.
- Tokens, API keys, database URLs, signing keys.
- Email/Slack message bodies.
- Anything covered by NDA, GDPR, HIPAA, SOC2 scope, etc.

**Summarize, don't quote.** The user is making a yes/no decision — they don't need the full payload, they need enough context to decide.

| Action | ❌ Don't | ✅ Do |
|---|---|---|
| Send email | full body | `Subject "..." to 3 recipients, 412 chars` |
| Apply DB migration | the SQL | `alembic upgrade head — adds 1 column, drops 0 indexes` |
| Push secret to vault | the secret | `Push GitHub PAT for repo X to vault path Y` |
| Edit config file | the diff | `Modify .env in /etc/myapp — 2 keys changed` |

If the user genuinely *needs* to see the payload to decide, it's a sign the gate is too coarse-grained — split the workflow so the human reviews the payload through a secure channel before the gate runs.

---

## 3. Default to deny

The script enforces this; your callers must respect it.

- Exit 0 = approved. **Anything else = do not proceed.**
- Treat timeout the same as rejection. Don't lengthen the timeout to "give the user more time" past what's reasonable — a busy user will approve days-old requests reflexively.
- If the gate errors (network, API), **do not fall back to "just run it"**. Surface the error and stop. A failing gate is an incident, not a nuisance.

The `&&` shell pattern (`gate && action`) is correct because it implements deny-by-default automatically. The wrong pattern is `gate; action` (always runs) or `gate || true` (swallows failures).

---

## 4. Use unique request IDs (the script does this for you)

The bundled script auto-generates a UUID-based request ID per invocation. Each approval message includes the ID; the script only accepts replies that match.

This matters because:

- A stale `APPROVE xyz` from yesterday's request can't authorize today's action.
- Two parallel gates in flight don't cross-approve each other.
- If you're scripting and pass `--request-id`, **make it unique per invocation**. Don't reuse a constant value across runs.

When integrating into another system (MCP tool, n8n, etc.), preserve this property: every approval call generates a fresh ID, and the answer must include that ID.

---

## 5. Restrict who can approve (built in — `TELEGRAM_APPROVER_IDS`)

Anyone who discovers the bot's username can `/start` it and, if there are no further controls, press an inline Approve button on a request that wasn't meant for them. The bot username is enumerable; treat it as public information.

The script defends against this with a numeric Telegram user-ID allow-list:

- **DM chats** (positive `TELEGRAM_CHAT_ID`): the allow-list defaults to `{TELEGRAM_CHAT_ID}` — only the chat owner can approve. No extra config needed for personal bots.
- **Group/channel chats** (negative `TELEGRAM_CHAT_ID`): `TELEGRAM_APPROVER_IDS` is **required** (comma-separated user IDs). Without it the gate exits 3 at startup. There's no safe default for groups because the chat ID is unrelated to any individual user.
- **Multiple approvers:** set `TELEGRAM_APPROVER_IDS` explicitly even in DM mode if you want to allow more than one person.

Decisions from a non-listed user are dropped:
- Inline button → ack-replied with `Not authorized` so Telegram's UI clears the spinner, but no decision is recorded.
- Text command (`APPROVE <id>` / `REJECT <id> ...`) → silently ignored. The bot does not reply at all, denying the attacker any signal that they hit a real request.
- Either case logs to stderr: `ignored {channel} decision from unauthorized user_id=X (username): <payload>`.

How to find a user's numeric ID: open a chat with the bot from that user's account, send any message, then `curl https://api.telegram.org/bot$TOKEN/getUpdates` and read `result[*].message.from.id`. Or use `@userinfobot`.

Other layered defenses still apply:

- **Prefer DMs over groups** when you can. Smaller attack surface, no audience for the request_id.
- **Disable group additions** for your bot in BotFather (`/setjoingroups` → `Disable`) so an attacker can't add the bot to a chat they control.
- **Rotate the token if the chat composition changes** (a former approver leaves the group, etc.). Allow-list edits don't invalidate prior knowledge.
- **Don't reuse one bot across projects.** A token leaked in one place gives the holder write access to every chat that bot is in.

---

## 6. Don't reuse the bot for unrelated traffic

A dedicated bot per use case is cleaner than one bot doing notifications + approvals + chat:

- Easier to revoke if compromised (one rotation, narrow blast radius).
- Easier to filter — the chat for approvals only contains approval traffic.
- Easier to audit — you can read the bot's whole message history and see every decision.

BotFather lets you create as many bots as you need.

---

## 7. Beware approval fatigue

This isn't a Telegram threat — it's a human one. If every action prompts an approval, users approve reflexively without reading. Then the gate stops working as a safety control even though it still runs.

Counter it by:

- **Gate at the right granularity.** A deploy: yes. Each individual file the deploy touches: no.
- **Make titles distinctive.** Identical-looking prompts are the easiest to rubber-stamp.
- **Surface the risky bit in `--details`.** "Force-pushing 3 commits" is harder to ignore than "git operation".
- **Default `--risk` honestly.** If everything is `critical`, nothing is.

---

## 8. Operational hygiene

- **Rotate the token periodically** — quarterly is reasonable for a personal bot, more often for shared ones. `/revoke` → `/token` in BotFather.
- **Pin a Python version** for the script in production (`python3 --version`). The script is stdlib-only and doesn't need pinning for correctness, but reproducibility matters.
- **Set a sensible default timeout.** 300s (5 min) is the script default. For unattended workflows, raise it; for tight CI windows, lower it. Either way, the wait is bounded.
- **Monitor `/getMe`** if the gate is on a critical path — a revoked or rate-limited bot is detectable before the gate is needed.
