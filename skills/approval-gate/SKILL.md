---
name: telegram-approval-gate
description: Routes human-in-the-loop questions to Telegram instead of blocking the chat. Use this in TWO situations. (1) BEFORE executing any sensitive, risky, expensive, or destructive action — sends a binary Approve/Reject request and waits for the user. Trigger whenever you are about to delete or overwrite files, run `rm -rf`, push or force-push to git, deploy or publish, run `npm run deploy`, modify production data or secrets, change CI/CD or infrastructure, call paid APIs, or send emails/messages. (2) WHENEVER you would otherwise ask the user a clarifying or architectural question in chat — use picker mode (`--option Label:value`, optionally `:prompt_comment` for free-text follow-up) so the user can answer from their phone instead of having to read the terminal. Treat questions like "should I use X or Y?", "delete this or keep it?", "where does this belong?" as picker candidates, not chat candidates. Always prefer this skill over an inline chat question for genuine decisions. Treat timeouts and ambiguous responses as rejection — deny by default.
---

# Telegram Approval Gate

A reusable human-in-the-loop checkpoint. When you're about to take a risky or irreversible action, run the bundled script and wait for the user to approve via Telegram. Continue **only** if the script exits 0.

## When to require approval

Stop and request approval before any of:

| Category | Examples |
|----------|----------|
| Destructive file ops | `rm -rf`, deleting/overwriting/moving files outside an obvious scratch dir, truncating logs |
| Git side effects | `git push` (especially `main`/`master`/`prod`), force-push, tag push, `git reset --hard` on shared branches |
| Deployment / publishing | `npm run deploy*`, `terraform apply`, `kubectl apply -f` to prod, `npm publish`, container image push, releasing artifacts |
| Production / shared data | DB migrations, dropping tables, mutating production records, cache flushes |
| Money / paid APIs | Anything that bills the user — paid LLM calls in a loop, paid SMS/email, cloud resource creation |
| Outbound messages | Sending emails, Slack/Telegram/SMS messages, posting to social, opening or commenting on PRs/issues that others see |
| Config / secrets / infra | Editing `.env` in real environments, rotating credentials, changing IAM, modifying CI/CD pipelines |
| User-flagged commands | Anything the user has told you (in CLAUDE.md, prior turns, or hook config) requires confirmation |
| Uncertainty | You're not confident the action is what the user wanted, or the action has cross-cutting effects |

If the action is local, reversible, and obviously in-scope (e.g. editing source files, running tests, reading data) — **don't** gate it. Approval friction has a cost; spend it where it matters.

## How to invoke the gate

Call the bundled script from the skill directory:

```bash
python scripts/request_telegram_approval.py \
  --title "Deploy to production?" \
  --details "Claude wants to run: npm run deploy:prod" \
  --risk high \
  --timeout-seconds 300
```

Behavior:
- Exits **0** → approved. Proceed with the action.
- Exits **non-zero** → rejected, timed out, or error. **Do not** proceed. Tell the user what happened and ask what they want to do next.

The script auto-generates a request ID if you don't pass `--request-id`. It prints the decision and (for rejects) the user's reason on stdout; errors go to stderr.

### Required configuration

The script reads these environment variables. If either is missing it exits 3 and refuses to send anything:

- `TELEGRAM_BOT_TOKEN` — bot token from @BotFather
- `TELEGRAM_CHAT_ID` — chat ID to send approvals to (your own user ID, or a private group)

Optional:
- `TELEGRAM_API_BASE` — override API host (defaults to `https://api.telegram.org`)
- `TELEGRAM_APPROVER_IDS` — comma-separated numeric Telegram user IDs allowed to approve. Decisions from anyone else are silently ignored and logged. **Default for DM chats:** the chat owner (i.e., `TELEGRAM_CHAT_ID` itself). **For groups/channels:** required — the gate refuses to start without it.

See `README.md` for one-time setup.

