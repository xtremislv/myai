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
| **account** | accounts data, balance sheets, finance mail, orders/bills/bookings (payments approval-gated) | `accounts:*`, `balance_sheet:*`, `mail:finance/*`, `order/bill/booking:*` |
| **hr** | review career mail and NSFW reports | `mail:career/*`, `report:nsfw/*` |
| **logistics** | manage inventory, track shipments, notify, remind | `inventory:*` (write), `shipments:*` |
| **researcher** | gather info, summarize, build reports | `research:*`, `reports:*` |
| **superuser** | manage inventory | `inventory:*` (write) |
| **admin** | manage users, policies, secrets — **everything except** chat history & personal data | all except `chat_history:*`, `personal_info:*`, `mail:personal/*` |

**Data that belongs to you, not a department** (owner-only — *only you* can read it, not even admin):
your **personal info** (`personal_info:<you>/*`), **personal mail** (`mail:personal/<you>/*`), and
**chat history** (`chat_history:<you>/*`). In chat history, text is kept but non-text attachments are
**deleted after 3 days**.

**Inventory** is readable by **everyone**, but only **logistics** and **superuser** can edit it.

If you request something outside your grants, you get **403 Forbidden** with the reason — expected, not a
bug. Two requests are handled specially: anything that **places an order / buys a product / books a
service / is a bill** is **forwarded to the accounts inbox** (returns `forwarded`); anything **explicit or
illegal** is **blocked and reported to HR + admin** (returns `flagged_nsfw`).

---

## 2. Get a token (log in)

An admin creates your user first (see §6). Then:

```bash
TOKEN=$(curl -s $BASE/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"your-password"}' | jq -r .access_token)

echo "$TOKEN"   # a JWT; it expires (default 1h) — log in again to refresh
```

**Replace your initial password first.** The admin chose it, so private chat stays off until you set your
own (the login response has `"must_change_password": true`). New passwords need at least 12 characters:

```bash
TOKEN=$(curl -s $BASE/v1/auth/change-password \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"old_password":"from-the-admin","new_password":"your-own-long-password"}' | jq -r .access_token)
```

> **Your password protects your private chats, and nobody can recover it.** Your chat key can only be
> unlocked with it. If you lose it, those chats cannot be read by anyone, including you and the admin.

Send the token on every request as `Authorization: Bearer $TOKEN`.

---

## 3. Send a request

There are two entrypoints:

- **`POST /v1/request`** — the **governed** path (recommended). The Request Manager classifies your
  request to a department, enforces your department's access + quota, and pauses **critical** actions for
  approval. Body is the same as `/v1/chat` plus an optional `"department"` hint.
- **`POST /v1/chat`** — your **private** conversation with the AI. See "Private chat" below.

```bash
curl -s $BASE/v1/request \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"log-1","mode":"single","message":"Track shipment SHP-8842."}'
```
Response: `{"outcome":"ok","department":"logistics","reply":"…"}`.
Outcomes: `ok`, `error`, `denied_auth` (403), `denied_quota` (429), `pending_approval` (`approval_id`),
`forwarded` (order/bill/service → `task_id` in the accounts inbox), `flagged_nsfw` (`report_id`).

### Private chat (`POST /v1/chat`)

This is a conversation between you and the AI. No user, department or admin has an API to read it,
and it stays separate from your `/v1/request` conversations, even with the same `thread_id`. It has no
access to department tools or data, so use `/v1/request` for work that needs them.

The text is encrypted in the database with a key only your password unlocks, and it is kept indefinitely.
Attachments are deleted after 3 days. While a message is being answered, the server handles it unencrypted.

The one exception: a message classified as **explicit or illegal** is blocked and that single message is
reported to HR and the admin (`"outcome":"flagged_nsfw"`). The rest of the conversation is not shared.

Pick a `mode`:
- `single` — one agent + tools, fast; good for direct questions/actions.
- `team` — planner → researcher → writer; good for multi-step tasks.

`thread_id` ties messages into one conversation (the agent remembers earlier turns via checkpoints).
Thread ids are private to you: another user sending the same `thread_id` gets a separate conversation.
It can be up to 128 characters.

