# Server setup, step by step

This takes you from an empty rented GPU server to the running platform. Run the commands on the server.
Anything in `<angle brackets>` is yours to fill in.

**Have these ready:** an aiccloud.in account with balance, an SSH key, the code in a GitHub repo, the exact
Hugging Face repo id of your model, a domain name (or a Cloudflare account), and somewhere for backups.

## 1. Rent the server
On aiccloud.in, deploy a GPU with an Ubuntu 22.04 + CUDA image and at least 100 GB of disk, and add your SSH key.

| GPU | Memory | FP8? | Notes |
|---|---|---|---|
| L40S | 48 GB | Yes | Recommended |
| Quadro RTX 8000 | 48 GB | No | Older (Turing): use AWQ or GPTQ INT4 weights |
| A100 80GB | 80 GB | No | Plenty of room |
| H100 80GB | 80 GB | Yes | Fastest |
| RTX 4090 | 24 GB | Yes | Tight: fewer people at once |
| RTX 3090 | 24 GB | No | Tight: fewer people at once |

Billing is per minute, and terminating can wipe the disk. Set up backups (step 9) before relying on it.

```bash
ssh -i ~/.ssh/id_ed25519 <user>@<server-ip> -p <ssh-port>
```

**Is your server itself a container?** Many GPU rentals (aiccloud.in instances run on vast.ai) give
you a Docker container, not a machine. Check with `ls /.dockerenv`. If that file exists, Docker can't
run inside it: skip steps 2 and 6 and follow **"Without Docker"** at the end of this guide instead.

## 2. Install Docker and GPU support
```bash
nvidia-smi                                    # should list your GPU
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER                 # then log out and back in

distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi    # must list your GPU
```

## 3. Get the code
```bash
sudo mkdir -p /opt/ai-platform && sudo chown $USER /opt/ai-platform
git clone <your-repo-url> /opt/ai-platform
cd /opt/ai-platform
```

## 4. Check the model id
The name in the old docs was a placeholder. This should print the repo's details, not an error:
```bash
curl -s https://huggingface.co/api/models/<org/model-name> | head -c 300
```

## 5. Create the settings file
This creates `.env` with new random passwords and keys.
```bash
python3 scripts/generate_env.py \
  --model <org/model-name> \
  --domain <chat.yourdomain.com> \
  --grafana-domain <ops.yourdomain.com> \
  --vllm-args "<flags, see below>" \
  --hf-token "<token, only for gated models>" \
  --backup-dest "<rclone destination, see step 9>"
```

`--vllm-args` depends on your weights:
- Already quantized with AWQ: `--quantization awq`
- FP8, on L40S, RTX 4090 or H100 only: `--quantization fp8`
- Not quantized: leave it empty

**Copy `.env` off the server now and keep it safe.** Backups don't include it, and it can't be recreated.

## 6. Start everything
```bash
docker compose up -d --build
docker compose logs -f vllm        # Ctrl-C once it says the server is ready; the first download is slow
```

## 7. Create the admin and check it
```bash
docker compose exec gateway python -m scripts.init_db --admin-pass '<strong-password>'
```
It prints the admin's recovery code once. **Save it.**
```bash
curl -s localhost:8000/readyz
```
Every check should say `ok`. `model_server: unavailable` means vLLM is still loading.

## 8. Put it on the web
Pick one. Point DNS at the server first.

**A. Caddy (needs a public IP with ports 80 and 443 open).** HTTPS is automatic:
```bash
docker compose --profile edge up -d
```

**B. Cloudflare Tunnel (no open ports needed).** Install `cloudflared`, then:
```bash
cloudflared tunnel login
cloudflared tunnel create ai-platform
# route <chat.yourdomain.com> to http://localhost:8000
# route <ops.yourdomain.com> to http://localhost:3000
cloudflared tunnel run ai-platform
```

**C. SSH tunnel (just you, for testing):**
```bash
ssh -N -L 8000:localhost:8000 -L 3000:localhost:3000 <user>@<server-ip> -p <ssh-port>
```

Then open the app at your domain. Grafana is at your Grafana domain: the user is `admin`, and the password is
`GRAFANA_PASSWORD` in `.env`.

