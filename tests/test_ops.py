"""Readiness, error handling, metrics exposure, log export, and deployment files."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import APIRouter
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from prometheus_client import REGISTRY

from app import health, observability
from app.main import app
from scripts import generate_env

ROOT = Path(__file__).resolve().parent.parent
compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())


# ---- readiness ----
def _checks(monkeypatch, **results):
    def _make(ok):
        def _check():
            if not ok:
                raise ConnectionError("postgres://user:hunter2@db failed")

        return _check

    monkeypatch.setattr(health, "CHECKS", {name: _make(ok) for name, ok in results.items()})


def test_ready_when_everything_is_up(monkeypatch):
    _checks(monkeypatch, postgres=True, model_server=True)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "checks": {"postgres": "ok", "model_server": "ok"}}


def test_not_ready_names_what_is_down_without_details(monkeypatch):
    _checks(monkeypatch, postgres=False, model_server=True)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == "unavailable"
    assert "hunter2" not in response.text


def test_real_checks_cover_every_dependency():
    assert set(health.CHECKS) == {"postgres", "redis", "object_storage", "model_server"}


def test_liveness_does_not_depend_on_anything(monkeypatch):
    _checks(monkeypatch, postgres=False)
    assert TestClient(app).get("/healthz").status_code == 200


# ---- errors ----
@pytest.fixture
def failing_routes():
    router = APIRouter()

    @router.get("/test-boom")
    def boom():
        raise RuntimeError("private text: MY " + "SECRET")

    @router.get("/test-denied")
    def denied():
        raise PermissionError("no matching allow")

    app.include_router(router)
    yield TestClient(app, raise_server_exceptions=False)
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") not in ("/test-boom", "/test-denied")]


def test_unexpected_error_gives_json_and_a_trace_id_without_internals(failing_routes):
    response = failing_routes.get("/test-boom")
    assert response.status_code == 500
    body = response.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["trace_id"])
    assert "MY SECRET" not in response.text and "RuntimeError" not in response.text


def test_unexpected_error_is_logged_without_its_message(failing_routes, caplog):
    with caplog.at_level(logging.ERROR):
        trace_id = failing_routes.get("/test-boom").json()["trace_id"]
    assert trace_id in caplog.text and "RuntimeError" in caplog.text and "test_ops.py" in caplog.text
    assert "MY SECRET" not in caplog.text


def test_permission_error_becomes_403_with_a_reason(failing_routes):
    response = failing_routes.get("/test-denied")
    assert response.status_code == 403
    assert response.json()["detail"] == "no matching allow" and response.json()["trace_id"]


# ---- metrics ----
def test_metrics_are_not_served_on_the_public_app():
    assert TestClient(app).get("/metrics").status_code == 404


def test_metrics_are_served_on_the_internal_port():
    server = observability.start_metrics_server(0)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics") as response:
            assert b"rm_requests_total" in response.read()
    finally:
        server.shutdown()


def test_prometheus_scrapes_the_internal_port():
    config = yaml.safe_load((ROOT / "deploy/prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["gateway"]["static_configs"][0]["targets"] == ["gateway:9100"]


def test_metrics_port_is_never_published():
    published = " ".join(str(p) for s in compose["services"].values() for p in s.get("ports", []))
    assert "9100" not in published


# ---- log export ----
class CollectingExporter:
    def __init__(self):
        self.records = []

    def export(self, batch):
        self.records.extend(batch)
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=None):
        return True


@pytest.fixture
def exported():
    exporter = CollectingExporter()
    handler = observability.log_export_handler(exporter, SimpleLogRecordProcessor)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.INFO)
    yield exporter
    root.removeHandler(handler)
    root.setLevel(previous)


def _bodies(exporter):
    return [str(r.log_record.body) for r in exporter.records]


def test_logs_reach_the_exporter_with_the_service_name_dashboards_query(exported):
    logging.getLogger("app.test").warning("something happened")
    [record] = exported.records
    assert record.log_record.body == "something happened"
    assert record.log_record.severity_text in ("WARN", "WARNING")
    assert record.resource.attributes["service.name"] == "ai-platform-gateway"


def test_exporter_failures_are_not_exported_back_into_itself(exported):
    logging.getLogger("opentelemetry.exporter.otlp").error("could not reach the collector")
    assert exported.records == []


def test_dashboard_queries_match_what_is_exported():
    dashboard = json.loads((ROOT / "deploy/grafana/provisioning/dashboards/diagnostics.json").read_text(encoding="utf-8"))
    queries = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", []) if "service_name" in t.get("expr", "")]
    assert queries and all('{service_name="ai-platform-gateway"}' in q for q in queries)
    assert not any("levelname" in q for q in queries)


def test_collector_sends_logs_to_lokis_otlp_endpoint():
    config = yaml.safe_load((ROOT / "deploy/otel-collector.yaml").read_text())
    assert "loki" not in config["exporters"], "the collector no longer ships a loki exporter"
    assert config["exporters"]["otlphttp/loki"]["endpoint"] == "http://loki:3100/otlp"
    assert config["service"]["pipelines"]["logs"]["exporters"] == ["otlphttp/loki"]


def test_collector_telemetry_uses_the_current_setting():
    telemetry = yaml.safe_load((ROOT / "deploy/otel-collector.yaml").read_text())["service"]["telemetry"]
    assert "address" not in telemetry["metrics"]
    assert telemetry["metrics"]["readers"][0]["pull"]["exporter"]["prometheus"]["port"] == 8888


# ---- alerts ----
def test_alert_rules_only_use_metrics_that_exist():
    rules = yaml.safe_load((ROOT / "deploy/prometheus-rules.yml").read_text())
    known = {m.name for m in REGISTRY.collect()}
    known |= {f"{name}_total" for name in known} | {f"{name}_bucket" for name in known}
    external = {"up", "vllm:num_requests_waiting", "vllm:gpu_cache_usage_perc"}
    alerts = [rule for group in rules["groups"] for rule in group["rules"]]
    assert {"GatewayDown", "ModelServerDown", "AccessDenialSpike", "QuotaExhausted", "ModelErrors", "ModelBacklog"} <= {
        a["alert"] for a in alerts
    }
    for alert in alerts:
        assert alert["annotations"]["summary"], alert["alert"]
        used = set(re.findall(r"(?<![\w:])([a-z_]+(?::[a-z_]+)?)(?=[\[{\s)]|$)", alert["expr"]))
        used = {u for u in used if u.endswith(("_total", "_bucket")) or ":" in u or u == "up"}
        assert used and used <= known | external, (alert["alert"], used - known - external)


def test_prometheus_loads_the_rules():
    config = yaml.safe_load((ROOT / "deploy/prometheus.yml").read_text())
    assert config["rule_files"] == ["/etc/prometheus/rules.yml"]
    assert any("prometheus-rules.yml:/etc/prometheus/rules.yml" in v for v in compose["services"]["prometheus"]["volumes"])


# ---- compose ----
def test_every_image_is_pinned():
    for name, service in compose["services"].items():
        image = service.get("image")
        if image:
            assert ":" in image and not image.endswith(":latest"), name


def test_only_the_edge_proxy_is_public():
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            if name == "caddy":
                assert str(port).split(":")[0] in ("80", "443")
            else:
                assert str(port).startswith("127.0.0.1:"), (name, port)


def test_services_wait_for_what_they_need():
    gateway = compose["services"]["gateway"]["depends_on"]
    for dependency in ("postgres", "redis", "minio"):
        assert gateway[dependency]["condition"] == "service_healthy"
        assert "healthcheck" in compose["services"][dependency]
    assert "healthcheck" in compose["services"]["gateway"]
    # The model can take many minutes to load and shouldn't hold everything else back.
    assert "vllm" not in gateway


def test_proxy_only_starts_when_asked_for():
    assert compose["services"]["caddy"]["profiles"] == ["edge"]


def test_required_secrets_have_no_insecure_default():
    assert "GRAFANA_PASSWORD:?" in compose["services"]["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"]
    assert "POSTGRES_PASSWORD:?" in compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert "MINIO_SECRET_KEY:?" in compose["services"]["minio"]["environment"]["MINIO_ROOT_PASSWORD"]


# ---- Dockerfile ----
def _names(requirements):
    return {re.split(r"[\[<>=!~ ]", r.strip().strip('"'))[0].lower() for r in requirements}


def test_image_installs_everything_the_project_declares():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    dockerfile = (ROOT / "Dockerfile").read_text()
    installed = _names(re.findall(r'"([^"]+)"', dockerfile))
    assert _names(declared) <= installed, _names(declared) - installed


def test_image_contains_what_the_app_needs_at_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text()
    for path in ("app", "scripts", "migrations", "alembic.ini"):
        assert f"COPY {path} " in dockerfile
    assert "USER " in dockerfile, "the app should not run as root"


# ---- generating the server's settings ----
def _generate(tmp_path, **overrides):
    path = tmp_path / ".env"
    args = {"model": "org/model", "domain": "chat.example.com", "grafana_domain": "ops.example.com", **overrides}
    generate_env.write_env(path, SimpleNamespace(
        hf_token=args.get("hf_token", ""), vllm_args=args.get("vllm_args", "--quantization awq"),
        backup_dest=args.get("backup_dest", ""), model=args["model"],
        domain=args["domain"], grafana_domain=args["grafana_domain"],
    ))
    return path


def _parse(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def test_generated_settings_pass_every_production_check(tmp_path):
    from app.config import Settings, secret_problems

    settings = Settings(_env_file=_generate(tmp_path))
    assert settings.environment == "production"
    assert secret_problems(settings) == []


def test_database_password_matches_in_both_places(tmp_path):
    values = _parse(_generate(tmp_path))
    assert f":{values['POSTGRES_PASSWORD']}@" in values["POSTGRES_DSN"]


def test_every_run_generates_different_secrets(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = _parse(_generate(tmp_path / "a")), _parse(_generate(tmp_path / "b"))
    for name in ("JWT_SECRET", "VAULT_MASTER_KEY", "POSTGRES_PASSWORD", "MINIO_SECRET_KEY", "GRAFANA_PASSWORD"):
        assert a[name] != b[name], name


def test_generator_will_not_overwrite_existing_secrets(tmp_path):
    path = _generate(tmp_path)
    with pytest.raises(SystemExit):
        generate_env.main(["--model", "x/y", "--domain", "a", "--grafana-domain", "b", "--path", str(path)])


def test_generated_file_sets_everything_compose_requires(tmp_path):
    values = _parse(_generate(tmp_path))
    required = set(re.findall(r"\$\{([A-Z_]+):\?", (ROOT / "docker-compose.yml").read_text()))
    assert required and required <= set(values), required - set(values)


# ---- backup scripts ----
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _fake_commands(tmp_path, log):
    """Stand-ins for docker and rclone that record how they were called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        f'echo "docker $*" >> "{log.as_posix()}"\n'
        'case "$*" in\n'
        '  *pg_dump*) echo "FAKE DATABASE DUMP" ;;\n'
        '  *"tar czf"*) for a in "$@"; do case "$a" in *:/backup) d="${a%%:/backup}";; esac; done; echo FILES > "$d/minio.tar.gz" ;;\n'
        '  *"compose ps"*) echo "" ;;\n'
        "esac\n"
    )
    (bin_dir / "rclone").write_text(f'#!/bin/bash\necho "rclone $*" >> "{log.as_posix()}"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def _run_backup(tmp_path, dest=""):
    log = tmp_path / "calls.log"
    log.touch()
    bin_dir = _fake_commands(tmp_path, log)
    work = tmp_path / "project"
    work.mkdir(exist_ok=True)
    (work / ".env").write_text(f"BACKUP_DEST={dest}\nBACKUP_KEEP_DAYS=7\n")
    backups = tmp_path / "backups"
    env = {"PATH": f"{bin_dir.as_posix()}:/usr/bin:/bin", "BACKUP_DIR": backups.as_posix(), "HOME": tmp_path.as_posix()}
    result = subprocess.run(
        [BASH, (ROOT / "scripts/backup.sh").as_posix()], cwd=work, env=env, capture_output=True, text=True
    )
    return result, backups, log.read_text()


@needs_bash
def test_backup_captures_database_and_files(tmp_path):
    result, backups, calls = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    archives = list(backups.glob("ai-platform-*.tar"))
    assert len(archives) == 1
    assert "pg_dump" in calls and "minio-data" in calls
    assert not [p for p in backups.iterdir() if p.is_dir()], "working folder should be cleaned up"


@needs_bash
def test_backup_warns_loudly_when_it_stays_on_the_server(tmp_path):
    result, _, _ = _run_backup(tmp_path)
    assert "NOT off this server" in result.stdout + result.stderr


@needs_bash
def test_backup_is_copied_off_the_server_when_configured(tmp_path):
    result, _, calls = _run_backup(tmp_path, dest="remote:ai-platform")
    assert result.returncode == 0, result.stderr
    assert "rclone copy" in calls and "remote:ai-platform" in calls
    assert "NOT off this server" not in result.stdout + result.stderr


@needs_bash
def test_scripts_have_valid_syntax():
    for script in ("backup.sh", "restore.sh"):
        result = subprocess.run([BASH, "-n", (ROOT / "scripts" / script).as_posix()], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


@needs_bash
def test_restore_refuses_without_confirmation_and_changes_nothing(tmp_path):
    result, backups, _ = _run_backup(tmp_path)
    archive = next(backups.glob("ai-platform-*.tar"))
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {"PATH": f"{(tmp_path / 'bin').as_posix()}:/usr/bin:/bin", "HOME": tmp_path.as_posix()}
    refused = subprocess.run(
        [BASH, (ROOT / "scripts/restore.sh").as_posix(), archive.as_posix()],
        cwd=tmp_path / "project", env=env, input="no\n", capture_output=True, text=True,
    )
    assert refused.returncode != 0
    assert log.read_text() == ""


@needs_bash
def test_restore_rejects_a_damaged_backup(tmp_path):
    result, backups, _ = _run_backup(tmp_path)
    archive = next(backups.glob("ai-platform-*.tar"))
    archive.write_bytes(b"corrupt")
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {"PATH": f"{(tmp_path / 'bin').as_posix()}:/usr/bin:/bin", "HOME": tmp_path.as_posix()}
    refused = subprocess.run(
        [BASH, (ROOT / "scripts/restore.sh").as_posix(), archive.as_posix()],
        cwd=tmp_path / "project", env=env, input="RESTORE\n", capture_output=True, text=True,
    )
    assert refused.returncode != 0 and log.read_text() == ""


# ---- web proxy ----
def test_proxy_config_hides_internal_routes_and_passes_both_sites():
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text()
    assert "{$DOMAIN}" in caddyfile and "{$GRAFANA_DOMAIN}" in caddyfile
    assert "gateway:8000" in caddyfile and "grafana:3000" in caddyfile
    assert "/readyz" in caddyfile


def test_systemd_units_exist_and_reference_the_scripts():
    units = ROOT / "deploy/systemd"
    assert "docker compose" in (units / "ai-platform.service").read_text()
    assert "scripts/backup.sh" in (units / "ai-platform-backup.service").read_text()
    assert "OnCalendar" in (units / "ai-platform-backup.timer").read_text()
