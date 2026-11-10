"""Email the admin the platform's public links (scripts/watchdog.sh runs this when they change, and once
after every container start).

    python -m scripts.notify_links APP_URL GRAFANA_URL [--note TEXT] [--restarted "WHEN"]

Sent from the notification mailbox (vault secret `mail/notify`); never from an official mailbox.
Exits 1 if nothing could be sent, so the watchdog tries again on its next round.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from app.db.session import SessionLocal
from app.reminders.delivery import email_admins


def body(app_url: str, grafana_url: str, note: str = "", restarted: str = "") -> str:
    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    links = [
        f"  App:      {app_url}",
        f"  Grafana:  {grafana_url}   (user admin, password GRAFANA_PASSWORD in .env)",
    ]
    if restarted:
        status = "is partly up" if note else "is back up, all services running"
        lines = [f"The AI Platform server restarted ({restarted}) and {status} ({when}).", "", *links]
    else:
        lines = [f"The AI Platform is up ({when}). Its public links changed:", "", *links, "",
                 "The old links no longer work."]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_url")
    parser.add_argument("grafana_url")
    parser.add_argument("--note", default="")
    parser.add_argument("--restarted", default="", help="when the container started: send the restart notice")
    args = parser.parse_args()
    with SessionLocal() as session:
        subject = "AI Platform: back up after a restart" if args.restarted else "AI Platform: new links"
        if args.restarted and args.note:
            subject = "AI Platform: restarted, something is still down"
        sent = email_admins(session, subject, body(args.app_url, args.grafana_url, args.note, args.restarted))
    if not sent:
        print("no notification mailbox: add mail/notify on the Credentials page", file=sys.stderr)
        return 1
    print(f"links emailed to {sent} address(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
