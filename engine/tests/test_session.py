# -*- coding: utf-8 -*-
"""セッション（複数トラック）と MCP のトラックのツール（issue #7。`docs/track-view.md`）。

- media: ガイドをずらして切り出すときの無音の詰め物（オフセットが負・終わりを越える）
- 後方互換: session.json の無いプロジェクト（1 テイク＋ガイド）を開くと 2 トラックのセッションになり、
  プロジェクトの中身（編集・ガイドの解析・ガイドの歌詞）はそのまま
- 既存の編集ツールは**編集対象のトラック**に効く（select_track で切り替え）
- 位置（音源全体をずらす）: ガイドとの対応がタイムライン上の位置で取り直される・編集は音と一緒に動く・
  ガイドの歌詞もずれる・書き出しは中身そのまま TimeReference だけ動く
- 再生用のトラックの音（render_tracks）: 編集が無ければ元のファイル、あれば書き出しと同じ中身で長さ不変
- トラックの追加・削除・種類・ガイドの指定の約束（エラーになる操作）
"""
import json
import os
import shutil
import struct

import numpy as np
import pytest
import soundfile as sf

from conftest import CLIP_E, GUIDE, TAKE, needs_clips, needs_model, stem

pytestmark = [needs_clips]


# ---------------------------------------------------------------- media の詰め物
def test_clip_pad_reads_silence_outside_source():
    from vocal_engine import media as M
    info = sf.info(TAKE)
    x, sr = sf.read(TAKE, dtype="float64", always_2d=True)
    total = int(info.frames)
    c = M.Clip(TAKE, offset_frames=-1000, length_frames=total + 2000, pad=True)
    off, n = M.resolve_range(c, sr, total)
    assert (off, n) == (-1000, total + 2000)
    m = M.describe(c, TAKE, role="guide")
    assert m["pad"] is True and m["offset_frames"] == -1000
    y, _ = M.read_clip(m)
    assert y.shape == (total + 2000, x.shape[1])
    assert np.all(y[:1000] == 0) and np.all(y[-1000:] == 0)
    assert np.array_equal(y[1000:1000 + total], x)
    # 詰め物なしのクリップは今までどおりソースの外をエラーにする
    with pytest.raises(M.MediaError):
        M.resolve_range(M.Clip(TAKE, offset_frames=-1), sr, total)
    # 1 サンプルも重ならない範囲は詰め物ありでもエラー
    with pytest.raises(M.MediaError):
        M.resolve_range(M.Clip(TAKE, offset_frames=total, length_frames=10, pad=True), sr, total)
    # 中身のハッシュ（アーカイブの照合）も詰め物込みで読める
    assert M.clip_hash_of(c) == M.clip_audio_hash(m)


def test_guess_kind():
    from vocal_engine.project.session import guess_kind
    assert guess_kind("Inst_mix.wav") == "inst"
    assert guess_kind("song_karaoke.wav") == "inst"
    assert guess_kind("オケ.wav") == "inst"
    assert guess_kind("lead_main(2).wav") == "vocal"
    assert guess_kind("GuideVocal_take3.wav") == "vocal"


# ---------------------------------------------------------------- MCP
@pytest.fixture
def mcp():
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    yield m, mt
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


