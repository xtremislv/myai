# User Guide — sending requests to the AI Platform

How to authenticate and send requests to the platform's API. For hosting/setup see
**CLOUD_SETUP_GUIDE.md**; for what's built vs. planned see **PROGRESS_AND_ROADMAP.md**.

> **Base URL.** Replace `$BASE` below with your endpoint — e.g. `https://api.yourdomain.com` in
> production, or `http://localhost:8000` over an SSH/Cloudflare tunnel.

```bash
BASE=https://api.yourdomain.com
```

---

## 1. Who can do what

Every user belongs to one or more **departments** (roles), and the platform is **deny-by-default**: you
can only touch data and tools your department is granted. The departments:

| Department | Can ask agents to… | Data it can reach |
|-----------|--------------------|-------------------|
| **finance** | run/initiate transactions, manage invoices, reconcile | finance records, payment secrets (approval-gated) |
| **logistics** | track shipments, send notifications/reminders, manage inventory | inventory & shipment data |
| **research** | gather info, summarize, build reports | research & report documents |
| **misc** | general, low-privilege tasks | shared documents |
| **admin** | manage users, policies, secrets, departments | everything |

If you request something outside your department's grants, you get **403 Forbidden** with the reason —
this is expected, not a bug.

---

## 2. Get a token (log in)

An admin creates your user first (see §6). Then:

```bash
TOKEN=$(curl -s $BASE/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"your-password"}' | jq -r .access_token)

echo "$TOKEN"   # a JWT; it expires (default 1h) — log in again to refresh
```

Send the token on every request as `Authorization: Bearer $TOKEN`.

---

## 3. Send a chat/agent request

`POST /v1/chat` runs the orchestrator. Pick a `mode`:
- `single` — one agent + tools, fast; good for direct questions/actions.
- `team` — planner → researcher → writer; good for multi-step tasks.

`thread_id` ties messages into one conversation (the agent remembers earlier turns via checkpoints).

```bash
curl -s $BASE/v1/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
        "thread_id": "alice-2026-10-31-1",
        "mode": "team",
        "message": "Summarize the Q3 logistics report and list any late shipments."
      }'
```

Response:
```json
{ "thread_id": "alice-2026-10-31-1", "reply": "…", "mode": "team" }
```

Continue the same conversation by reusing the `thread_id`:
```bash
curl -s $BASE/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"alice-2026-10-31-1","mode":"team","message":"Now draft reminders for those."}'
```

### Python
```python
import requests

BASE = "https://api.yourdomain.com"
tok = requests.post(f"{BASE}/v1/auth/login",
                    json={"username": "alice", "password": "your-password"}).json()["access_token"]

r = requests.post(f"{BASE}/v1/chat",
                  headers={"authorization": f"Bearer {tok}"},
                  json={"thread_id": "alice-1", "mode": "team",
                        "message": "List documents under reports/ and summarize q3."})
print(r.json()["reply"])
```

---

## 4. Department examples

**Finance — manage an invoice / request a transaction**
```bash
curl -s $BASE/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"fin-1","mode":"single",
       "message":"Create an invoice for ACME, 12 units at 500 INR, due in 30 days."}'
```
> Transactions and critical-data access are **approval-gated**: the agent prepares the action and it
> waits for an admin/finance approver before anything executes (see §5). This is by design.

**Logistics — tracking, reminders, inventory**
```bash
curl -s $BASE/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"log-1","mode":"single",
       "message":"Track shipment SHP-8842 and remind the warehouse if it slips past tomorrow."}'
```

**Research — gather & summarize**
```bash
curl -s $BASE/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"res-1","mode":"team",
       "message":"Compare our last three quarterly reports and highlight trends."}'
```

---

## 5. Approvals (critical actions) — *planned, Phase 4*

When an agent proposes a **critical** action (e.g. a finance transaction), the request pauses and an
approval is created. Approvers act on it:

```bash
# list pending approvals (approver/admin)
curl -s $BASE/v1/approvals -H "authorization: Bearer $TOKEN"

# approve (resumes the paused agent run) or deny
curl -s -X POST $BASE/v1/approvals/<id>/approve -H "authorization: Bearer $TOKEN"
curl -s -X POST $BASE/v1/approvals/<id>/deny    -H "authorization: Bearer $TOKEN"
```

---

## 6. Admin tasks

Admins manage users, access policies, and secrets.

**Create a user and assign departments (roles):**
```bash
curl -s $BASE/v1/auth/users -H "authorization: Bearer $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"username":"alice","password":"init-pass","roles":["logistics"]}'
```

**Grant a department access to data (ACL policy):**
```bash
curl -s $BASE/v1/admin/policies -H "authorization: Bearer $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"subject":"logistics","effect":"allow","action":"read","resource":"object:inventory/*"}'
```

**Store a secret in the vault (never returned in plaintext to non-admins):**
```bash
curl -s -X PUT $BASE/v1/admin/secrets -H "authorization: Bearer $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"name":"payment_gateway_key","value":"sk-live-..."}'
```

---

## 7. Responses & errors

| Code | Meaning | What to do |
|------|---------|-----------|
| 200 | OK | use `reply` |
| 401 | Unauthorized | token missing/expired → log in again |
| 403 | Forbidden | your department lacks the grant → ask an admin, or it's intentionally blocked |
| 409 | Conflict | e.g. username already exists |
| 429 | Too Many Requests | you/your department hit a rate limit → back off and retry (*Phase 1/4*) |
| 5xx | Server error | check status with an admin; every error carries a trace id for diagnosis |

---

## 8. Seeing what happened (observability)

Admins/operators can watch requests flow through the system in **Grafana**:
- **AI Platform — LLM & ACL** — request rates, latency, tokens, allow/deny.
- **AI Platform — Diagnostics & Logs** — service health, vLLM/GPU health, per-node workflow, and live
  error/warning logs.

Each request is traced end to end (gateway → orchestrator → tools), so a failed or slow request can be
followed step by step. See **CLOUD_SETUP_GUIDE.md §6** for how to reach Grafana securely.
