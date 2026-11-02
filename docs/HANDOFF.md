# Handoff for the session on the server

You are Claude Code running on the rented GPU server. The code was built and tested on a Windows machine with
**no Docker, GPU, real model or real MinIO**. Your job is first bring-up and tuning. Setup steps are in
`docs/SERVER_SETUP.md`.

## What this is
A self-hosted AI platform. Users get one chat box (`/ui/`). `guards.triage` sorts each message:
- **Private:** no tools, no audit, encrypted with a per-user key that only their password or recovery code unlocks.
- **Work:** runs in a department through the Request Manager, with tools, quotas, audit and approvals. It is stored
  apart from the encrypted chat because an approver must resume it without the user's key, then copied into the chat.
- **Orders, bookings, bills:** go to the accounts inbox.
- **Explicit or illegal:** blocked, with that one message reported to HR and the admin.

## Decisions the owner made (don't reverse without asking)
- Chat text is kept forever and encrypted per user. Attachments are deleted after 3 days.
- There is one admin, who may approve their own requests. Accounts is notified of the admin's finance work, except
  private chat and personal data.
- NSFW means explicit or illegal. When triage is unsure, a message stays private.
- `initiate_payment`, `manage_bill`, `manage_order` and `book_service` always need approval.
- **No limit on failed logins.** The owner declined it.
- A person can hold several departments. Roles are read from the database on every request.
- Grafana runs on the server only.

## This server (found 2026-11-02)
- **It is a container, not a VM** (vast.ai behind aiccloud.in): no `docker` command and no `CAP_SYS_ADMIN`, so
  `docker compose` can't run here. Run services directly: vLLM via `scripts/run_vllm.sh`
  (see "Without Docker" in `docs/SERVER_SETUP.md`). Postgres, Redis and MinIO still need native installs.
- vLLM 0.32.0 is installed in `/root/vllm-env` (Python 3.12). It sees the GPU (compute capability 7.5). On this card it
  needs `--dtype half` (bf16 is refused below 8.0) and uses the Triton attention backend. AWQ, GPTQ and
  compressed-tensors INT4 all accept compute capability 7.5, and AWQ and GPTQ have MoE kernels.
- **huggingface.co is unreachable from here:** DNS returns fake addresses and connections time out.
  `/root/models/gemma-4-26b` is empty. The owner must decide how to get weights onto the box.