```bash
curl -s $BASE/v1/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
        "thread_id": "alice-2026-10-31-1",
        "mode": "team",
        "message": "Help me plan a talk on supply chain basics."
      }'
```

Response:
```json
{ "thread_id": "alice-2026-10-31-1", "reply": "…", "mode": "team", "outcome": "ok", "reason": "" }
```

Continue the same conversation by reusing the `thread_id`:
```bash
curl -s $BASE/v1/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"alice-2026-10-31-1","mode":"team","message":"Now shorten that to five minutes."}'
```

### Python
```python
import requests

BASE = "https://api.yourdomain.com"
tok = requests.post(f"{BASE}/v1/auth/login",
                    json={"username": "alice", "password": "your-password"}).json()["access_token"]

r = requests.post(f"{BASE}/v1/request",
                  headers={"authorization": f"Bearer {tok}"},
                  json={"thread_id": "alice-1", "mode": "team",
                        "message": "List documents under reports/ and summarize q3."})
print(r.json()["reply"])
```

---

## 4. Department examples

**Account — balances, finance mail, orders/bills**
```bash
curl -s $BASE/v1/request -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"acc-1","mode":"single","message":"Look up the balance sheet for the ACME account."}'
```
> When the agent tries to make a payment, record a bill, change an order or book a service, the run
> **pauses** and returns
> `pending_approval`; nothing executes until an admin approves (see §5). Placing an order or paying a
> bill is **forwarded to the accounts inbox**, and the same approval applies when accounts processes it.

**Logistics — tracking, reminders, inventory**
```bash
curl -s $BASE/v1/request -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"log-1","mode":"single",
       "message":"Track shipment SHP-8842 and remind the warehouse if it slips past tomorrow."}'
```

**Researcher — gather & summarize**
```bash
curl -s $BASE/v1/request -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"thread_id":"res-1","mode":"team",
       "message":"Compare our last three quarterly reports and highlight trends."}'
```

---

## 5. Approvals (critical actions)

When an agent calls a **critical tool** (`initiate_payment`, `manage_bill`, `manage_order` or
`book_service`), the run pauses just before
the tool executes, and `/v1/request` returns `{"outcome":"pending_approval","approval_id":...}`. This
depends on what the agent actually tries to do, not on the wording of your message, and it applies to
every department, including admin. An admin reviews the exact tool and arguments, then decides:

```bash
# list pending approvals (approver/admin)
curl -s $BASE/v1/approvals -H "authorization: Bearer $TOKEN"

# approve (runs that one call, then resumes the agent) or deny (the agent is told it was rejected)
curl -s -X POST $BASE/v1/approvals/<id>/approve -H "authorization: Bearer $TOKEN"
curl -s -X POST $BASE/v1/approvals/<id>/deny    -H "authorization: Bearer $TOKEN"
```

The final reply comes back in the approve/deny response. If the agent goes on to another critical call,
that response is a new `pending_approval`, which needs its own decision. While a conversation is waiting,
new messages on the same `thread_id` return the same `pending_approval`; use another `thread_id` for
other work. A resume that fails is marked `failed` and is not retried, because the tool may already have
taken effect, so check before submitting it again.

---

## 5b. Department inbox & reports

Orders/products/services/bills are forwarded to the **accounts inbox**. Accounts members (or admin)
work the queue:
```bash
curl -s "$BASE/v1/inbox?department=account" -H "authorization: Bearer $TOKEN"   # list queued tasks
curl -s -X POST $BASE/v1/inbox/<task_id>/process -H "authorization: Bearer $TOKEN"  # run it
```
Processing can return `pending_approval` like any other request (see §5).

Reports show what your role is notified about:
- **hr** and **admin**: explicit or illegal messages that were flagged (`nsfw`).
- **account**: the admin's finance activity (`finance_notice`): requests in the account department,
  finance tool calls and approval decisions. Private chat and personal data never create a notice.

```bash
curl -s $BASE/v1/reports -H "authorization: Bearer $TOKEN"
```

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