## 9. Backups and starting on boot
```bash
curl https://rclone.org/install.sh | sudo bash
rclone config                      # create the remote you named in BACKUP_DEST; use an encrypted one
sudo mkdir -p /var/backups/ai-platform && sudo chown $USER /var/backups/ai-platform
scripts/backup.sh                  # try it now

sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ai-platform.service ai-platform-backup.timer
sudo systemctl start ai-platform-backup.timer
```
If you use option B or C in step 8, remove `--profile edge` from `/etc/systemd/system/ai-platform.service`.
The backup service runs as root, so run `rclone config` as root too, or copy its config to root.

**Test a restore before trusting backups:** `scripts/restore.sh /var/backups/ai-platform/<file>.tar`

## 10. Add people
In the app, go to Users and add each person with one or more departments. They set their own password and
get their own recovery code when they first sign in.

## Everyday commands
| To do this | Run |
|---|---|
| See what's running | `docker compose ps` |
| Read a service's logs | `docker compose logs -f <service>` |
| Check health | `curl -s localhost:8000/readyz` |
| Update the code | `git pull && docker compose up -d --build` |
| Change model | Edit `LLM_MODEL` in `.env`, then `docker compose up -d vllm gateway` |
| Stop paying for the GPU | Back up, then stop or terminate the instance |

## Without Docker (container-based GPU hosts)
For a server that is itself a container (`/.dockerenv` exists, no `docker` command). Tested on a
Quadro RTX 8000 (Turing, compute capability 7.5) with vLLM 0.32.0.

**1. Install vLLM in its own environment.** It needs Python 3.11 or newer. It takes about 8 GB, plus
about 8 GB of download cache that `uv cache clean` removes afterwards.
vLLM compiles GPU kernels at startup, so it needs a C compiler. Without one it fails with
"Failed to find C compiler".
```bash
apt-get install -y gcc g++
pip install uv
uv venv -p 3.12 /root/vllm-env
VIRTUAL_ENV=/root/vllm-env uv pip install 'vllm==0.32.0'
uv cache clean
```

**2. Get INT4 weights.** The full bf16 Gemma 4 26B-A4B weights are about 52 GB, too big for a 48 GB
card. Use an AWQ or GPTQ INT4 copy (about 15 GB) and put it in a local folder such as
`/root/models/gemma-4-26b`. vLLM reads the quantization from the checkpoint's `config.json`.

**3. Create `.env` with `--native`**, so every address is `127.0.0.1` rather than a compose service name:
```bash
python3 scripts/generate_env.py --native \
  --model google/gemma-4-26B-A4B-it --model-path /root/models/gemma-4-26b \
  --vllm-args "--dtype half --enable-auto-tool-choice --tool-call-parser gemma4" \
  --domain <chat.yourdomain.com> --grafana-domain <ops.yourdomain.com>
```
The agents use native function calling, so vLLM needs `--enable-auto-tool-choice --tool-call-parser gemma4`
(another model needs its own parser name). On Turing cards **`--dtype half` is required**: vLLM refuses bfloat16 below compute capability 8.0.
Attention runs on vLLM's Triton backend, since FlashAttention needs compute capability 8.0 or newer.

**4. Start the model server:**
```bash
nohup scripts/run_vllm.sh > /root/vllm.log 2>&1 &
curl -s localhost:8001/v1/models
```
`run_vllm.sh` binds to `127.0.0.1` only and refuses `--enable-log-requests` and `--enable-log-outputs`,
since those would write private chat text to the log.

**5. Postgres 16 and Redis** (apt). Ubuntu's Redis 6.0 is enough: the app only uses `INCR`/`EXPIRE`.
```bash
install -d /usr/share/postgresql-common/pgdg
curl -s -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update && apt-get install -y postgresql-16 redis-server
echo "listen_addresses = 'localhost'" >> /etc/postgresql/16/main/postgresql.conf
pg_ctlcluster 16 main start
PGPW=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)
su postgres -c "psql -c \"CREATE ROLE ai LOGIN PASSWORD '$PGPW'\"" && su postgres -c "createdb -O ai ai_platform"
sed -i 's/^bind .*/bind 127.0.0.1 ::1/; s/^daemonize .*/daemonize yes/; s/^supervised .*/supervised no/' /etc/redis/redis.conf
```

**6. MinIO.** MinIO no longer publishes free binaries (dl.min.io returns 410), so build the release that
compose pins, from source:
```bash
mkdir -p /root/build && cd /root/build
curl -sSL https://go.dev/dl/go1.26.10.linux-amd64.tar.gz | tar -xz
git clone --depth 1 --branch RELEASE.2025-09-06T17-38-46Z https://github.com/minio/minio.git minio-src
cd minio-src && CGO_ENABLED=0 GOTOOLCHAIN=local /root/build/go/bin/go build -trimpath -o /usr/local/bin/minio .
minio --version && rm -rf /root/build /root/go          # the build cache is ~2 GB
```