@needs_model
def test_old_project_becomes_two_track_session(tmp_path, mcp):
    """段階 2 までのプロジェクト（session.json なし）→ テイク＋ガイドの 2 トラック。中身はそのまま。"""
    from vocal_engine.project import Project
    m, mt = mcp
    d = str(tmp_path / "old")
    p = Project.open(TAKE, GUIDE, project_dir=d)
    p.analyze()
    nid = [n for n in p.take_notes if n.kind == "note"][0].id
    from vocal_engine.project.model import Target
    p.apply_edits([{"kind": "pitch_shift", "target": Target.note(nid), "params": {"cents": 30.0}}])
    p.set_lyrics_entries([{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"}], source="guide")
    g_before = dict(p.guide)
    mtime = os.path.getmtime(os.path.join(d, "cache", "guide-analysis.json"))
    assert not os.path.exists(os.path.join(d, "session.json"))

    r = _ok(m.open_project(TAKE, project_dir=d))          # ガイドを省いて開き直す（画面の起動と同じ）
    ss = r["session"]
    assert [t["name"] for t in ss["tracks"]] == [stem(TAKE), stem(GUIDE)]
    assert ss["guide"] == ss["tracks"][1]["id"] and ss["current"] == ss["tracks"][0]["id"]
    assert all(t["kind"] == "vocal" and t["offset_sec"] == 0.0 for t in ss["tracks"])
    assert r["edits"] == 1
    p2 = m._state["project"]
    assert p2.dir == os.path.abspath(d)
    assert p2.guide["sha256"] == g_before["sha256"] and p2.guide["offset_frames"] == 0
    assert p2.guide["frames"] == g_before["frames"]
    # ガイドは同じもの: 解析のキャッシュも歌詞も残る
    assert os.path.getmtime(os.path.join(d, "cache", "guide-analysis.json")) == mtime
    assert p2.lyrics_entries("guide")[0]["text"] == "あいうえ"
    assert json.load(open(os.path.join(d, "session.json"), encoding="utf-8"))["format"] == \
        "vocal-editor-session"
    # 開き直しても同じセッション（トラックは増えない）
    r2 = _ok(m.open_project(TAKE, project_dir=d))
    assert len(r2["session"]["tracks"]) == 2
    assert r2["session"]["last_current"] == ss["current"]


@needs_model
def test_tools_act_on_selected_track(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "sel")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(m.analyze_take())
    _ok(mt.select_track(t2))
    p2 = m._state["project"]
    assert p2.dir == os.path.join(os.path.abspath(d), "tracks", os.path.basename(p2.dir))
    assert p2.guide is None                      # 編集対象がガイドのトラック自身: 重ねない
    _ok(m.analyze_take())
    nid = _ok(m.list_notes())["notes"][0]["id"]
    _ok(m.shift_pitch(cents=40, note_id=nid))
    with open(os.path.join(p2.dir, "project.json"), encoding="utf-8") as f:
        assert len(json.load(f)["edits"]) == 1
    with open(os.path.join(d, "project.json"), encoding="utf-8") as f:
        assert len(json.load(f)["edits"]) == 0
    # 戻すと元のテイクの編集リスト・ガイドが戻る
    r = _ok(mt.select_track(t1))
    assert r["edits"] == 0 and r["guide"] is not None
    lt = _ok(mt.list_tracks())
    assert [t["current"] for t in lt["tracks"]] == [True, False]
    # 伴奏は選べない
    _ok(mt.add_track(CLIP_E, kind="inst"))
    t3 = _ok(mt.list_tracks())["tracks"][2]["id"]
    assert mt.select_track(t3)["ok"] is False
    assert mt.set_guide_track(t3)["ok"] is False


@needs_model
def test_offset_moves_guide_correspondence(tmp_path, mcp):
    """位置をずらすと、ガイドはタイムライン上の位置で切り出し直される。編集はテイクと一緒に動く。"""
    m, mt = mcp
    d = str(tmp_path / "off")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(m.analyze_take())
    _ok(m.set_lyrics(entries=[{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"}],
                     source="guide", reanalyze=False))
    nid = _ok(m.list_notes())["notes"][1]["id"]
    _ok(m.shift_pitch(cents=25, note_id=nid))
    sr = sf.info(GUIDE).samplerate
    total = sf.info(GUIDE).frames

    # テイクを 0.25 秒後ろへ: テイクの頭（タイムライン 0.25）= ガイドのファイルの 0.25 秒
    r = _ok(mt.set_track(t1, offset_sec=0.25))
    assert r["reopened"] is True
    p = m._state["project"]
    assert p.guide["offset_frames"] == int(round(0.25 * sr)) and not p.guide["pad"]
    assert p.guide["frames"] == total - int(round(0.25 * sr))
    # ガイドの歌詞もガイド内の秒でずれる（0.5〜1.5 → 0.25〜1.25）
    e = p.lyrics_entries("guide")[0]
    assert abs(e["start_sec"] - 0.25) < 1e-6 and abs(e["end_sec"] - 1.25) < 1e-6
    # 編集はテイクの頭からの秒のまま（音と一緒に動く）
    assert [x.target.note_id for x in p.edits] == [nid]
    _ok(m.analyze_take())
    assert _ok(m.list_deviations())["total"] >= 0

    # ガイドの方を 0.25 秒後ろへ: 差が 0 に戻る → ファイル全体（段階 2 までと同じ）
    r = _ok(mt.set_track(t2, offset_sec=0.25))
    assert r["reopened"] is True
    p = m._state["project"]
    assert p.guide["offset_frames"] == 0 and p.guide["frames"] == total
    # テイクを前へ（ガイドより 0.4 秒前）: ガイドの頭を無音で詰める
    _ok(mt.set_track(t1, offset_sec=-0.15))
    p = m._state["project"]
    assert p.guide["offset_frames"] == -int(round(0.4 * sr)) and p.guide["pad"]
    gx, _ = p.audio("guide")
    assert np.all(gx[:int(round(0.4 * sr))] == 0)
    _ok(m.analyze_take())
    # ミュート・名前だけなら開き直さない
    assert _ok(mt.set_track(t2, mute=True, name="guide"))["reopened"] is False
    # 外部（別のプロセス）が位置を変えた: guide_stale で分かる。select_track で開き直すと直る
    from vocal_engine.project.session import Session
    other = Session.load(d)
    other.track(t2)["offset_sec"] = 1.0
    other.save()
    assert _ok(mt.list_tracks())["guide_stale"] is True
    _ok(mt.select_track(t1))
    assert _ok(mt.list_tracks())["guide_stale"] is False


def _tref(path):
    with open(path, "rb") as f:
        b = f.read()
    i = 12
    while i + 8 <= len(b):
        cid = b[i:i + 4]
        n = struct.unpack_from("<I", b, i + 4)[0]
        if cid == b"bext":
            return struct.unpack_from("<Q", b, i + 8 + 338)[0]
        i += 8 + n + (n & 1)
    return None


@needs_model
def test_export_moves_time_reference_by_offset(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "exp")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1 = r["session"]["tracks"][0]["id"]
    _ok(m.analyze_take())
    nid = _ok(m.list_notes())["notes"][0]["id"]
    _ok(m.shift_pitch(cents=30, note_id=nid))
    base = _ok(m.export_wav(path=str(tmp_path / "a.wav"), background=False))
    assert base["timeline_offset_sec"] == 0.0 and _tref(base["path"]) is None
    _ok(mt.set_track(t1, offset_sec=0.5))
    e = _ok(m.export_wav(path=str(tmp_path / "b.wav"), background=False))
    sr = e["sr"]
    assert e["timeline_offset_sec"] == 0.5
    assert _tref(e["path"]) == int(round(0.5 * sr))      # bext が無い元でも位置を入れる
    # 中身・長さは同じ
    a, _ = sf.read(base["path"], dtype="int32")
    b, _ = sf.read(e["path"], dtype="int32")
    assert np.array_equal(a, b)
    # 前へずらして 0 より前: 0 にして警告
    _ok(mt.set_track(t1, offset_sec=-0.5))
    e = _ok(m.export_wav(path=str(tmp_path / "c.wav"), background=False))
    assert _tref(e["path"]) == 0 and any("0 にした" in w for w in e["warnings"])


@needs_model
def test_render_tracks_stems(tmp_path, mcp):
    m, mt = mcp
    d = str(tmp_path / "stem")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.set_track(t2, offset_sec=0.1))
    _ok(m.analyze_take())
    rt = _ok(mt.render_tracks(background=False))
    by = {x["id"]: x for x in rt["tracks"]}
    assert by[t1]["path"] == os.path.abspath(TAKE) and by[t1]["edited"] is False
    assert by[t2]["path"] == os.path.abspath(GUIDE) and by[t2]["start_sec"] == 0.1
    nid = _ok(m.list_notes())["notes"][2]["id"]
    _ok(m.shift_pitch(cents=60, note_id=nid))
    rt = _ok(mt.render_tracks(track_ids=[t1], background=False))
    s1 = rt["tracks"][0]
    assert s1["edited"] and os.path.exists(s1["path"])
    y, sr = sf.read(s1["path"], dtype="float64", always_2d=True)
    x, _ = sf.read(TAKE, dtype="float64", always_2d=True)
    assert y.shape == x.shape
    # 書き出しと同じ中身: 差し替えた窓の外は元と同じサンプル
    e = _ok(m.export_wav(path=str(tmp_path / "e.wav"), background=False))
    ex, _ = sf.read(e["path"], dtype="float64", always_2d=True)
    assert np.max(np.abs(ex - y)) < 1e-6
    diff = np.any(np.abs(y - x) > 1e-7, axis=1)
    lo, hi = [int(round(v * sr)) for v in e["replaced_spans_sec"][0]]
    assert not diff[:lo].any() and not diff[hi:].any() and diff[lo:hi].any()
    # 同じ編集なら同じファイル、編集が変われば別のファイル
    assert _ok(mt.render_tracks(track_ids=[t1], background=False))["tracks"][0]["path"] == s1["path"]
    _ok(m.undo())
    s2 = _ok(mt.render_tracks(track_ids=[t1], background=False))["tracks"][0]
    assert s2["edited"] is False and s2["path"] == os.path.abspath(TAKE)


def test_track_overview_signed_levels(tmp_path, mcp):
    from vocal_engine.project.session import open_session
    m, mt = mcp
    s, p, _ = open_session(TAKE, GUIDE, project_dir=str(tmp_path / "ov"))
    mt.adopt(s, p, s.current)
    r = _ok(mt.track_overview())
    for t in r["tracks"]:
        with open(t["path"], encoding="utf-8") as f:
            ov = json.load(f)
        path = s.track(t["id"])["path"]
        x, sr = sf.read(path, dtype="float32", always_2d=True)
        assert ov["version"] == 2 and ov["sr"] == sr
        assert ov["channels"] == x.shape[1]
        raw = np.fromfile(t["binary_path"], dtype="int8")
        assert raw.size == sum(v["length"] * x.shape[1] * 2 for v in ov["levels"])
        assert [v["hop"] for v in ov["levels"]] == [32, 128, 512, 2048, 8192]
        for level in ov["levels"]:
            hop = level["hop"]
            assert level["length"] == int(np.ceil(len(x) / hop))
            pairs = raw[level["offset"]:level["offset"] + level["length"] * x.shape[1] * 2]
            pairs = pairs.reshape(-1, x.shape[1], 2)
            for i in (0, level["length"] // 2, level["length"] - 1):
                segment = x[i * hop:(i + 1) * hop]
                assert np.all(pairs[i, :, 0] / 127 <= segment.min(axis=0) + 1e-6)
                assert np.all(pairs[i, :, 1] / 127 >= segment.max(axis=0) - 1e-6)
                assert np.max(np.abs(pairs[i] / 127 - np.stack((segment.min(axis=0), segment.max(axis=0)), axis=1))) <= 1 / 127 + 1e-6
        assert _ok(mt.track_overview(track_ids=[t["id"]]))["tracks"][0]["path"] == t["path"]


def test_add_remove_and_rules(tmp_path, mcp):
    from vocal_engine.project.session import open_session, Session
    m, mt = mcp
    inst = str(tmp_path / "Inst_mix.wav")
    shutil.copy(CLIP_E, inst)
    s, p, _ = open_session(TAKE, project_dir=str(tmp_path / "rules"))
    mt.adopt(s, p, s.current)
    t1 = s.current
    r = _ok(mt.add_track(inst, select=True))                   # 伴奏は select でも選ばない
    ti = r["track"]
    assert s.track(ti)["kind"] == "inst" and r["selected"] is False
    assert mt.current_track_id() == t1
    assert mt.add_track(inst)["ok"] is False                   # 同じファイルは 2 本にしない
    assert mt.remove_track(t1)["ok"] is False                  # ボーカルが無くなる
    assert mt.set_track(t1, kind="inst")["ok"] is False        # 編集中は伴奏にできない
    r = _ok(mt.add_track(GUIDE, guide=True))                  # 足してガイドに
    tg = r["track"]
    assert r["reopened"] is True and m._state["project"].guide is not None
    _ok(mt.set_track(tg, kind="inst"))                         # ガイドを伴奏にすると指定が外れる
    assert _ok(mt.list_tracks())["guide"] is None
    assert m._state["project"].guide is None
    _ok(mt.set_track(tg, kind="vocal"))
    _ok(mt.set_guide_track(tg))
    # 編集中のトラックを外す → 残りのボーカルが編集対象
    r = _ok(mt.remove_track(t1))
    assert r["switched_to"] == tg and mt.current_track_id() == tg
    assert os.path.exists(os.path.join(str(tmp_path / "rules"), "project.json"))   # 編集は消さない
    # ソロ・ミュートの聞こえ方
    _ok(mt.set_track(ti, solo=True))
    lt = _ok(mt.list_tracks())
    assert {t["id"]: t["audible"] for t in lt["tracks"]} == {tg: False, ti: True}
    # 新しい版の session.json は読まない（上書きしない）
    sp = os.path.join(str(tmp_path / "rules"), "session.json")
    with open(sp, encoding="utf-8") as f:
        d = json.load(f)
    d["version"] = 99
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(Exception):
        Session.load(str(tmp_path / "rules"))
    assert m.open_project(TAKE, project_dir=str(tmp_path / "rules"))["ok"] is False
    # 壊れた session.json は退避して作り直す
    with open(sp, "w", encoding="utf-8") as f:
        f.write("{broken")
    r = _ok(m.open_project(TAKE, project_dir=str(tmp_path / "rules")))
    # テイクと、テイクのプロジェクトに残っていたガイドから作り直す
    assert [t["name"] for t in r["session"]["tracks"]] == [stem(TAKE), stem(GUIDE)]
    assert any(n.startswith("session.json.broken-") for n in os.listdir(str(tmp_path / "rules")))


@needs_model
def test_mcp_stdio_lists_track_tools():
    """MCP のツール一覧にトラックのツールが載っている（画面・Claude Code から呼べる）。"""
    from vocal_engine import mcp_server as m
    names = [f.__name__ for f in m.TOOLS]
    for n in ("list_tracks", "select_track", "add_track", "remove_track", "set_track",
              "set_guide_track", "track_overview", "render_tracks"):
        assert n in names
    assert len(names) == len(set(names))


def test_reopen_after_primary_removed_and_readded(tmp_path, mcp):
    """最初のテイクのトラックを外して足し直しても、そのファイルで開き直せる（トラックは増えない）。"""
    m, mt = mcp
    d = str(tmp_path / "prim")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.remove_track(t1))
    t3 = _ok(mt.add_track(TAKE))["track"]
    r = _ok(m.open_project(TAKE, project_dir=d))
    assert [t["id"] for t in r["session"]["tracks"]] == [t2, t3]
    assert r["session"]["current"] == t3 and r["session"]["guide"] == t2
    # 外しただけ（足し直していない）なら、最初のテイクとして戻る
    d2 = str(tmp_path / "prim2")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d2))
    a, b = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.remove_track(a))
    r = _ok(m.open_project(TAKE, project_dir=d2))
    assert [t["name"] for t in r["session"]["tracks"]] == [stem(TAKE), stem(GUIDE)]
    assert r["project_dir"] == os.path.abspath(d2)


# ---------------------------------------------------------------- セルフレビューで見つけたもの
@needs_model
def test_guide_lyrics_survive_move_and_unset(tmp_path, mcp):
    """ガイドの歌詞は、前へずらして戻す・ガイドを外して付け直す・別のテイクに重ねる、で消えない。"""
    m, mt = mcp
    d = str(tmp_path / "gl")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    ent = [{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"},
           {"start_sec": 2.2, "end_sec": 3.4, "text": "かきくけ"}]
    _ok(m.set_lyrics(entries=ent, source="guide", reanalyze=False))
    texts = lambda: [e["text"] for e in m._state["project"].lyrics_entries("guide")]   # noqa: E731
    _ok(mt.set_track(t2, offset_sec=-1.5))          # 前へ: 最初の区間は切り出しの外
    assert texts() == ["かきくけ"]
    _ok(mt.set_track(t2, offset_sec=0.0))           # 戻す: 両方戻る
    assert texts() == ["あいうえ", "かきくけ"]
    assert m._state["project"].lyrics_entries("guide")[0]["start_sec"] == 0.5
    _ok(mt.set_guide_track(None))
    assert m._state["project"].guide is None
    _ok(mt.set_guide_track(t2))
    assert texts() == ["あいうえ", "かきくけ"]
    # 素材全体の歌詞（区間なし）はずらしても残る
    _ok(m.set_lyrics(text="あいうえ", source="guide", reanalyze=False))
    _ok(mt.set_track(t2, offset_sec=0.2))
    assert texts() == ["あいうえ"]


@needs_model
def test_missing_guide_file_keeps_project_guide(tmp_path, mcp):
    """段階 2 までのプロジェクトで、ガイドの音声が一時的に見つからなくても、ガイド・解析・歌詞を捨てない。"""
    from vocal_engine.project import Project
    m, mt = mcp
    g = str(tmp_path / "guide.wav")
    shutil.copy(GUIDE, g)
    d = str(tmp_path / "miss")
    p = Project.open(TAKE, g, project_dir=d)
    p.set_lyrics_entries([{"start_sec": 0.5, "end_sec": 1.5, "text": "あいうえ"}], source="guide")
    os.rename(g, g + ".away")
    r = _ok(m.open_project(TAKE, project_dir=d))
    assert r["guide"] is not None and r["guide"]["path"] == g
    assert len(r["session"]["tracks"]) == 1
    assert m._state["project"].lyrics_entries("guide")
    os.rename(g + ".away", g)                        # 戻ってきた: ガイドのトラックになる
    r = _ok(m.open_project(TAKE, project_dir=d))
    assert [t["name"] for t in r["session"]["tracks"]] == [stem(TAKE), "guide"]
    assert r["session"]["guide"] == r["session"]["tracks"][1]["id"]
    assert m._state["project"].lyrics_entries("guide")[0]["text"] == "あいうえ"


def test_primary_kind_and_replacement(tmp_path, mcp):
    """先頭のテイクを伴奏にされても、そのファイルで開けば編集できる。別の素材に差し替えたら位置などは引き継がない。"""
    m, mt = mcp
    d = str(tmp_path / "pk")
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=d))
    t1, t2 = [t["id"] for t in r["session"]["tracks"]]
    _ok(mt.select_track(t2))
    _ok(mt.set_track(t1, kind="inst", offset_sec=2.0, mute=True))
    r = _ok(m.open_project(TAKE, project_dir=d))
    assert r["session"]["tracks"][0]["kind"] == "vocal" and r["session"]["current"] == t1
    r = _ok(m.open_project(CLIP_E, project_dir=d))  # 同じ置き場に別の素材
    t = r["session"]["tracks"][0]
    assert t["name"] == stem(CLIP_E) and t["offset_sec"] == 0.0 and t["mute"] is False
    assert mt.current_offset_sec() == 0.0


