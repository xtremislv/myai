# Public addresses of the app and Grafana, shared by start_native.sh and watchdog.sh (sourced, not run).
# Needs LOG_DIR and env_value.
#
# Two modes:
#   named tunnel  CF_TUNNEL=<name> in .env and its config at ~/.cloudflared/<name>.yml: one cloudflared
#                 process ("tunnel") serves the fixed hostnames PUBLIC_APP_URL and PUBLIC_GRAFANA_URL
#                 (e.g. https://tw-ai.in, https://grafana.tw-ai.in). Setup: docs/SERVER_SETUP.md "Own domain".
#   quick tunnels otherwise: two cloudflared processes ("app-tunnel", "grafana-tunnel") with random
#                 trycloudflare.com addresses that change on every restart.

CF_TUNNEL="$(env_value CF_TUNNEL)"
CF_CONFIG="${CF_CONFIG:-$HOME/.cloudflared/${CF_TUNNEL:-none}.yml}"
CF_METRICS="${CF_METRICS:-127.0.0.1:20260}"  # off the 20241-20245 range quick tunnels pick by default

named_tunnel() { [[ -n "$CF_TUNNEL" && -f "$CF_CONFIG" ]]; }

quick_url() { grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/$1.log" 2>/dev/null | tail -1; }

app_url() {
  if named_tunnel; then env_value PUBLIC_APP_URL | sed 's#/*$##'; else quick_url app-tunnel; fi
}

grafana_url() {
  if named_tunnel; then env_value PUBLIC_GRAFANA_URL | sed 's#/*$##'; else quick_url grafana-tunnel; fi
}

# cloudflared answers 200 on /ready once at least one connection to Cloudflare's edge is up.
# Checked locally: this host's DNS is unreliable, so fetching our own domain from here proves little.
tunnel_ready() { curl -sf -m 5 "http://$CF_METRICS/ready" >/dev/null 2>&1; }
