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
| A100 80GB | 80 GB | No | Plenty of room |
| H100 80GB | 80 GB | Yes | Fastest |
| RTX 4090 | 24 GB | Yes | Tight: fewer people at once |
| RTX 3090 | 24 GB | No | Tight: fewer people at once |

Billing is per minute, and terminating can wipe the disk. Set up backups (step 9) before relying on it.

```bash
ssh -i ~/.ssh/id_ed25519 <user>@<server-ip> -p <ssh-port>
```

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
