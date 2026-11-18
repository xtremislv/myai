"""FETCH replies and BODYSTRUCTURE as servers really send them."""
from app.mail import imapparse


def test_items_in_any_order_with_a_literal_inside_the_structure():
    rows = [
        (b'1 (UID 7 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "QUOTED-PRINTABLE" 10 1 NIL NIL '
         b'NIL NIL)("APPLICATION" "OCTET-STREAM" ("NAME" {9}', b"r\xc3\xa9sum.pdf"),
        b') NIL NIL "BASE64" 80 NIL ("ATTACHMENT" ("FILENAME*" "utf-8\'\'na%C3%AFve.txt")) NIL NIL) "MIXED" '
        b'("BOUNDARY" "x") NIL NIL NIL) FLAGS (\\Seen \\Flagged))',
    ]
    [item] = imapparse.messages(rows)
    assert item["UID"] == "7" and item["FLAGS"] == ["\\Seen", "\\Flagged"]
    text, file = imapparse.parts(item["BODYSTRUCTURE"])
    assert (text.section, text.type, text.charset, text.attachment) == ("1", "text/plain", "iso-8859-1", False)
    assert (file.section, file.filename, file.attachment) == ("2", "naïve.txt", True)
    assert imapparse.body_part([text, file]) is text


def test_nested_parts_html_only_and_forwarded_mail():
    bs = [[["text", "html", ["charset", "utf-8"], None, None, "7bit", "50", "2", None, None, None, None],
           ["image", "png", ["name", "=?utf-8?q?logo_1.png?="], "<id>", None, "base64", "900", None,
            ["inline", None], None, None], "related", None, None, None, None],
          ["message", "rfc822", None, None, None, "7bit", "500", None, None, "20", None, None, None, None],
          "mixed", None, None, None, None]
    html, logo, fwd = imapparse.parts(bs)
    assert html.section == "1.1" and logo.section == "1.2" and logo.filename == "logo 1.png" and logo.attachment
    assert fwd.section == "2" and fwd.attachment and fwd.filename == "forwarded-email-2.eml"
    assert imapparse.body_part([html, logo, fwd]) is html


def test_a_single_part_email_is_section_1_and_sections_with_spaces_are_one_item():
    rows = [(b'3 (UID 9 BODY[HEADER.FIELDS (FROM SUBJECT)] {6}', b"a: b\r\n"), (b' BODY[1]<0> {3}', b"xyz"), b")"]
    [item] = imapparse.messages(rows)
    assert item["BODY[HEADER.FIELDS (FROM SUBJECT)]"] == b"a: b\r\n" and imapparse.section_value(item, "1") == b"xyz"
    [p] = imapparse.parts(["TEXT", "PLAIN", None, None, None, "8BIT", "3", "1"])
    assert p.section == "1" and p.encoding == "8bit" and not p.attachment


def test_decoding_survives_a_cut_base64_and_bad_charsets():
    assert imapparse.decode(b"aGVsbG8gd29y\r\nbGQ", "base64") == b"hello world"
    assert imapparse.decode(b"aGVsbG8gd29yb", "base64") == b"hello wor"
    part = imapparse.Part("1", "text/plain", "quoted-printable", "x-unknown", 0, "", False)
    assert imapparse.text_of(b"caf=C3=A9", part) == "café"