def test_failed_change_does_not_leak(tmp_path, mcp):
    """検証に落ちた変更は、メモリにも session.json にも残らない。"""
    from vocal_engine.project.session import open_session
    m, mt = mcp
    inst = str(tmp_path / "Inst_x.wav")
    shutil.copy(CLIP_E, inst)
    s, p, _ = open_session(TAKE, project_dir=str(tmp_path / "leak"))
    mt.adopt(s, p, s.current)
    assert mt.add_track(inst, guide=True)["ok"] is False      # 伴奏はガイドにできない
    assert len(_ok(mt.list_tracks())["tracks"]) == 1
    assert mt.set_track(s.current, name="x", offset_sec=float("nan"))["ok"] is False
    assert _ok(mt.list_tracks())["tracks"][0]["name"] == stem(TAKE)


def test_render_tracks_skips_missing_file(tmp_path, mcp):
    """音声が無いトラックがあっても、他のトラックは鳴らせる（そのトラックだけ error）。"""
    from vocal_engine.project.session import open_session
    m, mt = mcp
    e = str(tmp_path / "E.wav")
    shutil.copy(CLIP_E, e)
    s, p, _ = open_session(TAKE, project_dir=str(tmp_path / "rm"))
    mt.adopt(s, p, s.current)
    _ok(mt.add_track(e, kind="inst"))
    os.remove(e)
    r = _ok(mt.render_tracks(background=False))
    assert [("error" in x) for x in r["tracks"]] == [False, True]
    ov = _ok(mt.track_overview())
    assert [("error" in x) for x in ov["tracks"]] == [False, True]


