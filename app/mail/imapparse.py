"""Reading IMAP FETCH replies and BODYSTRUCTURE, so an email's text can be fetched whole without its
attachments, and an attachment fetched alone when someone asks for it.

imaplib hands back a FETCH reply as a list: a tuple (text ending in {n}, n bytes) for each literal,
and plain bytes for the text between literals. `messages()` turns that into one dict per email
({"UID": "5", "FLAGS": [...], "BODYSTRUCTURE": [...], "BODY[1.1]<0>": b"..."}), wherever the server
put each item. `parts()` walks a BODYSTRUCTURE (RFC 3501 7.4.2) into its leaf parts.
"""
from __future__ import annotations

import base64
import binascii
import quopri
import re
from collections.abc import Iterator
from dataclasses import dataclass
from email.header import decode_header, make_header
from urllib.parse import unquote

_LITERAL = re.compile(rb"\{(\d+)\+?\}\s*$")


class _Lit(bytes):
    """A literal's bytes: kept apart from atoms and quoted strings."""


def _tokens(chunk: bytes) -> Iterator[object]:
    i, n = 0, len(chunk)
    while i < n:
        c = chunk[i:i + 1]
        if c in b" \r\n\t":
            i += 1
        elif c in b"()":
            yield c.decode()
            i += 1
        elif c == b'"':
            out, i = bytearray(), i + 1
            while i < n and chunk[i:i + 1] != b'"':
                if chunk[i:i + 1] == b"\\" and i + 1 < n:
                    i += 1
                out += chunk[i:i + 1]
                i += 1
            i += 1
            yield ("q", out.decode("utf-8", errors="replace"))
        else:
            start = i
            while i < n and chunk[i:i + 1] not in b" ()\r\n\t":
                if chunk[i:i + 1] == b"[":  # BODY[HEADER.FIELDS (FROM DATE)]: spaces and parens inside
                    end = chunk.find(b"]", i)
                    i = n if end < 0 else end + 1
                else:
                    i += 1
            atom = chunk[start:i].decode("utf-8", errors="replace")
            if re.fullmatch(r"\{\d+\+?\}", atom):
                continue  # the literal itself follows as its own element
            yield None if atom.upper() == "NIL" else ("a", atom)


def _stream(rows) -> Iterator[object]:
    for row in rows or []:
        if isinstance(row, tuple) and len(row) >= 2:
            head = row[0] if isinstance(row[0], bytes) else str(row[0]).encode()
            yield from _tokens(_LITERAL.sub(b"", head))
            yield _Lit(row[1] if isinstance(row[1], bytes) else str(row[1]).encode())
        elif isinstance(row, bytes):
            yield from _tokens(row)


def _value(tok):
    if isinstance(tok, tuple):
        return tok[1]
    return tok  # None (NIL) or _Lit


def _parse(tokens: list, i: int) -> tuple[list, int]:
    """The list opening at tokens[i] == "(" -> (python list, index after its ")")."""
    out: list = []
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "(":
            sub, i = _parse(tokens, i)
            out.append(sub)
            continue
        if tok == ")":
            return out, i + 1
        out.append(_value(tok))
        i += 1
    return out, i


def messages(rows) -> list[dict]:
    """One {ITEM NAME: value} dict per email in a FETCH reply. Names are upper case; a
    section keeps its spelling from the reply ("BODY[1.2]<0>")."""
    tokens = list(_stream(rows))
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == "(":
            items, i = _parse(tokens, i)
            got = {}
            for k in range(0, len(items) - 1, 2):
                if isinstance(items[k], str):
                    got[items[k].upper()] = items[k + 1]
            out.append(got)
        else:
            i += 1  # the message sequence number, or noise between replies
    return out


def section_value(item: dict, section: str):
    """The bytes the reply gave for BODY[<section>] (with or without a <0> origin)."""
    want = f"BODY[{section}]".upper()
    for key, val in item.items():
        if key == want or (key.startswith(want + "<")):
            return val
    return None


