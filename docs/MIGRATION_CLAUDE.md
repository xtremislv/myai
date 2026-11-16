# Moving to a no-GPU server with Claude as the model

From: the vast.ai GPU host (Gemma 4 26B on vLLM, Quadro RTX 8000).
To: an ordinary Ubuntu 24.04 server, **8 vCPU / 16 GB RAM / 100 GB SSD, no GPU**, with Claude through the
Anthropic API. Branch: `claude-api-migration`.

## What changes and what stays

| | Before | After |
|---|---|---|
| Model | Gemma 4 26B on vLLM (local GPU) | Claude, picked per call by the model manager: Haiku / Sonnet / Opus / Fable, within a monthly budget |
| GPU | 48 GB Quadro RTX 8000 | none |
| Voice input | Whisper on the GPU | the same Whisper model on the CPU (`STT_DEVICE=cpu`): a few seconds per clip |
| Start on boot | `/etc/rc.local` (container) | systemd `ai-platform.service` (runs the same `scripts/start_native.sh`) |
| Backups | manual / timer not usable | `ai-platform-backup.timer`, nightly 02:30 |
| Domain, tunnel | `tw-ai.in`, `grafana.tw-ai.in`, Cloudflare tunnel `tw-ai` | **the same**: the tunnel's credentials move, Cloudflare routing and DNS are untouched |
| Android app, website, logins | | **unchanged**: same address, same accounts, same sessions (same `.env` secrets) |
| Data | Postgres, MinIO, vault | moved as-is (backup + restore, checked) |
| Agents' web traffic | through the VPS SSH proxy (`OUTBOUND_PROXY`) | unchanged (same key, same VPS) |

**Privacy, to decide knowingly:** with Gemma nothing left the server. With Claude, every model call
(private chats included, attachments, mail text the agents read) is sent to Anthropic's API. It stays
encrypted at rest here as before, and nothing is logged here, but review Anthropic's commercial terms and
data-retention options for the organisation before the switch.

## How the code switched

- `LLM_PROVIDER=anthropic` makes `app/llm/client.py` send every call through `app/llm/anthropic_backend.py`,
  which converts the app's (OpenAI-shaped) messages and tools to the Messages API and back. No caller changed.
  Prompt caching is on (5 minutes), temperature isn't sent, and Claude's thinking blocks are never replayed
  (the app edits its history: fitted context, compacted tool results, the clock stamp).
- Model per call: the **model manager** (`app/llm/model_manager.py`) picks a model per call:
  Haiku 5.5 for background and short sorting work (triage, safety checks, tool picking, scout searches, mail and
  file summaries), Sonnet 5.5 for normal answers and scout judging, Opus 5.5 for multi-step work (supervisor and
  specialists, team mode, long tool chains, big contexts, analysis/planning requests), Fable 5.2 when a person
  asks for deep thinking. Monthly mix targets 50/35/10/5% (`LLM_TIER_TARGETS`) act as ceilings: a tier over its
  share steps down one tier. Budget `LLM_MONTHLY_BUDGET` (50,000 INR at `LLM_USD_RATE`): admin alerts (bell +
  email) when the month is projected over budget, at 80% and at 100%; economy mode (Fable/Opus step down) while
  projected over; only Haiku/Sonnet once it is used up (`LLM_BUDGET_HARD_STOP=true` refuses calls instead).
  This month's figures: `GET /v1/admin/llm-usage`, Prometheus `llm_month_spend_usd` / `llm_month_projected_usd`.
- `LLM_THINKING=off` by default (fast, cheap); `adaptive` lets the model think where it helps. Claude Opus 5.5
  and Fable 5.2 always think. `LLM_SMART_ROUTING=false` sends everything to `LLM_MODEL`.
- No GPU governor: background work (scouts, summaries, mail sheets) takes at most
  `LLM_BACKGROUND_CONCURRENCY` (4) calls at a time per process, so live chats stay quick within the
  account's rate limits.
- `/readyz` checks the key and the route to the API (free model lookup); `start_native.sh status` shows it as
  `claude-api`. Prometheus leaves out the vLLM job (its "down" alert would fire forever).
- Token use is still logged per task (`LLM_USAGE_LOG`), now with the model; `scripts/llm_cost.py` adds what
  was actually spent per model.

## Before you start

1. **Anthropic API key**: console.anthropic.com > API keys. Set a monthly spend limit and an alert under
   Billing / Limits. Expected spend at today's use: see `scripts/llm_cost.py` (about $570 / 30 days measured
   on the GPU server with Sonnet everywhere; Haiku for the short calls lowers it).
2. **The new server**: Ubuntu 24.04, root SSH access, outbound internet. No inbound ports are needed besides
   SSH: everything listens on 127.0.0.1 and the Cloudflare tunnel connects outwards.
3. Repository access on the new server (the repo is private): a deploy key or a token. The old server's git
   credentials are not part of the bundle; set them up separately.

## 1. Install the new server (≈15 minutes, nothing on the old server changes)

