---
name: myai-server-setup
description: "How myai runs on the no-GPU server: Claude API as the model, systemd + start_native.sh, tunnel tw-ai, VPS proxy, test DB rule"
metadata:
  type: project
---

Since the move (planned 2026-11-16, branch `claude-api-migration`, docs/MIGRATION_CLAUDE.md) the platform runs on an
8 vCPU / 16 GB / 100 GB Ubuntu 24.04 VM without a GPU. The model is Claude via the Anthropic API
(`LLM_PROVIDER=anthropic`; Sonnet 5.5 main, Haiku 5.5 for short sorting tasks) instead of Gemma on vLLM.
systemd `ai-platform.service` runs `scripts/start_native.sh` at boot (gateway, scheduler, MinIO, Whisper on CPU,
Loki/Alloy/Prometheus/Grafana, VPS proxy tunnel, cloudflared, watchdog); Postgres 16 and Redis are Ubuntu services.
Public access only through the named Cloudflare tunnel `tw-ai`: https://tw-ai.in/ui/ and https://grafana.tw-ai.in.

Agents' web traffic goes through the owner's VPS (SSH SOCKS on 127.0.0.1:1080, `PROXY_SSH*` and `OUTBOUND_PROXY` in
.env): the owner set it up and asked for it. If research breaks, check `start_native.sh status` for proxy-tunnel first.
Don't invent other ways around blocked sites; suggest and let the owner decide.

Every model call now costs money: ask before bulk re-runs or evals and give the expected spend
(`scripts/llm_cost.py` shows actual spend). Redis has a password (use REDISCLI_AUTH, never -a).
`tests/test_postgres.py` must never run against the live DB: use a throwaway `ai_platform_itest` and drop it.
Don't sign tokens as real users to probe.

**Why:** the GPU rental was replaced by the API; the earlier container quirks (no systemd, DNS poisoning) are gone.
**How to apply:** restarts via `systemctl restart ai-platform` or kill a PID + rerun start_native.sh; details in
docs/CLAUDE_CODE_SERVER.md. Related: [[myai-git-push-policy]], [[myai-session-start]].