## What the user sees

The script sends a Markdown message with inline Approve/Reject buttons:

```
🔐 Approval required

Action: Deploy to production?
Details: Claude wants to run: npm run deploy:prod
Risk: high
Request ID: a1b2c3d4

[ ✅ Approve ]  [ ❌ Reject ]

Or reply with:
APPROVE a1b2c3d4
REJECT a1b2c3d4 <reason>
```

Either the inline button **or** a typed reply works. The script ignores any message whose request ID doesn't match the one it just sent, so a stale `APPROVE` from earlier can't accidentally green-light the wrong action.

## Writing good approval requests

The user is making the decision on a phone, with seconds of attention. Make it easy.

**Title** — one short question. End with a `?`.
- Good: `Push to main?`, `Delete build directory?`, `Send email to all customers?`
- Bad: `Approval needed for the deployment script`, `Continue?`

**Details** — the literal command or a one-line summary of effects. Include enough context that the user can decide without opening a terminal.
- Good: `Claude wants to run: git push --force origin main (will overwrite 3 commits on remote)`
- Bad: `Doing some git stuff`

**Risk** — pick one of `low`, `medium`, `high`, `critical`. Use `high` or `critical` for anything that touches production, costs real money, or is hard to reverse.

### Examples

| Action | Title | Details | Risk |
|---|---|---|---|
| `rm -rf ./dist` | `Delete build directory?` | `Claude wants to run: rm -rf ./dist (removes 142 files in dist/)` | medium |
| `git push origin main` | `Push to main?` | `Claude wants to push 3 commits to origin/main` | high |
| `npm run deploy:prod` | `Deploy to production?` | `Claude wants to run: npm run deploy:prod` | critical |
| Send customer email | `Send email to 1,247 customers?` | `Subject: "Service update". Recipients: full customer list. Summary: announces tomorrow's maintenance window.` | high |

## Safety rules

These are non-negotiable because the whole point of the gate is to prevent accidents.

1. **Deny by default.** Timeout, network error, ambiguous reply, missing config — all mean "do not proceed." The script enforces this; you must respect its exit code.
2. **Never bypass on failure.** If the gate errors, surface the error to the user and stop. Do not retry-with-no-gate, do not assume approval, do not work around it.
3. **Don't leak secrets in `--details`.** Approval messages are sent to Telegram's servers and live in chat history. Summarize sensitive payloads (`Contains API key for service X`) instead of pasting them. Email bodies, file contents, tokens, customer PII — paraphrase, don't quote. Full guidance in `references/security-guidelines.md`.
4. **One request, one decision.** Each invocation generates a unique request ID. The script only accepts replies that include that exact ID, so a stale "APPROVE" can't authorize the wrong action.
5. **Approvers are scoped.** The script only accepts decisions from numeric user IDs in `TELEGRAM_APPROVER_IDS` (or — for DM chats — the chat owner by default). Anyone else who finds the bot and presses the button gets `Not authorized`; their attempt is logged. This means leaking the bot username alone does not grant approval power.
6. **Don't pre-announce approval.** Don't tell the user "I'll proceed once you approve" and then start the action — wait for the script to return 0 first.

## Integration patterns

Three common ways to wire this up:

1. **Wrap a shell command** — gate then run, in one line:
   ```bash
   python scripts/request_telegram_approval.py --title "Push to main?" \
       --details "git push origin main" --risk high \
     && git push origin main
   ```

2. **Claude Code PreToolUse hook** — auto-gate matching tool calls. See `hooks/pretooluse-hook.py` and `hooks/settings.example.json`.

3. **Inside a Python script or agent loop** — invoke as a subprocess, branch on exit code. See `references/integration-examples.md`.

Full examples (deploy gating, email gating, multi-step workflows, n8n / Make / MCP adapters) live in `references/integration-examples.md`. Read that file when you're wiring the gate into a new system.