```bash
git clone -b claude-api-migration <repo url> /root/myai
cd /root/myai
scripts/install_server.sh            # --no-voice to skip Whisper
```
Installs Postgres 16, Redis, MinIO, cloudflared, Grafana/Loki/Alloy/Prometheus, the Python env (with the
Anthropic SDK), Chromium for the agents, the code sandbox, Whisper (CPU), a 4 GB swap file, and the systemd
units (`ai-platform.service`, `ai-platform-backup.timer`). Safe to rerun.

## 2. Rehearsal (the old server keeps serving tw-ai.in)

On the **old** server:
```bash
cd /root/myai && git pull && scripts/migrate_export.sh
scp /root/migration/tw-ai-migration-<time>.tar root@<new>:/root/
```
On the **new** server:
```bash
cd /root/myai && scripts/migrate_import.sh /root/tw-ai-migration-<time>.tar   # asks for the API key once
```
A rehearsal bundle starts the new server with `TUNNEL_OFF=1` (the domain stays on the old server) and
`SCHEDULER_OFF=1` (no duplicate reminders, digests or scout runs). Test it from your computer:
```bash
ssh -L 8000:127.0.0.1:8000 -L 3000:127.0.0.1:3000 root@<new>     # then http://127.0.0.1:8000/ui/
```
Check: sign in, a private chat, a work request with tools, web research, a file upload, voice input,
Grafana. `scripts/start_native.sh status` should be all `up`. Changes made here are thrown away at cutover.

## 3. Cutover (a few minutes of downtime)

On the **old** server:
```bash
scripts/migrate_export.sh --final            # add --with-android-key to publish app updates from the new server
scp /root/migration/tw-ai-migration-<time>.tar root@<new>:/root/
```
`--final` stops the old gateway, scheduler and watchdog, makes the bundle, then stops the old tunnel and
sets `TUNNEL_OFF=1` there (so a reboot can't bring it back: two servers on one tunnel would split visitors).

On the **new** server:
```bash
scripts/migrate_import.sh /root/tw-ai-migration-<time>.tar
```
It restores the final data, turns the tunnel and the scheduler on, starts everything through systemd, asks
Claude for a one-word reply, and prints the status. `https://tw-ai.in` and the Android app now reach the
new server. Then delete the bundles on both servers (`shred -u <bundle>`): they hold every secret.

**Roll back** (new server misbehaves): on the new server `systemctl stop ai-platform`; on the old one
`python3 scripts/env_set.py TUNNEL_OFF=0 && scripts/start_native.sh`. Anything written on the new
server in between stays there only.

## 4. After the move

- Claude Code sessions here: `scripts/setup_claude_code.sh` (installs the CLI, seeds its memory with the
  background and the owner's rules), then read `docs/CLAUDE_CODE_SERVER.md`.
- `scripts/start_native.sh status`, Grafana's dashboards, and the admin's bell for alerts.
- Backups: set `BACKUP_DEST` (an rclone remote, ideally `crypt`) in `.env`; until then they stay on the server.
  Prove one: `scripts/restore.sh --verify /var/backups/ai-platform/<latest>.tar`.
- Spend: `.venv/bin/python -m scripts.llm_cost --hours 24` (last table: actual Claude spend by model).
- Tuning: `python3 scripts/env_set.py KEY=value` then `systemctl restart ai-platform`:
  `LLM_MONTHLY_BUDGET`, `LLM_USD_RATE`, `LLM_TIER_TARGETS`, `LLM_LIGHT_MODEL` / `LLM_STANDARD_MODEL` /
  `LLM_COMPLEX_MODEL` / `LLM_DEEP_MODEL`, `LLM_SMART_ROUTING`, `LLM_THINKING`, `LLM_EFFORT`, `LLM_BACKGROUND_CONCURRENCY`.
- Android publishing from the new server needs the Android SDK and the signing key
  (`--with-android-key`; never generate a new key: installed apps refuse updates signed with another).
- Keep the old server a few days, then stop renting it. Its `/root/models` and `/root/vllm-env` aren't needed.

## Services and resources on the new server

Measured on the GPU host without the model: about 3.7 GB RAM for all services together, low CPU. Each live
agent browser adds 0.3-0.5 GB; Whisper on the CPU about 1-1.5 GB while transcribing.

| Service | Port (127.0.0.1) | Started by |
|---|---|---|
| Postgres 16, Redis | 5432, 6379 | systemd (Ubuntu packages) |
| Gateway (FastAPI) | 8000 | `ai-platform.service` → `start_native.sh` |
| Whisper (voice) | 8011 | same |
| MinIO | 9000 | same |
| Scheduler, retention, watchdog | — | same |
| Loki, Alloy, Prometheus, Grafana | 3100, 4317/12345, 9090, 3000 | same |
| SSH SOCKS proxy to the VPS | 1080 | same (when `PROXY_SSH` is set) |
| cloudflared (tunnel `tw-ai`) | metrics 20260 | same |
