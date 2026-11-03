"""Documents from templates: placeholders (text and Word), examples imitated by the model,
batches from records, where templates come from, and who may use which."""
from __future__ import annotations

import io
import json
import os

import pytest
from docx import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.acl.models import Subject
from app.db.session import Base, User
from app.departments.registry import seed_departments
from app.documents import engine, store
from app.llm import client as llm
from app.orchestrator import tools
from app.orchestrator.context import attachment_files, user_key
from app.storage import objects, userfiles

KEY = os.urandom(32)


def _docx(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if table_rows:
        table = doc.add_table(rows=0, cols=len(table_rows[0]))
        for row in table_rows:
            cells = table.add_row().cells
            for cell, text in zip(cells, row):
                cell.text = text
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _docx_text(data: bytes) -> str:
    return engine.text_of("x.docx", data)


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    with Session() as s:
        seed_departments(s)
        s.add(User(id="alice", username="alice", password_hash="x", roles=["hr"], prefs={}))
        s.commit()
    monkeypatch.setattr(tools, "SessionLocal", Session)
    monkeypatch.setattr(tools, "_enforce", lambda *a: None)
    return Session


@pytest.fixture
def store_blobs(monkeypatch):
    blobs: dict[str, bytes] = {}
    monkeypatch.setattr(userfiles, "_put", lambda k, d: blobs.__setitem__(k, d))
    monkeypatch.setattr(userfiles, "_get", lambda k: blobs[k])
    monkeypatch.setattr(userfiles, "_remove", lambda k: blobs.pop(k, None))
    monkeypatch.setattr(objects, "put_system_object", lambda k, d: blobs.__setitem__(k, d))
    monkeypatch.setattr(objects, "get_system_object", lambda k: blobs[k])
    monkeypatch.setattr(objects, "system_object_size", lambda k: len(blobs[k]) if k in blobs else None)

    class FakeStore:
        def __init__(self, acl):
            self._acl = acl

        def put(self, subject, key, data, content_type="x"):
            from app.acl.models import Action

            self._acl.enforce(subject, Action.WRITE, objects.resource_for(key))
            blobs[key] = data

        def get(self, subject, key):
            return blobs[key]

    monkeypatch.setattr(objects, "ObjectStore", FakeStore)
    return blobs


@pytest.fixture
def mine():
    token = user_key.set(KEY)
    yield
    user_key.reset(token)


def _private():
    return {"subject_id": "alice", "subject_roles": ["hr"], "scratchpad": {"mode": "single"}, "messages": []}


def _work(dept="hr"):
    return {"subject_id": "alice", "subject_roles": [dept], "scratchpad": {"department": dept}, "messages": []}


def _read_mine(db, path) -> bytes:
    with db() as s:
        return userfiles.read(s, KEY, "alice", path)[1]


# ---- the engine ----
def test_text_placeholders_are_filled_and_missing_ones_named():
    text = "Dear {{ name }},\nYour salary is {{ salary }}.\n{% for d in docs %}- {{ d }}\n{% endfor %}"
    assert engine.fields("x.md", text.encode()) == ["docs", "name", "salary"]
    out = engine.fill_text(text, {"name": "Ravi", "salary": "12 LPA", "docs": ["ID", "PAN"]})
    assert "Dear Ravi" in out and "- PAN" in out
    with pytest.raises(engine.MissingFields) as e:
        engine.fill_text(text, {"name": "Ravi"})
    assert set(e.value.names) == {"salary", "docs"}


def test_templates_run_in_a_sandbox():
    with pytest.raises(engine.TemplateError):
        engine.fill_text("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_word_placeholders_keep_the_paragraphs_and_repeat_item_rows():
    tpl = _docx(
        ["INVOICE {{ number }}", "Bill to: {{ client }}"],
        [["Item", "Amount"], ["{{ item.description }}", "{{ item.amount }}"], ["Total", "{{ total }}"]],
    )
    assert engine.fields("inv.docx", tpl) == ["client", "items", "number", "total"]
    out = engine.fill_docx(tpl, {"number": "INV-7", "client": "Acme", "total": "300",
                                 "items": [{"description": "Bolts", "amount": "100"}, {"description": "Nuts", "amount": "200"}]})
    doc = Document(io.BytesIO(out))
    assert [p.text for p in doc.paragraphs][:2] == ["INVOICE INV-7", "Bill to: Acme"]
    rows = [[c.text for c in r.cells] for r in doc.tables[0].rows]
    assert rows == [["Item", "Amount"], ["Bolts", "100"], ["Nuts", "200"], ["Total", "300"]]
    with pytest.raises(engine.MissingFields):
        engine.fill_docx(tpl, {"number": "1"})


def test_an_example_word_file_is_rewritten_in_place():
    example = _docx(["Offer letter", "Dear Asha, you join on 1 March as Analyst."], [["CTC", "9 LPA"]])

    def chat(messages, **kw):
        assert "Asha" in messages[1]["content"] and "Ravi" in messages[1]["content"]
        return json.dumps({"0": "Offer letter", "1": "Dear Ravi, you join on 1 December as Engineer.", "2": "CTC", "3": "12 LPA"})

    out = engine.imitate_docx(example, "Ravi, Engineer, joins 1 December, 12 LPA", chat)
    text = _docx_text(out)
    assert "Ravi" in text and "12 LPA" in text and "Asha" not in text
    assert len(Document(io.BytesIO(out)).tables) == 1  # the layout survived


def test_records_come_from_csv_and_json():
    assert engine.records_from_file("p.csv", b"name,city\nRavi,Pune\nAsha,Goa\n") == [
        {"name": "Ravi", "city": "Pune"}, {"name": "Asha", "city": "Goa"}]
    assert engine.parse_data('{"a": 1}') == [{"a": 1}] and engine.parse_data("just words") is None


# ---- the tools ----
def test_private_chat_fills_an_attached_template_into_my_files(db, store_blobs, mine):
    token = attachment_files.set([{"name": "receipt.md", "content_type": "text/markdown",
                                   "data": b"Rent receipt\nReceived {{ amount }} from {{ tenant }} for {{ month }}.\n"}])
    try:
        info = tools.read_template(_private(), "receipt")
        assert "amount, month, tenant" in info
        missing = tools.create_from_template(_private(), "receipt", json.dumps({"tenant": "Ravi"}))
        assert missing.startswith("MISSING") and "amount" in missing
        done = tools.create_from_template(_private(), "receipt.md", json.dumps({"tenant": "Ravi", "amount": "18000", "month": "October"}),
                                          output_path="receipts/{{ tenant }}-{{ month }}.md")
    finally:
        attachment_files.reset(token)
    assert "receipts/Ravi-October.md" in done
    assert b"Received 18000 from Ravi for October." in _read_mine(db, "receipts/Ravi-October.md")


def test_a_batch_makes_one_document_per_record_from_a_csv(db, store_blobs, mine):
    tools.save_template(_private(), "welcome", content="Welcome {{ name }} to {{ team }}!\n")
    tools.write_file(_private(), "people.csv", "name,team\nRavi,Ops\nAsha,Sales\n")
    done = tools.create_from_template(_private(), "welcome", data_file="people.csv", output_path="welcome/{{ name }}.md")
    assert "Created 2" in done
    assert _read_mine(db, "welcome/Asha.md") == b"Welcome Asha to Sales!\n"


def test_free_text_data_is_turned_into_fields_by_the_model(db, store_blobs, mine, monkeypatch):
    tools.save_template(_private(), "note", content="To {{ name }}: meet at {{ place }}.\n")
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: '{"name": "Ravi", "place": "Gate 2"}')
    assert "Created 1" in tools.create_from_template(_private(), "note", "Tell Ravi to meet at Gate 2", "note-ravi.md")
    assert _read_mine(db, "note-ravi.md") == b"To Ravi: meet at Gate 2.\n"


def test_an_example_text_document_is_imitated(db, store_blobs, mine, monkeypatch):
    tools.write_file(_private(), "templates/minutes.md", "# Minutes 1 Oct\nAttendees: A, B\nDecision: buy laptops\n")
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "```md\n# Minutes 3 Nov\nAttendees: C\nDecision: [TO FILL: decision]\n```")
    done = tools.create_from_template(_private(), "minutes", "Meeting 3 Nov with C", "minutes-nov.md")
    assert "1 gap(s)" in done
    assert _read_mine(db, "minutes-nov.md").startswith(b"# Minutes 3 Nov")