- Gemma is trained in bf16. In fp16 it has a history of overflowing to inf or NaN. Check the first outputs for garbage.
- The root disk is 40 GB. The guide's 100 GB minimum isn't met, so keep the weights at INT4 and clear caches.
- The app's test venv is `.venv` (Python 3.12, installed with `uv`). Plain pip got stuck resolving dependencies.
- Running natively (2026-11-02): `.env` was made with `--native`. Postgres 16 (PGDG apt) and Redis 6.0 run on
  localhost, migrations are at head, and `tests/test_postgres.py` passes against a real Postgres. The gateway and
  retention loop run via `scripts/start_native.sh`. Still missing: MinIO (build from source, see SERVER_SETUP),
  the admin user, and the monitoring stack. Weights: `cyankiwi/gemma-4-26B-A4B-it-qat-AWQ-INT4` (compressed-tensors
  W4A16, from Google's QAT checkpoint) in `/root/models/gemma-4-26b`.
- Access is through a Cloudflare quick tunnel (`cloudflared`, started by `start_native.sh`): a random public
  `https://<words>.trycloudflare.com` address that changes on every restart (`start_native.sh status` prints it).
  Tailscale was tried and removed at the owner's request. The login page is public and failed logins are not
  limited (owner's decision), so prefer a named tunnel with Cloudflare Access once there is a domain.
- vLLM needs `gcc` (Triton compiles kernels at startup) and `deploy/patches/vllm-0.32.0-turing-head512.patch`:
  Gemma 4's 512-wide full-attention heads exceed Turing's 64 KB shared memory with vLLM's default tiles. With both,
  the model loads (16.6 GiB), has a 157k-token KV cache (about 19 concurrent 8k chats) and answers coherently in fp16
  at about 90 tokens/s for a single chat. vLLM does not log prompts (verified).
- Admin pages: Users (departments, reset password, disable/enable; migration 0005 adds `users.disabled`, checked at
  login and on every request) and Credentials (vault secrets; values are write-only). An admin reset removes the
  password's copy of the chat key but keeps the recovery code's copy, so "Forgot password?" still restores chats;
  chats encrypted under a key nobody can unlock any more are left out of the chat list.
- The web UI was restyled (brand mark, sign-in card, welcome screen, sidebar sections). Same CSP, same class names.
- `huggingface.co` works only because of `/etc/hosts` entries the owner added (real addresses from DNS-over-HTTPS).
  CDN addresses rotate, so redo them if downloads fail.

## Not verified yet: check these first
1. **`docker compose up --build` has never run.** Check that the Dockerfile builds, that multi-word `VLLM_EXTRA_ARGS`
   splits into separate flags, and that the non-root app user works.
2. ~~**Collector to Loki.**~~ Verified natively (Alloy in place of the collector): gateway logs reach Loki with
   `service_name`, and both dashboards show live data. vLLM 0.32 renamed `vllm:gpu_cache_usage_perc` to
   `vllm:kv_cache_usage_perc`; the dashboard and the GpuCacheFull alert now use the new name.
3. **MinIO image.** Docker Hub returned nothing for `minio/minio`, so compose uses the tag from MinIO's own compose
   file on quay.io. Quay needed auth to confirm the tag: check that it pulls and that `mc ready local` works.
4. **Attachments and retention against real MinIO.** Only fake storage has been used.
5. **`scripts/backup.sh` and `scripts/restore.sh` with real Docker.** They were tested with fake `docker` and `rclone`
   commands. Do a real restore test.
6. **Does vLLM log prompts?** In 0.32.0 `--enable-log-requests` defaults to off, and `scripts/run_vllm.sh` refuses
   it. Still confirm in the vLLM log after a chat.
7. ~~**`role: tool` messages.**~~ Fixed 2026-11-02. Gemma 4's template silently dropped them, so the model never saw
   tool output and repeated the call. `llm.chat` now sends them as "Tool result:" user turns. Gemma also sometimes
   answers in its own `call:name(k="v")` syntax: `_parse_tool_call` accepts that too (literal arguments only).

## Then do these
1. Get the weights. The model id is now `google/gemma-4-26B-A4B-it`. The card is a Quadro RTX 8000 (48 GB, Turing,
   driver 580, CUDA 13.0): no FP8 and no bf16, so use AWQ or GPTQ INT4 weights with `--dtype half`.
2. Benchmark, and tune `--max-model-len`, `--max-num-seqs`, and `max_concurrency` in `app/departments/packs.py`.
3. ~~**Test triage accuracy with the real model.**~~ Done 2026-11-02 with `scripts/eval_triage.py` (54 labelled
   cases in `tests/eval/triage_cases.jsonl`, 3 runs, admin and logistics+research personas): 324/324 correct, and
   0/180 personal messages sorted as work, including 78 that use work words ("my bill", "order a pizza"). Triage
   now lists every department and marks the ones a person isn't in, so their requests name the real owner and the
   Request Manager refuses them, instead of landing in a department they have. The set is small and hand-written:
   add real misroutes to it as they appear, and re-run after any prompt or model change.
4. Real TLS and DNS, load test, security review.
5. Alert notifications: rules exist in `deploy/prometheus-rules.yml`, but nothing sends them. Add a Grafana contact point.

## Known limits
- Payment, email/SMS, ERP and web search tools are stubs until vendor accounts exist.
- Attachments work in private messages only. The AI reads text and PDF only, and unreadable files skip the NSFW check.
- Each work request runs in one department.
- A request already waiting for approval keeps the roles its requester had when they asked.
- SQLAlchemy 2.1 is newer than the OpenTelemetry instrumentor supports, so database spans are skipped and it logs one
  error at startup.
- No way yet to delete a user's data on request, and no script for rotating the vault master key.
- `docs/PROGRESS_AND_ROADMAP.md` is historical. This file is the current state.

## Working here
- Read `CLAUDE.md` for conventions. Tests: `.venv/bin/python -m pytest tests/ -q`. Lint: `ruff check .`.
- `tests/test_postgres.py` needs `POSTGRES_INTEGRATION=1` and a **throwaway** `POSTGRES_DSN`.
- After changing a model, run `alembic revision --autogenerate`. A test fails if models and migrations differ.
- Never put message content in logs, spans or exception text.
- The owner lets you commit to the current branch unless a change is alarming. Don't commit `.claude/`.