@needs_model
def test_export_warns_when_source_has_time_reference(tmp_path, mcp):
    """DAW で途中から録ったテイク（bext あり）をずらして書き出すと、二重にずれることを警告する。"""
    from vocal_engine import bwf
    m, mt = mcp
    src = str(tmp_path / "vo.wav")
    shutil.copy(TAKE, src)
    tmp = src + ".b.wav"
    shutil.copy(src, tmp)
    bwf.write_meta(tmp, bwf.BwfMeta(bext=bwf.minimal_bext(48000), form="RIFF"))
    os.replace(tmp, src)
    r = _ok(m.open_project(src, project_dir=str(tmp_path / "bx")))
    _ok(m.analyze_take())
    nid = _ok(m.list_notes())["notes"][0]["id"]
    _ok(m.shift_pitch(cents=20, note_id=nid))
    e = _ok(m.export_wav(path=str(tmp_path / "a.wav"), background=False))
    assert not any("二重" in w or "DAW 上の位置（" in w for w in e["warnings"])
    _ok(mt.set_track(r["session"]["current"], offset_sec=1.0))
    e = _ok(m.export_wav(path=str(tmp_path / "b.wav"), background=False))
    assert _tref(e["path"]) == 48000 * 2
    assert any("DAW 上の位置（1.000 秒）" in w for w in e["warnings"])


