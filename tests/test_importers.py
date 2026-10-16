"""Importers produce the right rows from synthetic fixtures. No database needed."""

from __future__ import annotations

import collections

import pytest

from resonant_archive import importers
from resonant_archive.importers.base import Memory, Options, Record

from . import synth


def run(name, path, **kw):
    stats = collections.Counter()
    out = list(importers.get(name).records(path, Options(**kw), stats))
    return [r for r in out if isinstance(r, Record)], [m for m in out if isinstance(m, Memory)], stats


def test_registry_lists_builtins_and_detects(tmp_path):
    assert {"claude_code", "chronicle_mirror", "nexus_markdown", "markdown_folder"} <= set(importers.available())
    cc = synth.claude_code_session(tmp_path / "cc")
    assert importers.detect(cc)[0] == "claude_code" and importers.detect(cc.parent)[0] == "claude_code"
    assert importers.detect(tmp_path / "cc")[0] == "claude_code"
    mirror = synth.mirror_tree(tmp_path / "mirror")
    assert importers.detect(mirror) == ["chronicle_mirror"]
    nx = tmp_path / "nx"
    synth.nexus_gpt(nx)
    assert importers.detect(nx)[0] == "nexus_markdown"
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("hello world " * 30)
    assert importers.detect(notes) == ["markdown_folder"]
    assert importers.detect(tmp_path / "missing") == []


def test_broken_plugin_does_not_break_builtins(monkeypatch):
    class EP:
        name = "bad"
        def load(self):
            raise ImportError("nope")
    monkeypatch.setattr(importers.metadata, "entry_points", lambda group: [EP()])
    assert "claude_code" in importers.available()
    assert "bad" in importers.plugin_errors()[0]


def test_good_plugin_registers(monkeypatch):
    class Mine(importers.Importer):
        name = "mine"
    class EP:
        name = "mine"
        def load(self):
            return Mine
    monkeypatch.setattr(importers.metadata, "entry_points", lambda group: [EP()])
    assert "mine" in importers.available()


def test_record_validates_speaker():
    import datetime as dt
    with pytest.raises(ValueError):
        Record(id="x", source="s", conv_id="c", ts=dt.datetime.now(dt.timezone.utc), speaker="wizard", text="hi")
    with pytest.raises(ValueError):
        Record(id="x", source="s", conv_id="c", ts=dt.datetime.now(), speaker="user", text="hi")  # naive time


# ---------------------------------------------------------------- claude_code

def test_claude_code_rows(tmp_path):
    f = synth.claude_code_session(tmp_path)
    recs, _, stats = run("claude_code", f)
    by_id = {r.id.split(":", 2)[2]: r for r in recs}
    assert by_id["u1"].speaker == "user" and not by_id["u1"].hidden
    assert by_id["a1"].speaker == "agent" and by_id["a1"].model == "model-x"
    assert by_id["a1:thinking"].text.startswith("[thinking] ") and "thinking" in by_id["a1:thinking"].tags
    assert by_id["a1:tool1"].speaker == "tool" and by_id["a1:tool1"].hidden and by_id["a1:tool1"].text.startswith("Read: ")
    assert by_id["u2:tool1"].hidden and by_id["u2:tool1"].text.startswith("[result:Read] ")
    assert by_id["u3"].speaker == "system" and by_id["u3"].hidden
    assert by_id["u4"].speaker == "peer" and "peer:robo" in by_id["u4"].tags and not by_id["u4"].hidden
    assert "a2" not in by_id                         # sidechain
    assert stats["bad_lines"] == 1 and stats["sidechain_skipped"] == 1
    assert {r.conv_title for r in recs} == {"Fixing the widget"}   # custom beats AI title
    assert {r.source for r in recs} == {"claude-code"} and {r.platform for r in recs} == {"cli"}
    assert all(r.id.startswith("claude-code:sess-0001:") for r in recs)


def test_claude_code_source_override_and_project_filter(tmp_path):
    synth.claude_code_session(tmp_path, project="proj-a", sid="s-a")
    synth.claude_code_session(tmp_path, project="proj-b", sid="s-b")
    recs, _, _ = run("claude_code", tmp_path, source="work", projects=["proj-a"])
    assert {r.conv_id for r in recs} == {"s-a"} and {r.source for r in recs} == {"work"}


def test_claude_code_ids_are_deterministic(tmp_path):
    f = synth.claude_code_session(tmp_path)
    assert [r.id for r in run("claude_code", f)[0]] == [r.id for r in run("claude_code", f)[0]]


# ---------------------------------------------------------------- chronicle_mirror

def test_mirror_rows_and_memories(tmp_path):
    root = synth.mirror_tree(tmp_path)
    recs, mems, stats = run("chronicle_mirror", root)
    by_id = {r.id.split(":", 2)[2]: r for r in recs}
    assert by_id["m1"].speaker == "user" and not by_id["m1"].hidden
    assert by_id["m2"].speaker == "agent" and by_id["m2:tool:0"].hidden and by_id["m2:tool:0"].speaker == "tool"
    assert by_id["m3:result:0"].text == "[result] tulips and beans"
    assert "mid-turn" in by_id["m4"].tags and by_id["m4"].speaker == "user"
    assert by_id["m5"].speaker == "system" and by_id["m5"].hidden
    assert by_id["m6"].speaker == "agent"                       # second day folder joined to the same session
    summ = [r for r in recs if "summary" in r.tags]
    assert len(summ) == 1 and summ[0].hidden and "tulips" in summ[0].text
    assert {r.conv_id for r in recs} == {"mirror-sess-1"}
    assert stats["bad_lines"] == 1 and stats["duplicates_in_input"] == 1 and stats["events_ignored"] == 2
    assert len(mems) == 1 and mems[0].text.startswith("We sketched") and mems[0].ordinal == 0
    assert mems[0].t_from < mems[0].t_to and mems[0].sid == "mirror-sess-1"
    assert {r.source for r in recs} == {"chronicle"}


