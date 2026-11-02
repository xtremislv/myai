#!/usr/bin/env bash
# Start vLLM without Docker, for hosts where the server is itself a container (vast.ai /
# aiccloud.in instances can't run Docker). Reads the same .env as docker-compose.yml.
#
#   scripts/run_vllm.sh                # foreground
#   nohup scripts/run_vllm.sh > /var/log/vllm.log 2>&1 &
#
# Settings (all from .env, all optional except LLM_MODEL):
#   LLM_MODEL            name the gateway asks for; served under this name
#   VLLM_MODEL_PATH      local weights directory; if unset, LLM_MODEL is downloaded from Hugging Face
#   VLLM_BIN             vllm executable (default: /root/vllm-env/bin/vllm, else vllm on PATH)
#   VLLM_HOST/VLLM_PORT  default 127.0.0.1:8001. Keep it on localhost: vLLM has no auth.
#   VLLM_MAX_MODEL_LEN, VLLM_GPU_UTIL, VLLM_EXTRA_ARGS   as in docker-compose.yml
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-.env}"
# Read KEY=VALUE lines literally, like docker compose's env_file: values are unquoted and may
# contain spaces ("VLLM_EXTRA_ARGS=--dtype half"), so the file can't simply be sourced.
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
  done < "$ENV_FILE"
fi

: "${LLM_MODEL:?Set LLM_MODEL in .env}"
MODEL_SOURCE="${VLLM_MODEL_PATH:-$LLM_MODEL}"
if [[ -n "${VLLM_MODEL_PATH:-}" && ! -f "$VLLM_MODEL_PATH/config.json" ]]; then
  echo "VLLM_MODEL_PATH=$VLLM_MODEL_PATH has no config.json; download the weights first." >&2
  exit 1
fi

if [[ -z "${VLLM_BIN:-}" ]]; then
  if [[ -x /root/vllm-env/bin/vllm ]]; then VLLM_BIN=/root/vllm-env/bin/vllm; else VLLM_BIN=vllm; fi
fi

# Never log prompts or outputs: that would bypass the per-user chat encryption.
if [[ "${VLLM_EXTRA_ARGS:-}" == *--enable-log-requests* || "${VLLM_EXTRA_ARGS:-}" == *--enable-log-outputs* ]]; then
  echo "Refusing to start: request/output logging would write private chat text to disk." >&2
  exit 1
fi

# VLLM_EXTRA_ARGS is split on spaces on purpose, so "--dtype half --quantization awq" becomes 4 args.
# shellcheck disable=SC2086
exec "$VLLM_BIN" serve "$MODEL_SOURCE" \
  --served-model-name "$LLM_MODEL" \
  --host "${VLLM_HOST:-127.0.0.1}" \
  --port "${VLLM_PORT:-8001}" \
  --max-model-len "${VLLM_MAX_MODEL_LEN:-8192}" \
  --gpu-memory-utilization "${VLLM_GPU_UTIL:-0.90}" \
  ${VLLM_EXTRA_ARGS:-}