@needs_model
def test_offset_goes_into_guide_timing_global_offset(tmp_path, mcp):
    """#12・#61 との関係: トラックの位置をずらした量は測った「全体のずれ」に入る。

    150 ms 以内（歌い手の走り・もたり、レイテンシ程度のずらし）なら、合わせる基準はタイムライン上の
    ガイドの位置そのもの（ずらした量ごと合わせる。issue #61）。それを超えてずらしたら、置き場所の違う
    素材として以前どおり全体のずれを足した位置を基準にする。"""
    m, mt = mcp
    r = _ok(m.open_project(TAKE, GUIDE, project_dir=str(tmp_path / "gt")))
    t1 = r["session"]["tracks"][0]["id"]
    _ok(m.analyze_take(background=False))
    gt0 = m._state["project"].guide_timing()
    base = gt0.measured_offset_sec
    assert gt0.basis == "timeline" and gt0.offset_sec == 0.0
    _ok(mt.set_track(t1, offset_sec=-0.05))
    _ok(m.analyze_take(background=False))
    gt = m._state["project"].guide_timing()
    assert abs((gt.measured_offset_sec - base) - (-0.05)) < 0.03
    assert gt.basis == "timeline" and gt.offset_sec == 0.0
    _ok(mt.set_track(t1, offset_sec=-0.2))
    _ok(m.analyze_take(background=False))
    gt = m._state["project"].guide_timing()
    assert abs((gt.measured_offset_sec - base) - (-0.2)) < 0.03
    assert gt.basis == "offset" and gt.offset_sec == gt.measured_offset_sec
    assert m._state["project"].guide["pad"] is True