def test_mirror_one_unit_per_session(tmp_path):
    root = synth.mirror_tree(tmp_path)
    units = list(importers.get("chronicle_mirror").units(root, Options()))
    assert len(units) == 1 and len(units[0].files) == 3


# ---------------------------------------------------------------- nexus_markdown

def test_nexus_chatgpt(tmp_path):
    synth.nexus_gpt(tmp_path)
    recs, _, stats = run("nexus_markdown", tmp_path, timezone="UTC")
    assert [r.speaker for r in recs] == ["user", "agent", "tool", "tool"]
    assert recs[0].text == "Where should we stay in Lisbon?\nNear https://example.test/maps?q=lisbon please."
    assert recs[1].text == "Try the old town.\n\nIt is walkable."          # one quote level stripped
    assert recs[2].hidden and recs[3].hidden and "chatgpt-memory" in recs[2].tags and "chatgpt-memory" in recs[3].tags
    assert stats["chatgpt_memory_rows"] == 2
    assert {r.source for r in recs} == {"chatgpt"} and recs[0].platform == "chatgpt"
    assert recs[0].conv_title == "Trip planning"
    assert recs[0].ts.isoformat() == "2026-03-05T09:00:00+00:00"          # MM/DD/YYYY: 5 March, in the zone asked for
    assert recs[3].ts.isoformat() == "2026-03-05T21:02:00+00:00"          # 9:02 PM


def test_nexus_timezone_conversion(tmp_path):
    synth.nexus_gpt(tmp_path)
    recs, _, _ = run("nexus_markdown", tmp_path, timezone="America/New_York")
    assert recs[0].ts.isoformat() == "2026-03-05T14:00:00+00:00"          # EST is UTC-5 (US daylight saving starts 8 March)


def test_nexus_claude_dayfirst_and_attachments(tmp_path):
    synth.nexus_claude(tmp_path)
    recs, _, _ = run("nexus_markdown", tmp_path, timezone="UTC")
    assert [r.speaker for r in recs] == ["user", "agent"]
    assert recs[0].ts.isoformat() == "2026-03-10T12:00:00+00:00"
    assert recs[1].text == "Six minutes.\n[attachment: timer.png]\n[artifact: egg-notes v1]"
    assert {"image", "artifact"} <= set(recs[1].tags) and recs[1].platform == "claude.ai"
    assert {r.source for r in recs} == {"claude-ai"}
    flipped, _, _ = run("nexus_markdown", tmp_path, timezone="UTC", date_order="mdy")
    assert flipped[0].ts.isoformat() == "2026-10-03T12:00:00+00:00"


def test_nexus_duplicate_files_keep_the_latest(tmp_path):
    synth.nexus_gpt(tmp_path, "Trip planning.md", update="03/05/2026 at 9:30 AM")
    newer = synth.nexus_gpt(tmp_path, "Trip planning (1).md", update="03/06/2026 at 9:30 AM")
    newer.write_text(newer.read_text().replace("Try the old town.", "Try the old town, newer copy."))
    recs, _, stats = run("nexus_markdown", tmp_path, timezone="UTC")
    assert len(recs) == 4 and any("newer copy" in r.text for r in recs)
    assert stats["conversations"] == 1


def test_nexus_unknown_heading_is_counted_not_guessed(tmp_path):
    p = synth.nexus_gpt(tmp_path)
    p.write_text(p.read_text() + "\n### Someone Else, on 03/05/2026 at 9:05 AM;\n> hi\n")
    _, _, stats = run("nexus_markdown", tmp_path, timezone="UTC")
    assert stats["unknown_headings"] == 1


# ---------------------------------------------------------------- markdown_folder

def test_markdown_folder_chunks_and_frontmatter(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "garden.md").write_text(
        "---\ntitle: Garden notes\ntags: [plants, spring]\ndate: 2026-02-01\n---\n\n"
        + "\n\n".join(f"Paragraph {i} about tulips and beans. " * 12 for i in range(6)))
    (tmp_path / "plain.txt").write_text("A short plain note.")
    (tmp_path / ".hidden.md").write_text("never imported")
    recs, _, stats = run("markdown_folder", tmp_path)
    garden = [r for r in recs if r.conv_id == "sub/garden.md"]
    assert len(garden) > 1 and {r.conv_title for r in garden} == {"Garden notes"}
    assert {"document", "plants", "spring"} <= set(garden[0].tags) and garden[0].context == "notes"
    assert garden[0].ts.isoformat().startswith("2026-02-01")
    assert [r.order for r in garden] == sorted(r.order for r in garden)
    assert not any(".hidden" in r.conv_id for r in recs)
    assert {r.source for r in recs} == {"notes"} and {r.speaker for r in recs} == {"user"}
