"""Summaries of data bigger than the model's context: parts, temp folder, Word/Excel output."""
from __future__ import annotations

import io
import json
import re

import pytest

from app import bigsummary
from app.orchestrator import tools as tools_mod


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "work_dir", str(tmp_path / "work"))
    return tmp_path / "work"


def fake_chat(calls):
    def chat(messages, **kw):
        system, user = messages[0]["content"], messages[1]["content"]
        calls.append((system, user))
        if "JSON array" in system:
            return json.dumps([{"invoice": n, "amount": int(n[3:])} for n in re.findall(r"INV\d+", user)])
        if system.startswith("You condense"):
            return "- " + " ".join(sorted(set(re.findall(r"INV\d+", user)))) or "NOTHING RELEVANT"
        if system.startswith("You merge"):
            return "- merged " + " ".join(sorted(set(re.findall(r"INV\d+", user))))
        return "## Overview\n- " + " ".join(sorted(set(re.findall(r"INV\d+", user))))
    return chat


BIG = "\n".join(f"Line {i}: payment received for INV{i:05d} " + "x" * 200 for i in range(2000))


def test_a_text_bigger_than_the_context_is_read_in_parts_and_nothing_is_lost(workdir):
    calls = []
    steps = []
    result = bigsummary.run(BIG, "ledger.txt", "list the invoices", chat=fake_chat(calls), progress=steps.append)
    assert result.parts > 10 and not result.failed
    for i in (0, 999, 1999):  # first, middle and last invoice all reach the final summary
        assert f"INV{i:05d}" in result.text
    assert any(s.startswith("Reading part") for s in steps) and steps[-1] == "Writing the summary"
    assert all(len(u) < bigsummary.CHUNK_CHARS * 2 + 2000 for _, u in calls)  # every call fits
    assert list(workdir.glob("summary-*")) == []  # the temporary folder is gone


def test_a_small_text_is_summarized_in_one_call(workdir):
    calls = []
    result = bigsummary.run("INV00007 paid", "a.txt", chat=fake_chat(calls))
    assert result.parts == 1 and len(calls) == 1 and "INV00007" in result.text


def test_rows_from_every_part_go_into_one_table_with_the_same_columns():
    csv_text = "invoice,amount\n" + "\n".join(f"INV{i:05d},{i}" for i in range(6000))
    result = bigsummary.run(csv_text, "inv.csv", "invoices and amounts", kind="rows", table=True,
                            chat=fake_chat([]))
    assert result.columns == ["invoice", "amount"] and result.parts > 1
    assert len(result.rows) == 6000 and result.rows[-1] == {"invoice": "INV05999", "amount": 5999}


def test_word_and_excel_files_open_and_formulas_are_neutralised():
    from docx import Document
    from openpyxl import load_workbook

    doc = Document(io.BytesIO(bigsummary.to_docx("T", "## Part\n- **bold** point\n1. first\nplain")))
    texts = [p.text for p in doc.paragraphs]
    assert "Part" in texts and "bold point" in texts and "plain" in texts
    wb = load_workbook(io.BytesIO(bigsummary.to_xlsx("T", ["a"], [{"a": "=HYPERLINK(1)"}, {"a": 5}], [])))
    assert wb["Data"]["A2"].value == "'=HYPERLINK(1)" and wb["Data"]["A3"].value == 5


def test_stale_work_folders_are_swept(workdir):
    import os
    import time

    workdir.mkdir(parents=True)
    old = workdir / "summary-old"
    old.mkdir()
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    with bigsummary.Workspace() as ws:
        assert ws.path.exists() and oct(ws.path.stat().st_mode)[-3:] == "700"
    assert not old.exists()


# ---- the tool ----
@pytest.fixture
def tool_env(monkeypatch):
    saved = {}
    monkeypatch.setattr(tools_mod, "_enforce", lambda *a: None)
    monkeypatch.setattr(tools_mod, "_input_bytes", lambda state, path: (path, BIG.encode()))
    monkeypatch.setattr(tools_mod, "_save", lambda state, path, data, **kw: saved.update({path: data}) or f"Created '{path}'.")
    monkeypatch.setattr(bigsummary.llm, "chat", fake_chat([]))
    return saved


def test_the_tool_saves_a_word_document_when_asked(tool_env):
    out = tools_mod.summarize_large({"scratchpad": {}}, "ledger.txt", "invoices", output="word")
    assert "Summaries/ledger summary.docx" in tool_env and "Word document" in out


def test_the_tool_answers_in_the_chat_when_short(tool_env):
    out = tools_mod.summarize_large({"scratchpad": {}}, "ledger.txt", output="chat")
    assert "INV01999" in out and not tool_env


def test_the_tool_saves_a_sheet(tool_env):
    out = tools_mod.summarize_large({"scratchpad": {}}, "ledger.txt", "invoices", output="excel", save_as="out/inv")
    assert "out/inv.xlsx" in tool_env and "2000 rows" in out
