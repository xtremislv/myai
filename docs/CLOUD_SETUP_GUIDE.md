# Cloud Setup Guide — hosting on aiccloud.in

How to provision a rented GPU instance on **[aiccloud.in](https://aiccloud.in/)** and bring up the full
AI platform (vLLM + gateway + Postgres/Redis/MinIO + observability) on it.

> **Hardware change:** earlier docs assumed an owned RTX Quadro 8000. We now **rent** the GPU from
> aiccloud.in, so you pick a GPU at provisioning time and the FP8/quantization options change with the
> card (see §2). Everything above the GPU — the whole `docker-compose` stack — is unchanged.

---

## 1. Before you start

- An aiccloud.in account with wallet balance (billing is **per-minute**, INR via UPI/card/net-banking,
  or USD via PayPal — so **stop/terminate the instance when idle**).
- An SSH keypair (`ssh-keygen -t ed25519`) to add to the instance.
- The model weights' Hugging Face repo id, and a `HUGGING_FACE_HUB_TOKEN` if the model is gated.
- This repository.

---

## 2. Choose a GPU

aiccloud.in lists RTX 3090, RTX 4090, L4, L40S, A100 (40/80 GB), and H100. For the **26B / A4B MoE**
default (INT4 weights ≈ 13–15 GB; all experts must be resident):

| GPU | VRAM | Arch | FP8? | Fit for 26B-A4B | Notes |
|-----|------|------|------|-----------------|-------|
| **L40S** | 48 GB | Ada | ✅ | **Recommended** | Closest to the old Quadro 8000 footprint, adds FP8; great price/perf |
| A100 80GB | 80 GB | Ampere | ❌ | Excellent | Lots of KV-cache headroom; use AWQ/INT4 or BF16 (no FP8) |
| H100 80GB | 80 GB | Hopper | ✅ | Excellent | Fastest; FP8 available |
| RTX 4090 | 24 GB | Ada | ✅ | OK (tight) | INT4 weights fit; smaller KV cache / lower concurrency |
| RTX 3090 | 24 GB | Ampere | ❌ | OK (tight) | INT4 only; modest concurrency |

**Quantization by card:**
- **L40S / RTX 4090 / H100 (Ada/Hopper):** you may use `--quantization fp8` **or** AWQ/GPTQ INT4.
- **A100 / RTX 3090 (Ampere):** **no FP8** — use `--quantization awq` (INT4) or BF16.

The compose default is `--quantization awq`, which works on **every** card above. Only switch to `fp8`
if you provisioned an Ada/Hopper GPU.

---

## 3. Provision the instance

1. Log in to the aiccloud.in dashboard → **Get Started / Deploy a GPU**.
2. Select the GPU from §2 and an **Ubuntu 22.04 + CUDA** image if offered (preferred — drivers
   preinstalled). Note the vCPU/RAM/disk; give it **≥ 100 GB disk** (model weights + images + volumes).
3. Add your SSH public key.
4. Launch, then copy the instance's **SSH command / public IP and port** from the dashboard.
5. Connect:
   ```bash
   ssh -i ~/.ssh/id_ed25519 <user>@<instance-ip> -p <ssh-port>
   ```

> If the dashboard exposes a web console / Jupyter instead of raw SSH, use its terminal — the steps below
> are identical from there.

---

## 4. Prepare the host

```bash
# 1. Confirm the GPU is visible
nvidia-smi    # should list your chosen GPU

# 2. Install Docker (if not preinstalled)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # re-login to take effect

# 3. Install the NVIDIA Container Toolkit (lets containers see the GPU)
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 4. Verify containers can use the GPU
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

If step 4 lists your GPU, the host is ready.

---

## 5. Deploy the platform

```bash
# get the code onto the instance (git clone, scp, or rsync)
git clone <your-repo-url> ai-platform && cd ai-platform

# configure
cp .env.example .env
nano .env
```

Set in `.env`:
- `VAULT_MASTER_KEY` — generate: `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`
- `JWT_SECRET` — a long random string.
- `LLM_MODEL` — the exact HF repo id (default placeholder is `gemma-3-26b-a4b-it`).
- `HUGGING_FACE_HUB_TOKEN` — if the model is gated.
- Change `MINIO_*` and Postgres credentials from defaults.

```bash
# bring up the whole stack
docker compose up -d --build

# wait for vLLM to pull + load the weights (watch it)
docker compose logs -f vllm      # Ctrl-C once it reports the server is ready

# bootstrap DB, first admin, baseline policies
docker compose exec gateway python -m scripts.init_db --admin-pass '<strong-password>'

# health checks
curl -s localhost:8000/healthz
curl -s localhost:8000/readyz || true   # (added in Phase 1)
```

**FP8 option (Ada/Hopper only):** edit the `vllm` service `command` in `docker-compose.yml`, change
`--quantization awq` to `--quantization fp8`, then `docker compose up -d vllm`.

---

## 6. Expose the service securely

**Do not** put the raw ports on the public internet. Pick one:

### Option A — Cloudflare Tunnel (works even with no public port)
```bash
# install cloudflared, then:
cloudflared tunnel login
cloudflared tunnel create ai-platform
# route api.yourdomain.com -> http://localhost:8000
# route grafana.yourdomain.com -> http://localhost:3000
cloudflared tunnel run ai-platform
```
Gives HTTPS automatically and needs no inbound port on the instance.

### Option B — Reverse proxy + TLS (if aiccloud gives you a public IP + open 443)
Add a Caddy container (auto-HTTPS):
```
# Caddyfile
api.yourdomain.com   { reverse_proxy gateway:8000 }
grafana.yourdomain.com { reverse_proxy grafana:3000 }
```
Expose only 443. Keep 8001/5432/6379/9000/9090/3100 bound to the internal Docker network.

### Option C — SSH tunnel (admin-only, quickest)
```bash
ssh -N -L 8000:localhost:8000 -L 3000:localhost:3000 <user>@<instance-ip> -p <ssh-port>
```
Then reach the API at `http://localhost:8000` and Grafana at `http://localhost:3000` on your laptop.

---

## 7. Persistence, backups, and cost control

- **Volumes** (`pg-data`, `minio-data`, `grafana-data`, `hf-cache`, …) live on the instance disk. On a
  rented box, **the disk may be wiped on terminate** — treat it as ephemeral.
- **Back up off-box, nightly:**
  ```bash
  docker compose exec -T postgres pg_dump -U ai ai_platform | gzip > backup_$(date +%F).sql.gz
  # copy backup_*.sql.gz and a MinIO mirror to object storage / your laptop
  ```
- **Model cache:** `hf-cache` keeps weights so restarts don't re-download. If the instance is wiped,
  the first boot re-pulls the model (slow) — budget for it.
- **Cost:** billing is per-minute. **Stop or terminate** the instance when not serving; keep backups so
  you can re-provision quickly.

---

## 8. Verify end to end

1. `curl localhost:8000/healthz` → `{"status":"ok"}`.
2. Log in and get a token (see **USER_GUIDE.md** §2).
3. Send a `team`-mode chat request and get a reply.
4. Open Grafana → **AI Platform — Diagnostics & Logs**: gateway/vLLM "UP", node activity on your
   request, and the log panels streaming.

---

## 9. Day-2 operations

- **Roll the model:** change `LLM_MODEL` in `.env`, `docker compose up -d vllm`, watch logs.
- **Switch to Nemotron:** set the Nemotron repo id + `HUGGING_FACE_HUB_TOKEN`, restart `vllm`. No code change.
- **Rotate the vault master key:** generate a new key, call `AESGCMVault.rotate_master_key(new_b64)`
  (admin script to be added in Phase 1), then update `VAULT_MASTER_KEY`.
- **Watch spend & GPU:** `nvidia-smi`, the diagnostics dashboard (KV-cache %, requests running), and the
  aiccloud wallet.

See **USER_GUIDE.md** for how users send requests, and **PROGRESS_AND_ROADMAP.md** for what's built vs.
planned.
