# -*- coding: utf-8 -*-
"""描画データの差分（`export_view_data(since=…)`。engine/docs/MCP.md「描画データの差分」）。

- 差分の形（数の列の区間・id の列・辞書のキー）と、当てると新しい版と同じになること
- 編集・取り消し・やり直しのたびに since を渡して当てた結果が、毎回その版の全体と一致する
- 覚えていない版・別のトラックの版・忘れた後は全体を返す
"""
import json

import pytest

from test_ara_tools import _add, _ok, _open, _select_and_analyze, _voice, _wav, ara  # noqa: F401


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ================================================================ 差分の形
def test_number_lists_send_only_the_changed_spans():
    from vocal_engine.view.export_data import DIFF_GAP, apply_view_diff, view_diff
    old = {"f0": {"midi": [60.0 + i * 0.01 for i in range(1000)], "n": 1000}, "a": 1}
    new = json.loads(json.dumps(old))
    new["f0"]["midi"][100:110] = [None] * 10                 # null も数と同じに扱う
    new["f0"]["midi"][110 + DIFF_GAP // 2] = 70.0              # 近い変化は 1 つの区間にまとめる
    new["f0"]["midi"][800] = 71.5
    ops = view_diff(old, new)
    spans = ops["$sub"]["f0"]["$sub"]["midi"]["$spans"]
    assert [s for s, _ in spans] == [100, 800]
    assert sum(len(v) for _, v in spans) < 60
    assert set(ops) == {"$sub"} and set(ops["$sub"]) == {"f0"} and set(ops["$sub"]["f0"]) == {"$sub"}
    assert apply_view_diff(old, ops) == new
    assert old["f0"]["midi"][100] is not None                  # 元は書き換えない


def test_lists_with_ids_send_the_order_and_the_changed_items():
    from vocal_engine.view.export_data import apply_view_diff, view_diff
    old = {"notes": [{"id": "n%d" % i, "midi": 60 + i, "text": ""} for i in range(10)]}
    new = json.loads(json.dumps(old))
    new["notes"][3]["midi"] = 72
    del new["notes"][5]
    new["notes"].insert(7, {"id": "n10", "midi": 50, "text": "あ"})
    ops = view_diff(old, new)
    sub = ops["$sub"]["notes"]
    assert sub["$ids"] == [n["id"] for n in new["notes"]]
    assert sorted(n["id"] for n in sub["$put"]) == ["n10", "n3"]
    assert apply_view_diff(old, ops) == new


def test_keys_and_large_changes_are_replaced_whole():
    from vocal_engine.view.export_data import apply_view_diff, view_diff
    old = {"a": [1.0] * 100, "b": "x", "gone": 1, "short": [1.0, 2.0], "same": {"k": [1, 2]}}
    new = {"a": [2.0] * 100, "b": "y", "new": [3], "short": [1.0, 3.0], "same": {"k": [1, 2]}}
    ops = view_diff(old, new)
    assert set(ops["$set"]) == {"a", "b", "new", "short"}      # 半分を超えて変わった列・短い列は丸ごと
    assert ops["$del"] == ["gone"] and "$sub" not in ops
    assert apply_view_diff(old, ops) == new
    assert view_diff(new, new) == {}


# ================================================================ export_view_data(since=…)
def test_since_returns_a_diff_that_rebuilds_each_version(ara, tmp_path):
    """編集・取り消し・やり直しのたびに、前の版に差分を当てた結果がその版の全体と一致する。"""
    from vocal_engine.view.export_data import VIEW_MEM, apply_view_diff
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    tid = _add(a, "mod-A", src)["track"]["id"]
    notes = _select_and_analyze(m, tid)

    r = _ok(m.export_view_data())
    assert r["diff"] is False
    base, rev = _read(r["path"]), r["view_rev"]
    first = (base, rev)

    r = _ok(m.export_view_data(since=rev))                    # 変わっていなければ空の差分
    assert r["diff"] is True and r["view_rev"] == rev and _read(r["path"])["ops"] == {}

    steps = [
        lambda: m.shift_pitch(150, note_id=notes[0]),
        lambda: m.stretch(1.2, note_id=notes[3]),
        lambda: m.shift_pitch(-120, note_id=notes[4]),
        lambda: m.undo(),
        lambda: m.undo(),
        lambda: m.redo(),
        lambda: m.move_note(40, note_id=notes[2]),
    ]
    for i, step in enumerate(steps):
        _ok(step())
        r = _ok(m.export_view_data(since=rev))
        assert r["diff"] is True and r["since"] == rev and r["view_rev"] != rev, i
        d = _read(r["path"])
        assert d["diff_from"] == rev and d["base_dir"] == base["project_dir"], i
        full = _read(r["full_path"])
        new = apply_view_diff(base, d["ops"])
        assert new == full, i
        assert r["bytes"] < r["full_bytes"] / 2, i             # 変わったところだけ
        assert set(r["timing_sec"]) == {"build", "diff"}
        base, rev = new, r["view_rev"]

    # メモリから外れた古い版もファイルから読んで差分を取る
    assert len(steps) > VIEW_MEM
    r = _ok(m.export_view_data(since=first[1]))
    assert r["diff"] is True
    assert apply_view_diff(first[0], _read(r["path"])["ops"]) == _read(r["full_path"])


def test_unknown_or_other_track_versions_return_the_whole(ara, tmp_path):
    from vocal_engine.view.export_data import forget_views
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    ta = _add(a, "mod-A", src)["track"]["id"]
    tb = _add(a, "mod-B", src)["track"]["id"]                  # 同じ素材: 中身の鍵が同じになりうる
    notes = _select_and_analyze(m, ta)
    _ok(m.shift_pitch(100, note_id=notes[1]))
    ra = _ok(m.export_view_data())

    assert _ok(m.export_view_data(since="no-such-rev"))["diff"] is False
    _select_and_analyze(m, tb)
    rb = _ok(m.export_view_data(since=ra["view_rev"]))
    assert rb["diff"] is False                                 # 別のトラックの版とは取らない
    assert _read(rb["path"])["project_dir"] != _read(ra["path"])["project_dir"]

    rb2 = _ok(m.export_view_data(since=rb["view_rev"]))
    assert rb2["diff"] is True
    forget_views()                                             # エンジンの作り直しと同じ
    assert _ok(m.export_view_data(since=rb["view_rev"]))["diff"] is False


def test_explicit_path_ignores_since(ara, tmp_path):
    m, a = ara
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    _select_and_analyze(m, _add(a, "mod-A", src)["track"]["id"])
    rev = _ok(m.export_view_data())["view_rev"]
    out = str(tmp_path / "v.json")
    r = _ok(m.export_view_data(path=out, since=rev))
    assert r["path"].endswith("v.json") and "diff" not in r and r["view_rev"] == rev
    assert _read(out)["for"] == "ui"


@pytest.fixture(autouse=True)
def _forget():
    from vocal_engine.view.export_data import forget_views
    forget_views()
    yield
    forget_views()
