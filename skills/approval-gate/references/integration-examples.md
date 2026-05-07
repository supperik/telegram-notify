# Integration examples

How to wire the approval gate into common workflows. Every example follows the same shape: **run the gate; if it exits 0, proceed; otherwise stop.**

The gate is a single Python script — it works the same from a shell pipeline, a CI step, a Claude Code hook, or another script. Pick the integration that matches your context.

> Replace `$GATE` with the absolute path to `scripts/request_telegram_approval.py` if you're not running from the skill directory.

---

## 1. Wrap a one-off shell command

Chain with `&&` so the action runs only on a clean exit (0 = approved):

```bash
python scripts/request_telegram_approval.py \
    --title "Delete build directory?" \
    --details "rm -rf ./dist (142 files)" \
    --risk medium \
  && rm -rf ./dist
```

PowerShell equivalent (PS 5.1 has no `&&` chain operator):

```powershell
python scripts\request_telegram_approval.py `
    --title "Delete build directory?" `
    --details "Remove-Item -Recurse -Force ./dist" `
    --risk medium
if ($LASTEXITCODE -eq 0) { Remove-Item -Recurse -Force ./dist }
```

---

## 2. Reusable shell wrapper

Drop this into `~/bin/gated` or your dotfiles for `gated <title> -- <command...>`:

```bash
#!/usr/bin/env bash
# gated — run a command only after Telegram approval
set -e
title="$1"; shift
[ "$1" = "--" ] && shift
gate="${TELEGRAM_GATE_SCRIPT:-$HOME/skills/telegram-approval-gate/scripts/request_telegram_approval.py}"
python "$gate" --title "$title" --details "$*" --risk "${RISK:-medium}" \
  && "$@"
```

Usage:

```bash
gated "Push to main?" -- git push origin main
RISK=critical gated "Deploy to prod?" -- npm run deploy:prod
```

---

## 3. Gate `git push` to protected branches

A pre-push hook in `.git/hooks/pre-push` (don't forget `chmod +x`):

```bash
#!/usr/bin/env bash
# Block pushes to protected branches without Telegram approval.
protected_re='^refs/heads/(main|master|prod|release/.*)$'
gate="$HOME/skills/telegram-approval-gate/scripts/request_telegram_approval.py"

while read -r local_ref _ remote_ref _; do
  if [[ "$remote_ref" =~ $protected_re ]]; then
    branch="${remote_ref#refs/heads/}"
    if ! python "$gate" \
         --title "Push to ${branch}?" \
         --details "git push to $(git remote get-url origin) :: ${branch}" \
         --risk high; then
      echo "Push blocked: approval not granted." >&2
      exit 1
    fi
  fi
done
```

---

## 4. Gate a deploy in CI / a Makefile

```makefile
GATE := python scripts/request_telegram_approval.py

deploy-prod:
	$(GATE) --title "Deploy to production?" \
	        --details "make deploy-prod (commit $(shell git rev-parse --short HEAD))" \
	        --risk critical
	./deploy.sh prod
```

In a CI step (GitHub Actions):

```yaml
- name: Approval gate
  env:
    TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
    TELEGRAM_CHAT_ID:   ${{ secrets.TELEGRAM_CHAT_ID }}
  run: |
    python scripts/request_telegram_approval.py \
      --title "Deploy ${{ github.ref_name }} to prod?" \
      --details "commit ${{ github.sha }} by ${{ github.actor }}" \
      --risk critical \
      --timeout-seconds 1800

- name: Deploy
  run: ./deploy.sh prod
```

CI runners typically have a hard step timeout — set `--timeout-seconds` below it so the gate fails cleanly rather than the runner killing the job.

---

## 5. Gate file deletion in a script

```bash
target="./build/release-$(date +%F)"
python scripts/request_telegram_approval.py \
    --title "Delete release artifacts?" \
    --details "rm -rf ${target} ($(du -sh "$target" | cut -f1))" \
    --risk high \
  && rm -rf "$target"
