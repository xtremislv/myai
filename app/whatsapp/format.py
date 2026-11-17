"""The assistant writes Markdown; WhatsApp has its own small format and a 4096-character limit.

Short answers are converted and sent as they are. Long ones, tables and dashboards go to the person's result page
(links.save_page) and WhatsApp gets the opening of the answer with the link.
"""
from __future__ import annotations

import re

PAGE_OVER = 3000       # characters: longer answers go to the page
PREVIEW_CHARS = 900

_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$", re.M)
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BULLET = re.compile(r"^([ \t]*)[*+][ \t]+", re.M)
_RULE = re.compile(r"^[ \t]*([-*_])[ \t]*(\1[ \t]*){2,}$", re.M)


def has_table(markdown: str) -> bool:
    return bool(_TABLE_RULE.search(markdown or ""))


def needs_page(markdown: str, dashboards: list | None = None) -> bool:
    return bool(dashboards) or has_table(markdown) or len(markdown or "") > PAGE_OVER


def to_whatsapp(markdown: str) -> str:
    """WhatsApp's format: *bold*, _italic_, ~strike~, ```code```, "- " lists. Links are written out."""
    text = (markdown or "").replace("\r\n", "\n")
    parts = re.split(r"(```.*?```)", text, flags=re.S)  # leave code blocks alone
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)
            continue
        part = _RULE.sub("", part)
        part = _HEADING.sub(lambda m: f"*{m.group(1).strip('*_ ')}*", part)
        part = _BULLET.sub(lambda m: f"{m.group(1)}- ", part)  # before bold: "* item" is a bullet, not emphasis
        part = _BOLD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", part)
        part = _LINK.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)}: {m.group(2)}", part)
        out.append(part)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def preview(markdown: str, limit: int = PREVIEW_CHARS) -> str:
    """The opening of an answer, cut at a paragraph (or line) and without half a table."""
    text = (markdown or "").strip()
    match = _TABLE_RULE.search(text)
    if match:  # stop before the table's header row
        text = text[:text.rfind("\n", 0, match.start()) if "\n" in text[:match.start()] else 0].rstrip()
    if len(text) <= limit:
        return to_whatsapp(text)
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 3:
        cut = text.rfind("\n", 0, limit)
    if cut < limit // 3:
        cut = text.rfind(" ", 0, limit)
    return to_whatsapp(text[:max(cut, 1)]).rstrip() + " …"


def title_of(markdown: str, fallback: str) -> str:
    match = _HEADING.search(markdown or "")
    return (match.group(1).strip("*_ ") if match else fallback)[:120]


def chunks(text: str, limit: int = 3900) -> list[str]:
    """Text over WhatsApp's limit as several messages, split at paragraphs (or lines, or words)."""
    text = text.strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*out, text] if text else out
