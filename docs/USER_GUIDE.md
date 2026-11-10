# User Guide — sending requests to the AI Platform

The web app at `$BASE/ui/` opens on one chat box. Ask it anything. It works out for itself whether a message
is a private conversation or work for one of your departments. A reply that was handled as work names the
department; anything else stayed private. Approvals, the inbox, reports, the overview and your account are in
the sidebar. Below is the API it uses.

How to authenticate and send requests to the platform's API. For hosting/setup see
**SERVER_SETUP.md**; for what's built vs. planned see **PROGRESS_AND_ROADMAP.md**.

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

The first response also has a `recovery_code`. **Save it somewhere safe: it is shown once.** Your chat key
can only be unlocked with your password or this code. Anyone holding the code can read your private
chats, and if you lose both, nobody can read them again, including you and the admin.

```bash
# forgot your password: this sets a new one and returns a new code (the old code stops working)
curl -s $BASE/v1/auth/recover -H 'content-type: application/json' \
  -d '{"username":"alice","recovery_code":"ABCD-EFGH-...","new_password":"a-new-long-password"}'

# lost your code but know your password: this replaces it
curl -s $BASE/v1/auth/recovery-code -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"password":"your-own-long-password"}'
```

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

### The chat box (`POST /v1/chat`)

Each message is sorted by the AI. If it is unsure, the message stays private.

- **Private** (questions, writing help, personal matters): everything in this section applies.
- **Work** (using or changing company data or systems): it runs as a `/v1/request` in the department that
  fits, with that department's tools, quotas, audit log and approvals. The department can see the work. The
  response has `"route":"work"` and the `department`. Progress is at `GET /v1/chat/threads/<id>/requests`.
  Attachments are only read in private messages.

You can belong to several departments, and the AI picks the one that fits each request.

This is a conversation between you and the AI. No user, department or admin has an API to read it,
and it stays separate from your `/v1/request` conversations, even with the same `thread_id`. It has no
access to department tools or data, so use `/v1/request` for work that needs them. It can use the web
(read-only) and **your own** mailbox, files and reminders: see "Your mail, files and reminders" below.

The text is encrypted in the database with a key only your password unlocks, and it is kept indefinitely.
While a message is being answered, the server handles it unencrypted.

**Attachments** belong to one chat, and only you can get them. They are encrypted with the same key and
deleted after 3 days. The limit is 10 MB per file.

```bash
curl -s $BASE/v1/chat/attachments -H "authorization: Bearer $TOKEN" \
  -F thread_id=alice-1 -F file=@notes.pdf            # upload
curl -s "$BASE/v1/chat/attachments?thread_id=alice-1" -H "authorization: Bearer $TOKEN"   # list
curl -s $BASE/v1/chat/attachments/<id> -H "authorization: Bearer $TOKEN" -o notes.pdf     # download
curl -s -X DELETE $BASE/v1/chat/attachments/<id> -H "authorization: Bearer $TOKEN"        # delete
```

While a file exists, the AI can read it in every message of that chat if it is text or a PDF. It cannot read
other kinds of file yet, such as images. Replies it writes about a file are chat text, so they are kept after
the file is deleted. A readable file that is explicit or illegal is rejected, and its name and an excerpt are
reported to HR and the admin. Files the AI cannot read, such as images, are not checked.

Your past chats are at `GET /v1/chat/threads` and `GET /v1/chat/threads/<thread_id>/messages`.

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

### Your mail, files and reminders

In a private chat the assistant can work with things that are yours alone:

- **Your mailbox.** Connect it under *My account > Mail and reminders*: **Sign in with Google** (Gmail,
  Google Workspace) or **Sign in with Microsoft** (Outlook, Hotmail, Microsoft 365), or an app password for
  Gmail and other providers. Then ask "what's new in my mail?", "anything I
  need to reply to?", "summarize my unread email and remind me of deadlines". The assistant reads, searches
  and marks mail, and drafts replies. **Sending always shows you the exact email with a Send button first.**
  The login is stored in the vault; the admin can't see or change it.
- **Your files** (*My files*). Upload files, or ask: "write a packing list to notes/trip.md", "add milk to my
  groceries list", "rename it", "undo that change". Text, Markdown, CSV and JSON can be edited; PDFs can be
  read. Files are encrypted with your key, so only you (and the assistant in your private chats) can open them.
  Every change keeps the previous version (5 are kept), and `restore_file` / "undo" brings it back.
  **Deleting asks you to confirm**, then the file sits in the trash for 7 days. Space: 500 MB per person.
- **Reminders** (*Reminders*). "Remind me tomorrow at 9 to call the bank", "every weekday at 6pm remind me
  to log my hours", "what reminders do I have?", "cancel the bank one". When one is due it appears under
  *Notifications* (and as a browser pop-up if you allowed it), and by email if you asked for that.
- **Morning mail digest.** Switch it on under *My account* and pick an hour. Each morning the assistant reads
  your new mail, sends you a summary (urgent and actionable mail first, newsletters folded into one line),
  and sets a reminder for each deadline it finds: the morning before a date, or an hour before a time.

At work (`/v1/request`, or chat messages sorted as work) the same file tools act on your department's shared
folder (`shared/<department>/`, plus areas like `research/` your department can reach), deleting needs an
approver, and reminders you set go to you.

Reminder and notification text is encrypted with a server key in the vault (not your personal key), because
the server must read a reminder when it falls due while you're signed out.

