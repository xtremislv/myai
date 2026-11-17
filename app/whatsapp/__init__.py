"""The WhatsApp bot (owner, 2026-11-17): the same assistant on WhatsApp, through Meta's official Cloud API.

  cloud.py   Meta's API: settings (vault `whatsapp/cloud`), webhook signatures, sending messages
  links.py   who is who: a person links their number by sending a one-time code from it; the conversation key;
             the per-person result page (one secret link, content replaced each time)
  format.py  Markdown -> WhatsApp text, and when an answer goes to the result page instead
  bot.py     incoming messages -> triage -> private or work run -> reply; notifications and approvals out

docs/WHATSAPP.md has the owner's setup in Meta and the decisions behind it.
"""
