#!/usr/bin/env bash
# Install the interpreter that runs the assistant's analysis code (app/sandbox.py): system
# Python with numpy and pandas in /opt/agent-sandbox, readable by the unprivileged user `nobody`
# (which can't read /root, so it can't use the app's own .venv). Safe to rerun.
set -euo pipefail
DEST="${SANDBOX_VENV:-/opt/agent-sandbox}"
PY="${SANDBOX_BASE_PYTHON:-/usr/bin/python3}"
command -v uv >/dev/null || { echo "uv is needed (https://docs.astral.sh/uv/)"; exit 1; }
[ -x "$DEST/bin/python" ] || uv venv -q --python "$PY" "$DEST"
uv pip install -q --python "$DEST/bin/python" numpy pandas
chmod -R a+rX "$DEST"
install -d -m 0711 /var/lib/agent-sandbox
su -s /bin/sh nobody -c "$DEST/bin/python -I -c 'import pandas, numpy; print(\"sandbox ok: pandas\", pandas.__version__)'"