```bash
curl -s $BASE/v1/reminders -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"when":"tomorrow 9am","text":"Call the bank","repeat":"none","email":false}'
curl -s $BASE/v1/reminders -H "authorization: Bearer $TOKEN"                 # waiting reminders
curl -s $BASE/v1/notifications?unread_only=true -H "authorization: Bearer $TOKEN"
curl -s $BASE/v1/files -H "authorization: Bearer $TOKEN" -F folder=notes -F file=@todo.md   # upload
```
**Documents from templates.** Give the assistant a template and the data, and it makes the document in the
same format (Word stays Word):
- Attach a template to a private chat, or keep it in *My files* under `templates/`: "make a rent receipt
  from my template for Ravi, October, 18000". At work, use company templates (*Templates* page): "make offer
  letters from the Offer letter template for these three people".
- Mark what to fill with `{{ name }}`, `{{ joining_date }}`; in Word, a table row using `{{ item.description }}`
  repeats once per line item. Missing values are asked for, never invented.
- A document without placeholders works as an example: "make a similar certificate for Vikram from the
  attached one". The new document keeps the layout and the issuer's details, takes every fact about the new
  person from you, and marks anything it doesn't know as `[TO FILL: ...]`.
- Many at once: give a list, or a CSV in your files ("one letter per row of people.csv, named by name").

**Shopping.** Ask "compare earbuds under 1500 on Flipkart" or "order me a Pixel 9 cover on Amazon". The
assistant searches shops, compares price, rating, reviews and delivery, and asks whether the order is for
**personal use or work**. Work orders go to the Accounts team. For personal orders it signs in and pays with your
**Saved details** (My account): logins, cards, UPI IDs and addresses, encrypted with your key so only you can see
them (showing one needs your password again). It fills them in without seeing them, and only on the right site.
For your card's CVV, an OTP, or a password you haven't saved, a **secure box** appears in the chat: what you type
goes straight to the shop's page and isn't kept. Before anything is bought you get a card with the exact order and
the buttons "Personal: place it" or "Work: send to Accounts". For CAPTCHAs or app approvals, a live browser
appears where you click and type yourself.

Shop sign-ins made in the live browser are kept, encrypted with your key; "Sign out & close" forgets them. The
globe button next to the message box opens the live browser any time. Orders for the company go to Accounts, and
an approver confirms each one.

**Mail.** Under My account, choose one:
- **Sign in with Google**: a Google page opens; choose your account and allow mail access. Google then sends
  you to a page that doesn't load (`http://127.0.0.1:8765/...`). That's expected: copy the whole address from
  the address bar and paste it into the box in the app.
- **Sign in with Microsoft**: the app shows a short code; open microsoft.com/devicelogin, type it and sign in.
  The app notices by itself.
- **App password** (Gmail without sign-in, Yahoo, Zoho, iCloud): your address and an app password (Gmail: turn
  on 2-Step Verification, create one at myaccount.google.com/apppasswords, and turn IMAP on in Gmail settings).
  Outlook can't use a password.

**Data and dashboards.** Attach a CSV (or point to one in your files) and ask "analyse this: revenue by region
and month, which is growing fastest?". The assistant works the numbers out with Python (pandas) and shows a
**dashboard** under its reply: headline numbers, charts (hover for values; "Show the numbers" for the table),
tables with *Download CSV*, and a few takeaways. It works with numbers from mail, the web or earlier replies too
("make a dashboard of these prices"). Dashboards stay with the chat (encrypted like it) and go when the chat is
deleted; ask it to save the results to a file if you want to keep them elsewhere. The code runs with no internet
and sees only copies of the files you name.

Replies are formatted (headings, lists, tables, links).

When a private reply needs your go-ahead, `/v1/chat` answers `"outcome":"confirm"` with a `confirm` object
(the exact email or file). Answer with `POST /v1/chat/threads/<id>/confirm {"approve": true|false}`;
`GET /v1/chat/threads/<id>/pending` shows it again. Sending a new message instead counts as "no".

### Big files
Ask the assistant to summarize a file or attachment of any size (PDF, Word, text, CSV, Excel), for example
"summarize the July ledger, focus on unpaid invoices" or "put every unpaid invoice from ledger.xlsx in a
sheet". It reads it part by part. Short answers come in the chat; long ones are saved to your Files as a
Word document (`Summaries/...docx`); ask for a sheet to get an Excel table. Large files take a few minutes.

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

## 6b. The Android app

The TW-ai app for Android (see `docs/ANDROID_APP.md`) signs in once and stays signed in, with reminders, finished
replies and approvals as phone notifications. Its sign-in endpoints:

```bash
# sign a phone in: an access token plus a refresh token for this device
curl -s $BASE/v1/auth/device/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"...","name":"Pixel 8","push_token":"<FCM token, optional>"}'
# a fresh access token (the refresh token is replaced: keep the new one)
curl -s $BASE/v1/auth/device/refresh -H 'content-type: application/json' \
  -d '{"device_id":"...","refresh_token":"..."}'
curl -s $BASE/v1/devices -H "authorization: Bearer $TOKEN"                    # your signed-in phones
curl -s -X DELETE $BASE/v1/devices/<id> -H "authorization: Bearer $TOKEN"     # sign one out
curl -s $BASE/v1/app/config                                                   # {"push_enabled": ...}
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
followed step by step. See **SERVER_SETUP.md §6** for how to reach Grafana securely.
