# -*- coding: utf-8 -*-
"""外部の歌詞ファイルを使わない取り込みの回帰テスト。"""
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import materials as M
from vocal_engine import score_import as si
from vocal_engine import mcp_server as m
from vocal_engine.phoneme.auto_lyrics import utterance_ranges
from vocal_engine.project import Project


def _vlq(value):
    out = [value & 127]
    value >>= 7
    while value:
        out.insert(0, (value & 127) | 128)
        value >>= 7
    return bytes(out)


def _midi_track(events):
    raw = b"".join(_vlq(delta) + event for delta, event in events) + b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(raw)) + raw


def test_svp_group_offset_tempo_and_lyric(tmp_path):
    obj = {"time": {"tempo": [{"position": 0, "bpm": 120},
                               {"position": si.BLICK * 2, "bpm": 60}],
                    "meter": [{"index": 0, "numerator": 4, "denominator": 4}]},
           "library": [{"uuid": "g", "notes": [
               {"onset": 0, "duration": si.BLICK, "pitch": 60, "lyrics": "あ"},
               {"onset": si.BLICK * 2, "duration": si.BLICK, "pitch": 62, "lyrics": "い"}]}],
           "tracks": [{"name": "主旋律", "mainGroup": {"notes": []},
                       "groups": [{"groupID": "g", "blickOffset": si.BLICK,
                                   "blickAbsoluteBegin": si.BLICK,
                                   "blickAbsoluteEnd": -1, "pitchOffset": 12}]}]}
    path = tmp_path / "small.svp"
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    score = si.read_svp(path)
    notes = score["tracks"][0]["notes"]
    assert [(n["start_sec"], n["end_sec"], n["pitch"], n["lyric"]) for n in notes] == [
        (.5, 1.0, 72, "あ"), (2.0, 3.0, 74, "い")]
    assert [t["bpm"] for t in score["tempo"]] == [120, 60]
    assert score["meter"] == [{"beat": 0.0, "numerator": 4, "denominator": 4}]


def test_midi_variable_tempo_running_status_and_lyrics(tmp_path):
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 2, 480)
    meta = _midi_track([(0, b"\xff\x51\x03\x07\xa1\x20"),
                        (480, b"\xff\x51\x03\x0f\x42\x40")])
    lyric1 = "あ".encode()
    lyric2 = "い".encode()
    data = _midi_track([
        (0, b"\xff\x03\x04Vox!"),
        (0, b"\xff\x05" + _vlq(len(lyric1)) + lyric1),
        (0, b"\x90\x3c\x64"),
        (480, b"\x3c\x00"),  # running status の note-on velocity 0
        (0, b"\xff\x05" + _vlq(len(lyric2)) + lyric2),
        (0, b"\x90\x3e\x64"),
        (480, b"\x80\x3e\x00"),
    ])
    path = tmp_path / "song.mid"
    path.write_bytes(header + meta + data)
    score = si.read_midi(path)
    assert score["tracks"][1]["name"] == "Vox!"
    notes = score["tracks"][1]["notes"]
    assert [(n["start_sec"], n["end_sec"], n["lyric"]) for n in notes] == [
        (0.0, .5, "あ"), (.5, 1.5, "い")]


def test_match_imports_voiced_part_and_reports_missing_part():
    notes = [si._note(0, .3, 60, "あ"), si._note(.3, .6, 62, "い"),
             si._note(2, 2.3, 65, "う"), si._note(2.3, 2.6, 67, "え")]
    take = [SimpleNamespace(id="n0", kind="note", start_sec=.05, end_sec=.35, pitch_midi=60),
            SimpleNamespace(id="n1", kind="note", start_sec=.35, end_sec=.65, pitch_midi=62)]
    entries, matched, missing = si.match_to_take(notes, take, [.05, .35], 3, 0)
    assert len(entries) == 1 and entries[0]["text"] == "あい"
    assert entries[0]["start_sec"] == .05
    assert len(matched) == 1 and matched[0]["onset_pairs"] == 2
    assert len(missing) == 1 and missing[0]["reason"] == "対応する発声がない"


