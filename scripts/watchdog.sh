#!/usr/bin/env bash
# Keep the platform up on a host with no systemd or cron, and email the admin the public links
# whenever they change. start_native.sh starts this in the background (it shows as "watchdog");
# /etc/rc.local runs start_native.sh when the container starts (docs/SERVER_SETUP.md, "Auto-start").
#
#   scripts/watchdog.sh           # run forever
#   scripts/watchdog.sh --once    # one round, e.g. to test
#
# Every INTERVAL seconds:
#   1. anything `start_native.sh status` reports down is started again;
#   2. quick tunnels: each public link is fetched through Cloudflare. A tunnel whose link has failed
#      for DEAD_AFTER seconds is restarted (it gets a new link): the process can live on while
#      Cloudflare has forgotten it ("Tunnel not found"). Named tunnel (own domain): restarted when
#      cloudflared has had no edge connection for DEAD_AFTER seconds; its links never change;
#   3. after a container start (/etc/rc.local leaves $LOG_DIR/boot.pending): once every service is up
#      and the links work, the admin gets one "back up after a restart" email with the links; if
#      something is still down after BOOT_GRACE seconds, the email goes anyway and names it;
#   4. when both links work and differ from the last ones emailed, they're emailed.
#   Mail goes through scripts/notify_links.py, from the `mail/notify` mailbox only; a failed send is
#   retried next round.
set -uo pipefail

cd "$(dirname "$0")/.."
LOG_DIR="${LOG_DIR:-/var/log/ai-platform}"
INTERVAL="${WATCHDOG_INTERVAL:-60}"
DEAD_AFTER="${WATCHDOG_DEAD_AFTER:-600}"
DEAD_AFTER_BOOT="${WATCHDOG_BOOT_GRACE:-900}"
SENT="$LOG_DIR/links.sent"
BOOTED="$LOG_DIR/boot.pending"
PY=.venv/bin/python

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') $*"; }
env_value() { grep -m1 "^$1=" .env | cut -d= -f2-; }
source scripts/public_urls.sh
# Anything below 500 came from our own server; Cloudflare answers 5xx (530, 502...) when it can't reach it.
reachable() { local code; code=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$1"); [[ "$code" =~ ^[234] ]]; }

declare -A failing_since=()

check_tunnel() { # name, url getter, path: 0 if its link works
  local name=$1 url now
  url=$($2)
  if [[ "$name" == tunnel ]] && tunnel_ready || { [[ "$name" != tunnel && -n "$url" ]] && reachable "$url$3"; }; then
    unset "failing_since[$name]"
    return 0
  fi
  now=$(date +%s)
  : "${failing_since[$name]:=$now}"
  if (( now - failing_since[$name] >= DEAD_AFTER )); then
    log "$name: ${url:-no link} unreachable for $(( now - failing_since[$name] ))s; restarting it"
    [[ -f "$LOG_DIR/$name.pid" ]] && kill "$(cat "$LOG_DIR/$name.pid")" 2>/dev/null
    rm -f "$LOG_DIR/$name.pid"
    unset "failing_since[$name]"
    scripts/start_native.sh >/dev/null 2>&1
  fi
  return 1
}

boot_notice() { # app, grafana, services down at the start of this round
  local waited since note=""
  since=$(cat "$BOOTED")
  waited=$(( $(date +%s) - $(stat -c %Y "$BOOTED") ))
  if [[ -n "$3" ]] || ! curl -sf -m 10 http://127.0.0.1:8000/readyz >/dev/null; then
    (( waited < DEAD_AFTER_BOOT )) && return 0  # still starting (vLLM takes ~2 min): look again next round
    note="Still not up after $(( waited / 60 )) minutes: ${3:-the gateway is not ready (/readyz)}. The watchdog keeps trying."
  fi
  if "$PY" -m scripts.notify_links "$1" "$2" --restarted "$since" --note "$note"; then
    rm -f "$BOOTED"
    echo "$1 $2" > "$SENT"
    log "restart emailed: $1 $2 ${note:+($note)}"
  fi
}

round() {
  local down app grafana
  down=$(scripts/start_native.sh status 2>/dev/null | awk '$1 == "down" {print $2}' | tr '\n' ' ')
  if [[ -n "$down" ]]; then
    log "down: $down- starting"
    scripts/start_native.sh >/dev/null 2>&1
  fi

  local links_ok=1
  if named_tunnel; then
    check_tunnel tunnel app_url || links_ok=0
  else
    check_tunnel app-tunnel app_url /ui/ || links_ok=0
    (( links_ok )) && { check_tunnel grafana-tunnel grafana_url / || links_ok=0; }
  fi
  app="$(app_url)/ui/"
  grafana="$(grafana_url)/"
  if [[ -f "$BOOTED" ]]; then
    (( links_ok )) || down="${down}public link (Cloudflare tunnel) "
    boot_notice "$app" "$grafana" "$down"
    return 0
  fi
  (( links_ok )) || return 0
  [[ "$app $grafana" == "$(cat "$SENT" 2>/dev/null)" ]] && return 0
  if "$PY" -m scripts.notify_links "$app" "$grafana"; then
    echo "$app $grafana" > "$SENT"
    log "links emailed: $app $grafana"
  fi
}

log "watchdog started (every ${INTERVAL}s)"
if [[ "${1:-}" == "--once" ]]; then round; exit 0; fi
while true; do
  sleep "$INTERVAL"  # first: start_native.sh, which started us, is still finishing its own start
  round
done
