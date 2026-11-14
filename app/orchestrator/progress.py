"""What an agent is doing right now, for the person waiting on it ("Checking inventory").

Nodes call step(); a streamed chat sets `progress_sink` so the steps reach the browser as they
happen. Unset (tests, approvals resumed later, the plain JSON endpoint) it does nothing.
Steps are shown only to the requester and are never logged.
"""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

progress_sink: ContextVar[Callable[[str], None] | None] = ContextVar("progress_sink", default=None)

# Friendly names for tool steps. Unknown tools fall back to their name.
TOOL_STEPS = {
    "read_document": "Reading {key}",
    "list_documents": "Listing documents in {prefix}",
    "get_secret": "Reading a stored credential",
    "balance_lookup": "Looking up the balance for {account}",
    "manage_order": "Updating order {order_id}",
    "manage_bill": "Recording bill {bill_id}",
    "initiate_payment": "Preparing a payment to {payee}",
    "book_service": "Booking {service}",
    "read_mail": "Reading {category} mail",
    "inventory_lookup": "Checking inventory for {sku}",
    "inventory_adjust": "Adjusting stock for {sku}",
    "track_shipment": "Tracking shipment {shipment_id}",
    "send_notification": "Sending a notification to {to}",
    "set_reminder": "Setting a reminder for {when}",
    "web_search": "Searching the web for {query}",
    "news_search": "Checking the news on {query}",
    "scholar_search": "Searching research papers on {query}",
    "product_search": "Looking for {query} in shops",
    "product_page": "Checking the product page",
    "ask_person_to_browse": "Handing the browser to you",
    "list_saved_details": "Checking your saved details",
    "fill_saved": "Filling in your saved {field}",
    "ask_person_for_secret": "Asking you for {what}",
    "weather": "Checking the weather in {location}",
    "fetch_page": "Reading {url}",
    "scrape_page": "Extracting {what} from {url}",
    "browser_open": "Opening {url}",
    "browser_read": "Reading the page",
    "browser_click": "Clicking on the page",
    "browser_type": "Typing {text}",
    "browser_select": "Choosing {option}",
    "browser_scroll": "Scrolling",
    "browser_back": "Going back",
    "browser_commit": "Confirming: {summary}",
    "search_mail": "Searching {category} mail for {query}",
    "open_mail": "Opening an email",
    "mark_mail": "Marking an email as {action}",
    "send_mail": "Sending an email to {to}",
    "summarize": "Summarizing",
    "summarize_mail": "Reading and summarizing the {category} mail",
    "list_mailboxes": "Checking which mailboxes you have",
    "list_reminders": "Checking your reminders",
    "cancel_reminder": "Cancelling a reminder",
    "list_files": "Listing files in {folder}",
    "read_file": "Reading {path}",
    "write_file": "Saving {path}",
    "append_file": "Adding to {path}",
    "edit_file": "Editing {path}",
    "move_file": "Moving {path} to {new_path}",
    "copy_file": "Copying {path} to {new_path}",
    "delete_file": "Deleting {path}",
    "restore_file": "Restoring {path}",
    "list_templates": "Looking at the templates",
    "read_template": "Reading the template {name}",
    "create_from_template": "Making documents from {template}",
    "save_template": "Saving the template {name}",
    "review_report": "Opening report {report_id}",
    "run_python": "Running code on the data",
    "summarize_large": "Summarizing {source}",
    "mail_to_sheet": "Finding every email matching {query}",
    "job_status": "Checking on the background work",
    "forum_search": "Searching forums for {query}",
    "list_people": "Checking who's on the platform",
    "share_with": "Sharing with {to}",
    "shared_with_me": "Checking what's been shared with you",
    "open_shared": "Opening what was shared",
    "github_search": "Searching GitHub for {query}",
    "gadget_search": "Searching the gadget stores for {query}",
    "youtube_search": "Searching YouTube for {query}",
    "youtube_channel": "Checking the YouTube channel {channel}",
    "youtube_video": "Looking at the YouTube videos",
    "read_thread": "Reading {url}",
    "save_table": "Saving the sheet {path}",
    "web_research": "Starting research on {topic} across many sites",
    "start_background_task": "Starting the task in the background",
    "make_dashboard": "Building a dashboard: {title}",
}


class _Blank(dict):
    """format_map values where a missing argument is just left out."""

    def __missing__(self, key: str) -> str:
        return ""


def step(text: str) -> None:
    sink = progress_sink.get()
    if sink is not None:
        try:
            sink(text)
        except Exception:  # noqa: BLE001 - progress must never break a run
            pass


def tool_step(name: str, args: Any) -> str:
    """A short description of a tool call, e.g. "Checking inventory for laptop"."""
    template = TOOL_STEPS.get(name, name.replace("_", " ").capitalize())
    values = {k: str(v)[:60] for k, v in args.items()} if isinstance(args, dict) else {}
    try:
        text = template.format_map(_Blank(values))
    except (ValueError, IndexError):
        text = template
    return " ".join(text.split()).removesuffix(" for").removesuffix(" in")