def test_score_pitch_corrects_incorrect_bext_origin():
    notes = [si._note(5 + i * .3, 5.25 + i * .3, 60 + i % 5, "あ") for i in range(12)]
    take = [SimpleNamespace(id=str(i), kind="note", start_sec=i * .3, end_sec=.25 + i * .3,
                            pitch_midi=60 + i % 5) for i in range(12)]
    origin, check = si.estimate_song_start(notes, take, 0)
    assert abs(origin - 5) < .1
    assert check["source"] == "score_pitch"
    kept, check = si.estimate_song_start(notes, take, 5)
    assert kept == 5 and check["source"] == "bext"


def test_nearby_onsets_do_not_confirm_the_wrong_melody():
    notes = [si._note(i * .25, i * .25 + .2, 60 + i, "あ") for i in range(5)]
    take = [SimpleNamespace(id=str(i), kind="note", start_sec=i * .25 + .03,
                            end_sec=i * .25 + .23,
                            pitch_midi=60 + i if i < 2 else 65 + i) for i in range(5)]
    entries, matched, missing = si.match_to_take(
        notes, take, [i * .25 + .03 for i in range(5)], 2, 0)
    assert not entries and not matched
    assert missing[0]["reason"] == "発音の頭と音高の対応が不足"


def test_score_phrases_match_the_list_utterances_boundaries():
    notes = [si._note(0, .2, 60, "あ"), si._note(.2, .4, 62, "い"),
             si._note(.8, 1, 64, "う"), si._note(1, 1.2, 65, "え")]
    take = [SimpleNamespace(id=str(i), kind="note", start_sec=n["start_sec"] + .02,
                            end_sec=n["end_sec"] + .02, pitch_midi=n["pitch"])
            for i, n in enumerate(notes)]
    expected = utterance_ranges(take, 2)
    assert len(si.phrases(notes)) == len(expected) == 2
    entries, matched, unmatched = si.match_to_take(
        notes, take, [n.start_sec for n in take], 2, 0)
    assert len(entries) == len(matched) == 2 and not unmatched
    assert [(x["utterance_start_sec"], x["utterance_end_sec"], x["note_ids"])
            for x in matched] == expected


CLIP_H = Path(M.clip("H"))              # 短いせりふの素材


@pytest.mark.skipif(not CLIP_H.exists(), reason="テスト素材 H が無い（GLISS_TEST_MATERIALS）")
def test_import_overwrites_estimated_lyrics_as_confirmed(tmp_path, monkeypatch):
    clip = CLIP_H
    p = Project.open(str(clip), project_dir=str(tmp_path / "project"))
    estimated = {"start_sec": .1, "end_sec": .5, "text": "あい",
                 "origin": "estimated", "estimate": {"reading": "あい", "confidence": .2}}
    p.set_lyrics_entries([estimated])
    monkeypatch.setitem(m._state, "project", p)
    monkeypatch.setattr(p, "ensure_analyzed", lambda: None)
    monkeypatch.setattr(p, "onsets", lambda: [])
    imported = {"start_sec": .1, "end_sec": .5, "text": "かな"}
    monkeypatch.setattr(si, "match_to_take", lambda *args: ([imported], [], []))
    svp = tmp_path / "small.svp"
    svp.write_text(json.dumps({"tracks": [{"name": "Vocal", "mainGroup": {"notes": [
        {"onset": 0, "duration": si.BLICK, "pitch": 60, "lyrics": "か"},
        {"onset": si.BLICK, "duration": si.BLICK, "pitch": 62, "lyrics": "な"}]}}]},
        ensure_ascii=False), encoding="utf-8")
    result = m.import_lyrics(str(svp), song_start_sec=0, reanalyze=False)
    assert result["ok"], result
    saved = m.get_lyrics()["entries"]
    assert len(saved) == 1 and saved[0]["text"] == "かな"
    assert saved[0]["origin"] == "confirmed"
    assert saved[0]["estimate"] == estimated["estimate"]


@pytest.mark.parametrize("data", [b"", b"not midi", b"MThd\x00\x00\x00\x06"])
def test_invalid_midi_is_rejected(tmp_path, data):
    path = tmp_path / "bad.mid"
    path.write_bytes(data)
    with pytest.raises(si.ScoreImportError):
        si.read_midi(path)