```

The point of putting `du -sh` in `--details` is that the user sees *what they're deleting* (size, count) rather than just the path. Same idea for git — show how many commits, what branch, what remote.

---

## 6. Gate sending an email

Summarize, don't quote. The recipient list and subject are usually fine to send; the body is usually not (see `security-guidelines.md`).

```python
import subprocess, sys

def gated_send_email(to: list[str], subject: str, body: str, risk: str = "high") -> None:
    rcpt_summary = ", ".join(to[:3]) + (f" (+{len(to)-3} more)" if len(to) > 3 else "")
    details = (
        f"Send email to: {rcpt_summary}\n"
        f"Subject: {subject!r}\n"
        f"Body: {len(body)} chars, summary: {body.strip().splitlines()[0][:80]}…"
    )
    result = subprocess.run([
        sys.executable, "scripts/request_telegram_approval.py",
        "--title", f"Send email to {len(to)} recipient(s)?",
        "--details", details,
        "--risk", risk,
    ])
    if result.returncode != 0:
        raise RuntimeError(f"Email send blocked (exit {result.returncode})")
    _send_email(to, subject, body)  # your real send function
```

---

## 7. Inside a Claude Code workflow

Two ways:

**(a) Tell the model directly.** In `CLAUDE.md` or a slash command, add:

```markdown
Before any destructive shell command, deploy, push, or paid API call, run:

    python /path/to/telegram-approval-gate/scripts/request_telegram_approval.py \
        --title "<short question>?" --details "<what will happen>" --risk <level>

Proceed only if the script exits 0. If it exits non-zero, stop and tell the user.
```

**(b) Use a PreToolUse hook** to enforce it deterministically — the harness blocks the tool call until the script returns 0. See `hooks/pretooluse-hook.py` and `hooks/settings.example.json` in this skill.

The hook approach is preferred when you want a hard guarantee (the model can't "forget"). The instruction approach is preferred when you want flexibility — the model decides when gating is appropriate.

---

## 8. From another Python script / agent loop

```python
import subprocess, sys
from pathlib import Path

GATE = Path(__file__).parent / "telegram-approval-gate/scripts/request_telegram_approval.py"

def require_approval(title: str, details: str, risk: str = "medium",
                     timeout: int = 300) -> None:
    """Raise PermissionError if the user does not approve."""
    proc = subprocess.run([
        sys.executable, str(GATE),
        "--title", title, "--details", details,
        "--risk", risk, "--timeout-seconds", str(timeout),
    ])
    if proc.returncode != 0:
        raise PermissionError(
            f"action denied (gate exit {proc.returncode}): {title}"
        )

# usage in an agent step
require_approval(
    title="Run database migration?",
    details="alembic upgrade head — adds 1 column, drops 0",
    risk="high",
)
run_migration()
```

For an `asyncio` loop, use `asyncio.create_subprocess_exec` so the approval wait doesn't block the event loop.

---

## 9. n8n / Make / Zapier

The gate is a CLI script — any platform that can run a shell command and branch on exit code can use it.

**n8n** — use an *Execute Command* node:
```
python /path/to/request_telegram_approval.py \
  --title "{{$json.title}}" --details "{{$json.details}}" --risk high
```
Wire the success output to the next step; wire the error output to a "stop & notify" branch.

**Make / Zapier** don't run arbitrary shell, but they can hit Telegram's API directly. The pattern translates: send a message via Telegram module, then use a *Webhook → Wait* (Make) or a *Schedule → Filter on response* step (Zapier) to gate the next action. The harder part — matching a typed reply to a specific request ID — is what the script does for you, so prefer running this script from a self-hosted runner or n8n where you can.

---

## 10. MCP / custom agent backend

If you're building an MCP server, expose a `request_approval` tool that wraps this script. Tool returns:

```json
{ "approved": true,  "user": "alice" }
{ "approved": false, "user": "alice", "reason": "wrong env" }
{ "approved": false, "reason": "timeout" }
```

The tool implementation is the same `subprocess.run(...)` from example 8 — mapping exit code 0 to `approved: true` and any non-zero to `approved: false` with a reason field.

This lets every agent in your stack — not just Claude Code — call into the same approval surface and bills consistently against the same Telegram chat.