**7. App environment and database:**
```bash
pip install uv && uv venv -p 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -e .
.venv/bin/python -m alembic upgrade head
```

**8. Start everything.** There is no systemd in a container, so rerun this after every restart:
```bash
scripts/start_native.sh            # SKIP_VLLM=1 to leave the model server off
scripts/start_native.sh status
```
Logs are in `/var/log/ai-platform/`. Then create the admin (step 7 above, without `docker compose exec`):
`.venv/bin/python -m scripts.init_db --admin-pass '<strong-password>'`.

**9. Access from the internet: Cloudflare quick tunnel** (no domain, no account, no open ports):
```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared jammy main' \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update && apt-get install -y cloudflared
scripts/start_native.sh            # starts the tunnel
scripts/start_native.sh status     # prints https://<random-words>.trycloudflare.com/ui/
```
Quick tunnels are meant for testing. **The address changes every time the tunnel restarts** and has no uptime
guarantee, and the login page is on the public internet. For a fixed address, add a domain to Cloudflare and
use a named tunnel (step 8, option B), ideally with Cloudflare Access in front.

**10. vLLM patch for Turing cards.** Gemma 4's 512-wide attention heads need more shared memory than Turing has
(64 KB). Apply the patch after every vLLM install or upgrade:
```bash
patch -p1 -d /root/vllm-env/lib/python3.12/site-packages < deploy/patches/vllm-0.32.0-turing-head512.patch
```

**11. Logs and dashboards.** Prometheus from Ubuntu; Grafana, Loki and Grafana Alloy from Grafana's apt
repository. Alloy stands in for the OpenTelemetry collector (no apt package): it receives the gateway's OTLP on
127.0.0.1:4317 and sends logs to Loki. Configs are in `deploy/native/`.
```bash
mkdir -p /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
apt-get update && apt-get install -y --no-install-recommends grafana=13.0.2 loki=3.7.9 alloy prometheus
scripts/start_native.sh            # also opens a second quick tunnel for Grafana
scripts/start_native.sh status     # prints both public addresses
```
| Service | Local port | Reached from outside |
|---|---|---|
| App (gateway) | 127.0.0.1:8000 | app quick tunnel, `/ui/` |
| Grafana | 127.0.0.1:3000 | dashboards quick tunnel (user `admin`, password `GRAFANA_PASSWORD`) |
| Prometheus | 127.0.0.1:9090 | no; use Grafana, or `ssh -L 9090:localhost:9090` |
| Loki | 127.0.0.1:3100 | no; use Grafana's Explore |
| Alloy (OTLP) | 127.0.0.1:4317/4318, UI :12345 | no |
| vLLM | 127.0.0.1:8001 | no |

Dashboards: *AI Platform — LLM & ACL* and *AI Platform — Diagnostics & Logs*. Prometheus keeps 15 days (max 2 GB)
and Loki 14 days of logs.

**12. Background jobs, alerts, backups.** `start_native.sh` also runs `scripts/scheduler.py`: due reminders,
morning mail digests, and cleanup of trashed files and undo copies. Prometheus sends its alerts to the gateway
(`alerting` in `deploy/native/prometheus.yml`, token `ALERT_WEBHOOK_TOKEN` in `.env`), and they appear under
the admin's *Notifications*. `GatewayDown` can't arrive that way: check Grafana for it.

Backups work natively too: `scripts/backup.sh` dumps Postgres over `POSTGRES_DSN` and archives MinIO's folder.
Prove a backup without touching live data with `scripts/restore.sh --verify <archive>` (restores into a scratch
database and folder, checks them, deletes them). Set `BACKUP_DEST` to copy backups off the server.

Rotate the vault master key with `scripts/rotate_vault_key.py` (stop the gateway and scheduler first; it
explains itself with `--help`). Everyone is signed out; nothing stored is lost.

`scripts/load_test.py` measures the model under 1-16 simultaneous chats.

### Browser for the agents
The agents' headless browser needs Chromium and its system libraries:

```bash
.venv/bin/playwright install --with-deps chromium
```

For good web search, add a Tavily or Serper API key on the admin Credentials page as `search/tavily` or
`search/serper`. Without one, search falls back to Bing, which gives weaker results to servers.

