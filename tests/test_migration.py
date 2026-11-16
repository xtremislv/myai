"""The move to a no-GPU server with Claude: .env editing (scripts/env_set.py), a fresh Claude .env
(scripts/generate_env.py --provider anthropic), and the setup scripts' syntax."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import env_set, generate_env

ROOT = Path(__file__).resolve().parent.parent


def test_env_set_replaces_in_place_adds_new_keys_and_comments_out():
    text = "# keep me\nA=1\nSECRET=s\nA=dup\nVLLM_EXTRA_ARGS=--x\n"
    out = env_set.set_values(text, {"A": "2", "LLM_PROVIDER": "anthropic"}, ("VLLM_EXTRA_ARGS",))
    assert out.splitlines()[:4] == ["# keep me", "A=2", "SECRET=s", "# VLLM_EXTRA_ARGS=--x"]
    assert out.rstrip().endswith("LLM_PROVIDER=anthropic") and "A=dup" not in out
    assert env_set.get_value(out, "SECRET") == "s" and env_set.get_value(out, "NOPE") is None


def test_env_set_cli_keeps_the_file_private(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    env_set.main(["--path", str(path), "B=2"])
    assert path.read_text().splitlines()[-1] == "B=2" and oct(path.stat().st_mode)[-3:] == "600"
    with pytest.raises(SystemExit) as done:
        env_set.main(["--path", str(path), "--get", "B"])
    assert done.value.code == 0


def test_generate_env_for_claude_has_no_model_server(tmp_path):
    path = tmp_path / ".env"
    generate_env.write_env(path, SimpleNamespace(
        provider="anthropic", model="", hf_token="", vllm_args="", model_path="", native=True,
        domain="tw-ai.in", grafana_domain="grafana.tw-ai.in", backup_dest=""))
    text = path.read_text()
    assert "LLM_PROVIDER=anthropic" in text and "LLM_MODEL=claude-sonnet-5-5" in text
    assert "VLLM_EXTRA_ARGS" not in text and "STT_DEVICE=cpu" in text and "VAULT_MASTER_KEY=" in text


@pytest.mark.parametrize("script", ["install_server.sh", "migrate_export.sh", "migrate_import.sh",
                                    "start_native.sh", "watchdog.sh", "backup.sh", "restore.sh"])
def test_setup_scripts_parse_and_run(script):
    path = ROOT / "scripts" / script
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    assert path.stat().st_mode & 0o100, f"{script} must be executable (the systemd units run it directly)"


def test_prometheus_config_without_the_model_server_job():
    awk = "/^  - job_name: vllm$/{skip=1; next} skip && /^  - job_name:/{skip=0} !skip"
    out = subprocess.run(["awk", awk, str(ROOT / "deploy/native/prometheus.yml")], capture_output=True,
                         text=True, check=True).stdout
    assert "job_name: vllm" not in out and "job_name: gateway" in out and "job_name: prometheus" in out
    assert awk in (ROOT / "scripts/start_native.sh").read_text()  # the same filter start_native.sh uses