def test_company_templates_follow_department_access(db, store_blobs):
    with db() as s:
        store.save(s, Subject("root", ("admin",)), "hr", "Offer letter", "offer.md", b"Dear {{ name }}, welcome.\n")
        store.save(s, Subject("root", ("admin",)), "account", "Invoice", "inv.md", b"Invoice {{ n }}\n")
        store.save(s, Subject("root", ("admin",)), "global", "Letterhead", "lh.md", b"ACME\n{{ body }}\n")
        hr = Subject("alice", ("hr",))
        assert [r.name for r in store.visible(s, hr, ["hr", "account", "global"])] == ["Offer letter", "Letterhead"]
        with pytest.raises(PermissionError):
            store.save(s, hr, "account", "Mine now", "x.md", b"x")
        with pytest.raises(PermissionError):
            store.save(s, hr, "global", "Everyone", "x.md", b"x")
    listing = tools.list_templates(_work("hr"))
    assert "Offer letter" in listing and "Invoice" not in listing
    done = tools.create_from_template(_work("hr"), "offer letter", '{"name": "Ravi"}', "offers/ravi.md")
    assert "shared/hr/offers/ravi.md" in done
    assert store_blobs["shared/hr/offers/ravi.md"] == b"Dear Ravi, welcome.\n"
    assert tools.create_from_template(_work("hr"), "Invoice", '{"n": 1}').startswith("ERROR: no template")


def test_private_chat_does_not_use_company_templates(db, store_blobs, mine):
    with db() as s:
        store.save(s, Subject("root", ("admin",)), "global", "Letterhead", "lh.md", b"ACME {{ body }}\n")
    assert "no template" in tools.create_from_template(_private(), "Letterhead", '{"body": "x"}')


def test_a_name_copied_with_its_listing_details_still_finds_the_template(db):
    with db() as s:
        store.save(s, Subject("root", ("admin",)), "hr", "Offer letter", "offer.docx", _docx(["{{ name }}"]))
        hr = Subject("alice", ("hr",))
        for asked in ("Offer letter", "offer letter [hr, docx]", "Offer letter (for: hr; format: docx)", "offer"):
            assert store.find(s, hr, asked, ["hr", "global"]) is not None, asked


def test_text_is_never_written_into_a_word_or_pdf_file(db, store_blobs, mine):
    for call in (lambda: tools.write_file(_private(), "a.docx", "hello"),
                 lambda: tools.append_file(_private(), "a.pdf", "hello"),
                 lambda: tools.edit_file(_private(), "a.docx", "x", "y")):
        assert "not a plain-text format" in call()
