# TW-ai on WhatsApp

The same assistant on WhatsApp, through Meta's official WhatsApp Cloud API (not an unofficial WhatsApp Web library,
which breaks WhatsApp's terms and gets numbers banned). Code: `app/whatsapp/` and `app/api/routes_whatsapp.py`.

## What people get

- **Linking (the allowlist):** in TW-ai, Settings > WhatsApp: enter your number, get a code, send `LINK <code>` to the
  bot from that number (a button opens WhatsApp with the message typed in). WhatsApp vouches for the sender's number,
  so only the number's owner can link it. Unlinked numbers get one "not linked" reply every 6 hours and nothing else.
- **Chat:** the same triage as the app. Work goes to the person's departments through the Request Manager (tools,
  quotas, approvals, audit). Private questions use web research, product search, YouTube, weather, reminders, data
  analysis and dashboards. One conversation per person; `/reset` starts a new one. Commands: `/help`, `/reset`,
  `/page`, `/mute`, `/unmute`.
- **Long answers:** WhatsApp messages stop at 4,096 characters. Answers over 3,000 characters, tables and dashboards
  go to the person's **result page**: one secret link per person (`https://tw-ai.in/r/<token>`), its content replaced
  by each new result, kept 7 days. WhatsApp gets the opening of the answer plus the link. Anyone with the link can
  open it (owner's choice): Settings has "New page link" to replace a forwarded one.
- **Notifications:** every bell notification (reminders, alerts, finished jobs, digests, shares, budget/credit
  alerts) also goes to a linked number, unless the person turns it off (Settings or `/mute`).
- **Approvals:** linked admins get approval requests with Approve / Reject buttons. Someone who asked for work on
  WhatsApp gets the result there once it's approved or rejected.

## Decisions (owner, 2026-11-17)

| Question | Decision |
|---|---|
| Private data | The WhatsApp conversation is encrypted with a key derived from the server's notes key (like reminders), not the person's own key, which only their password or phone app opens. So their encrypted app chats, My files, own mailbox, templates and saved logins/cards are **not** reachable from WhatsApp. |
| Result page | Secret link only, no sign-in. |
| Outside 24 hours | WhatsApp allows free-form messages only within 24 hours of the person's last message. Outside it, notifications go as the approved template with the title (`Reminder: Call the courier`), which Meta charges per message. |
| Confirmations | Approve / Reject buttons for admins. Live-browser steps (sign-in, OTP, CVV) and saved card/UPI filling stay in the app; those tools aren't offered on WhatsApp. |

**Privacy, knowingly:** WhatsApp Cloud API messages pass through Meta's servers in readable form (the encryption ends
at Meta), and the answers are written by Claude (Anthropic's API), as in the app.

**Meta's policy (as described to the owner, check the current text):** since 2026-01-15 Meta doesn't allow
general-purpose AI chatbots as the main service of a WhatsApp Business number. This bot is an internal tool for the
company's own staff, answering linked numbers only, which is the kind of business use still allowed. Don't open it to
the public.

## Setup in Meta (owner)

1. **A number** that is not on WhatsApp or WhatsApp Business (or delete that account first). It only needs to
   receive one SMS or call for the OTP.
2. **developers.facebook.com** > My Apps > Create App > "Connect with customers through WhatsApp". Link the
   TechWiser Business Portfolio (create it if needed).
3. **WhatsApp > API Setup** > Add phone number: display name (e.g. "TW-ai"), verify with the OTP. Copy the **Phone
   number ID** (an id, not the number).
4. **A permanent token:** business.facebook.com > Settings > Users > System users > Add (Admin) > Add assets: the
   app, full control > Generate token: the app, expiry **Never**, permissions `whatsapp_business_messaging` and
   `whatsapp_business_management`.
5. **App secret:** developers.facebook.com > the app > App settings > Basic > App secret.
6. **Notification template** (WhatsApp Manager > Message templates > Create): category **Utility**, name
   `twai_notification`, language English, body:
   `You have a new update from TW-ai: {{1}}. Reply here to see it.` (sample: `Reminder: Call the courier`).
   Wait for approval. Without it, nothing is sent outside the 24-hour window.
7. **In TW-ai, Credentials page**, add `whatsapp/cloud` with this JSON (one line is fine):
   ```json
   {"token": "<permanent token>", "phone_id": "<phone number id>", "app_secret": "<app secret>",
    "verify_token": "<any long random string>", "display_number": "<bot number with country code, e.g. 9198xxxxxxxx>",
    "template": "twai_notification", "template_lang": "en"}
   ```
8. **Webhook** (developers.facebook.com > the app > WhatsApp > Configuration > Webhook > Edit): callback URL
   `https://tw-ai.in/v1/whatsapp/webhook`, verify token = the `verify_token` above > Verify and save. Then under
   **Webhook fields** subscribe to **messages** (easy to miss: without it nothing arrives).
9. Link your number in TW-ai (Settings > WhatsApp) and send "hi".

Until the business is verified, Meta limits how many people the number may message first each day: plenty for the team.

## Server side

- Settings: vault `whatsapp/cloud` (above); `PUBLIC_APP_URL` in `.env` (the result page links).
- Table `whatsapp_links` (migration 0014): number, link code (hash), 24-hour window, conversation version,
  notifications on/off, result page (encrypted) and its token hash.
- The webhook answers at once and checks Meta's signature on every call; messages run in a small pool in the gateway
  (`bot.EXECUTOR`, 4 at a time), one at a time per conversation; Meta's retries are skipped by message id (Redis).
- Erasing a person's data unlinks their number; their WhatsApp conversation goes with their checkpoints.
- Not yet: voice notes (Whisper is on the server: download the audio from Meta, transcribe, answer), images, files.
