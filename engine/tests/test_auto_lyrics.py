"""推定歌詞の保存、保護、音節修正と取り消し。"""
from pathlib import Path

import numpy as np
import pytest

import materials as M
from vocal_engine import mcp_server as m
from vocal_engine.phoneme.auto_lyrics import decode_logits
from vocal_engine.phoneme.hubertfa import model_found
from vocal_engine.project import Project


CLIP = Path(M.clip("H"))                # 短いせりふの素材


def test_decoder_keeps_consonant_vowel_grammar():
    vocab = {"SP": 0, "ja/a": 1, "ja/k": 2, "ja/o": 3}
    logits = np.full((4, 18), -5.0)
    logits[0, :2] = logits[0, 16:] = 5
    logits[2, 2:5] = 5
    logits[1, 5:10] = 5
    logits[2, 10:12] = 5
    logits[3, 12:16] = 5
    syll = decode_logits(logits, np.full(18, 2.0), vocab)
    assert "".join(s.kana for s in syll) == "かこ"
    assert all(s.start_sec < s.end_sec for s in syll)


def test_decoder_reads_short_closure_before_stop_as_small_tsu():
    vocab = {"SP": 0, "ja/a": 1, "ja/k": 2}
    logits = np.full((3, 50), -8.0)
    logits[0, :3] = logits[0, 19:29] = logits[0, 45:] = 8
    logits[1, 3:19] = logits[1, 34:45] = 8
    logits[2, 29:34] = 8
    syll = decode_logits(logits, np.full(50, 2.0), vocab)
    assert "".join(s.kana for s in syll) == "あっか"


@pytest.mark.skipif(not model_found(), reason="HubertFA の重みが無い")
def test_auto_lyrics_and_single_syllable_correction(tmp_path):
    p = Project.open(str(CLIP), project_dir=str(tmp_path / "auto"))
    p.analyze(with_guide=False)
    entries = p.lyrics_entries("take")
    assert entries and all(e["origin"] == "estimated" for e in entries)
    assert all(e["estimate"]["reading"] == e["text"] for e in entries)
    assert p.phonemes("take").syllables
    assert Project(p.dir).load().lyrics_entries("take") == entries

    m._state["project"] = p
    utterances = m.list_utterances()
    assert utterances["ok"] and utterances["utterances"][0]["note_ids"]
    assert m.get_lyrics()["entries"][0]["origin"] == "estimated"
    note_id = utterances["utterances"][0]["note_ids"][0]
    candidates = [s for s in p.phonemes("take").syllables
                  if s["end_sec"] > p.note(note_id).start_sec
                  and s["start_sec"] < p.note(note_id).end_sec]
    result = m.set_note_syllable(note_id, "ど", syllable_index=candidates[0]["index"])
    assert result["ok"], result
    corrected = p.lyrics_entries("take")[0]
    assert corrected["origin"] == "estimated"
    assert corrected["confirmed_syllables"] == [0]
    assert corrected["estimate"] == entries[0]["estimate"]
    p.undo(result["changeset"])
    assert p.lyrics_entries("take") == entries
    p.analyze(with_guide=False)
    assert p.lyrics_entries("take") == entries
    original = entries[0]
    p.change_lyrics([dict(original, text="今日", origin="confirmed")], source="take")
    p.analyze_phonemes("take", force=True)
    manual_syllable = p.phonemes("take").syllables[0]
    manual_note = next(n for n in p.take_notes if n.start_sec < manual_syllable["end_sec"]
                       and n.end_sec > manual_syllable["start_sec"])
    result = m.set_note_syllable(manual_note.id, "ま",
                                 syllable_index=manual_syllable["index"])
    assert result["ok"], result
    assert p.lyrics_entries("take")[0]["text"] == "今日"
    assert p.lyrics_entries("take")[0]["reading"].startswith("ま")
    p.undo(result["changeset"])
    assert p.lyrics_entries("take")[0]["text"] == "今日"
    assert "reading" not in p.lyrics_entries("take")[0]
    p.change_lyrics([], source="take", author="human")
    restored = Project.from_archive(p.to_archive(), project_dir=str(tmp_path / "restored"))
    restored.analyze(with_guide=False)
    assert restored.lyrics_entries("take") == []


@pytest.mark.skipif(not model_found(), reason="HubertFA の重みが無い")
def test_manual_note_boundaries_hide_estimates_and_undo_restores_them(tmp_path):
    from vocal_engine.analysis.phonemes import get_phonemes
    from vocal_engine.phoneme.lyrics import hidden_estimated_syllable_indices
    from vocal_engine.project import timing as TM
    from vocal_engine.view.export_data import export_view_data
    import json

    p = Project.open(str(CLIP), project_dir=str(tmp_path / "scope"))
    p.analyze(with_guide=False)
    original = p.lyrics_entries("take")
    note = next(n for n in p.take_notes if n.kind == "note" and n.duration_sec > 0.06
                and any(s["end_sec"] > n.start_sec and s["start_sec"] < n.end_sec
                        for s in p.phonemes("take").syllables))
    m._state["project"] = p
    result = m.split_note((note.start_sec + note.end_sec) / 2, note_id=note.id)
    assert result["ok"], result
    assert {result["left"], result["right"]} <= p.estimated_excluded_note_ids()
    hidden = hidden_estimated_syllable_indices(original, p.phonemes("take").syllables,
                                                p.take_notes, p.estimated_excluded_note_ids())
    assert hidden
    assert not hidden.intersection(s["index"] for s in get_phonemes(p)["syllables"])
    with open(export_view_data(p)["path"], encoding="utf-8") as f:
        view = json.load(f)
    assert not hidden.intersection(s["index"] for s in view["phonemes"]["syllables"])
    p.undo(result["changeset"])
    assert p.estimated_excluded_note_ids() == set()
    assert p.lyrics_entries("take") == original
    assert hidden.intersection(s["index"] for s in get_phonemes(p)["syllables"])
    p.redo(result["changeset"])
    assert result["right"] in p.estimated_excluded_note_ids()

    merged = m.merge_notes(result["left"], result["right"])
    assert merged["ok"], merged
    assert result["left"] in p.estimated_excluded_note_ids()
    p.undo(merged["changeset"])
    assert result["right"] in p.estimated_excluded_note_ids()

    p.undo(result["changeset"])
    plan = TM.plan_edge(p, note.id, "end")
    x = min(0.01, plan.x_hi / 2)
    assert x > 0
    cs, _ = TM.apply_plan(p, plan, x)
    assert note.id in p.estimated_excluded_note_ids()
    p.undo(cs.id)
    assert p.estimated_excluded_note_ids() == set()