@dataclass
class Part:
    section: str  # "1", "1.2", ...: what BODY.PEEK[...] takes
    type: str  # "text/plain"
    encoding: str  # "base64", "quoted-printable", "7bit", ...
    charset: str
    size: int  # encoded bytes on the server
    filename: str
    attachment: bool  # a file (by disposition, a name, or not being text)


def _params(raw) -> dict[str, str]:
    if not isinstance(raw, list):
        return {}
    return {str(raw[k]).lower(): str(raw[k + 1] or "") for k in range(0, len(raw) - 1, 2)}


def _decoded_name(params: dict[str, str]) -> str:
    for key in ("filename", "name"):
        if params.get(key):
            try:
                return str(make_header(decode_header(params[key])))
            except Exception:  # noqa: BLE001 - a malformed encoded word stays as sent
                return params[key]
        if params.get(key + "*"):  # RFC 2231: utf-8''na%C3%AFve.pdf
            value = params[key + "*"]
            charset, _, rest = value.partition("''") if "''" in value else ("utf-8", "", value)
            try:
                return unquote(rest, encoding=charset or "utf-8", errors="replace")
            except LookupError:
                return unquote(rest)
    return ""


def parts(structure, prefix: str = "") -> list[Part]:
    """The leaf parts of a BODYSTRUCTURE, in order. A forwarded email (message/rfc822) is one part:
    an attachment that open_mail can read like any other."""
    if not isinstance(structure, list) or not structure:
        return []
    if isinstance(structure[0], list):  # multipart: children, then the subtype
        out: list[Part] = []
        n = 0
        for child in structure:
            if not isinstance(child, list):
                break
            n += 1
            out += parts(child, f"{prefix}{n}.")
        return out
    section = prefix.rstrip(".") or "1"
    kind = f"{str(structure[0] or '').lower()}/{str(structure[1] or '').lower()}"
    params = _params(structure[2] if len(structure) > 2 else None)
    encoding = str(structure[5] or "7bit").lower() if len(structure) > 5 else "7bit"
    try:
        size = int(structure[6]) if len(structure) > 6 and structure[6] is not None else 0
    except (TypeError, ValueError):
        size = 0
    # body-ext-1part: md5, disposition... after the type's own fields (text: + lines; message/rfc822:
    # + envelope, body, lines).
    ext = 8 if kind.startswith("text/") else 10 if kind == "message/rfc822" else 7
    disposition = structure[ext + 1] if len(structure) > ext + 1 else None
    disp_kind, disp_params = "", {}
    if isinstance(disposition, list) and disposition:
        disp_kind = str(disposition[0] or "").lower()
        disp_params = _params(disposition[1] if len(disposition) > 1 else None)
    filename = _decoded_name(disp_params) or _decoded_name(params)
    attachment = (disp_kind == "attachment" or bool(filename)
                  or kind not in ("text/plain", "text/html"))
    if kind == "message/rfc822" and not filename:
        filename = f"forwarded-email-{section}.eml"
    return [Part(section, kind, encoding, params.get("charset", ""), size, filename, attachment)]


def body_part(all_parts: list[Part]) -> Part | None:
    """The part holding the email's text: the first inline plain-text part, else the first HTML
    one (as email.message.get_body prefers)."""
    inline = [p for p in all_parts if not p.attachment]
    return (next((p for p in inline if p.type == "text/plain"), None)
            or next((p for p in inline if p.type == "text/html"), None))


def decode(data: bytes, encoding: str) -> bytes:
    """Undo the transfer encoding. A partial fetch may cut base64 mid-group: the rest is dropped."""
    encoding = (encoding or "").lower()
    if encoding == "base64":
        clean = re.sub(rb"[^A-Za-z0-9+/]", b"", data)  # padding is put back below
        if len(clean) % 4 == 1:  # one stray character holds no whole byte
            clean = clean[:-1]
        clean += b"=" * (-len(clean) % 4)
        try:
            return base64.b64decode(clean)
        except (binascii.Error, ValueError):
            return b""
    if encoding == "quoted-printable":
        return quopri.decodestring(data)
    return data


def text_of(data: bytes, part: Part) -> str:
    raw = decode(data, part.encoding)
    charset = part.charset or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{max(1, round(size / 1024))} KB"
