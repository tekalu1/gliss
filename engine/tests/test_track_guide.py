# -*- coding: utf-8 -*-
"""トラックごとのガイド（`set_track_guide`・トラックの `guide_id`。engine/docs/MCP.md §2「トラックごとのガイド」）。

音は合成（`test_ara_tools._voice`。同じ旋律を半音いくつか上下させた歌声もどき）。ピッチ検出は重みの要らない Praat。

- 2 つのテイクに別々のガイドを指定すると、それぞれの list_deviations / correct_to_guide が自分のガイドに対して計算される
  （ガイドの音高が違えば結果が変わる）。指定が無いトラックは共通のガイド（set_guide_track）
- 取り消し・やり直し（1 回で 1 つ）。エラー（自分自身・無いトラック・伴奏）
- トラックを外す・伴奏にすると、そのトラックを指していた指定は共通のガイドに戻る（取り消しで戻る）
- .gliss の保存 → 読み込み。guide_id の無い古い .gliss も読める（キーごと持たないのでバイト互換）
- ARA: ara_archive の `guides` → 別の作業場所で ara_sync(guides=…) で戻る（履歴に入らない）・ガイドの修飾を外すと指定が外れる・
  古いアーカイブ（guides 無し）・DAW の取り消しでは戻らない指定を Gliss の undo で戻す・中継（外部の AI）の set_track_guide
- 裏の準備: ガイドごとに署名が別・ガイドとして使われているトラックを先に準備する
"""
import json
import os

import pytest

from test_ara_tools import _add, _ok, _open, _voice, _wav


# ================================================================ 共通の部品
@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_tracks as mt
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    F.set_preferred_estimator("praat")           # 重みが無くても解析できる
    yield m, mt, md
    F.set_preferred_estimator(None)
    md._clear()
    m._state.update(project=None, session=None, track=None, document=None)
    m._invalidate_renderer()


def _media(tmp_path, up=3, down=-3):
    """テイク 2 本（同じ旋律）と、音高の違うガイド 2 本（テイクより +up / down 半音）。"""
    d = tmp_path / "media"
    return {"t1": _wav(d / "take1.wav", _voice(seed=1)),
            "t2": _wav(d / "take2.wav", _voice(seed=2)),
            "gA": _wav(d / "guideA.wav", _voice(transpose=up, seed=3)),
            "gB": _wav(d / "guideB.wav", _voice(transpose=down, seed=4))}


def _session(mcp, tmp_path, **kw):
    """無題のプロジェクトにテイク 2 本とガイド 2 本を足す。{名前: トラック id}。"""
    m, mt, md = mcp
    paths = _media(tmp_path, **kw)
    _ok(md.new_project())
    ids = {"t1": _ok(mt.add_track(paths["t1"], select=True))["track"]}
    for k in ("t2", "gA", "gB"):
        ids[k] = _ok(mt.add_track(paths[k]))["track"]
    return paths, ids


def _rows(mt):
    return {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}


def _devs(m, mt, tid):
    """トラックを選んで解析し、list_deviations のノートごとのセントを返す。"""
    _ok(mt.select_track(tid))
    _ok(m.analyze_take(background=False))
    rows = _ok(m.list_deviations(threshold_cents=0.0, threshold_ms=1000.0))["deviations"]
    return [r["pitch_cents"] for r in rows]


# ================================================================ 2 つのテイクが別々のガイドに合う
def test_each_take_is_measured_against_its_own_guide(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    r = _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    assert r["guide_id"] == ids["gA"] and r["effective_guide_id"] == ids["gA"]
    _ok(mt.set_track_guide(ids["t2"], ids["gB"]))

    a = _devs(m, mt, ids["t1"])
    assert os.path.normcase(m._state["project"].guide["path"]) == os.path.normcase(paths["gA"])
    assert a and all(v == pytest.approx(-300.0, abs=15) for v in a)       # テイクはガイド A より 3 半音低い
    b = _devs(m, mt, ids["t2"])
    assert os.path.normcase(m._state["project"].guide["path"]) == os.path.normcase(paths["gB"])
    assert b and all(v == pytest.approx(300.0, abs=15) for v in b)        # ガイド B より 3 半音高い
    # 選び直しても自分のガイド
    assert _devs(m, mt, ids["t1"]) == a

    # correct_to_guide も自分のガイドへ寄せる（A は上へ、B は下へ）
    def shift_of(tid):
        _ok(mt.select_track(tid))
        _ok(m.correct_to_guide(pitch_strength=1.0, match_pitch_shape=False))
        ps = [e.params["cents"] for e in m._state["project"].edits if e.kind == "pitch_shift"]
        assert ps
        return sum(ps) / len(ps)
    assert shift_of(ids["t1"]) == pytest.approx(300.0, abs=15)
    assert shift_of(ids["t2"]) == pytest.approx(-300.0, abs=15)
    # 補正後は自分のガイドに対する残差が小さい（measure_against_guide も実効のガイド）
    from vocal_engine import mcp_measure as MM
    r = _ok(MM.measure_against_guide(render=False))
    assert r["rows"]
    after = [row["pitch_cents"]["edited"] for row in r["rows"] if row["pitch_cents"]["edited"] is not None]
    assert after and all(abs(v) < 40 for v in after)


def test_list_tracks_reports_guide_fields_and_shared_fallback(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    rows = _rows(mt)
    assert all(t["guide_id"] is None and t["effective_guide_id"] is None for t in rows.values())
    assert not any(t["is_guide"] for t in rows.values())

    _ok(mt.set_guide_track(ids["gB"]))                      # 共通のガイド
    rows = _rows(mt)
    assert rows[ids["gB"]]["guide"] is True and rows[ids["gB"]]["effective_guide_id"] is None   # 自分自身には無い
    assert rows[ids["t1"]]["effective_guide_id"] == ids["gB"] and rows[ids["t1"]]["guide_id"] is None
    assert sorted(rows[ids["gB"]]["guide_for"]) == sorted([ids["t1"], ids["t2"], ids["gA"]])

    _ok(mt.set_track_guide(ids["t1"], ids["gA"]))           # t1 だけ別のガイド
    rows = _rows(mt)
    assert rows[ids["t1"]]["guide_id"] == ids["gA"] and rows[ids["t1"]]["effective_guide_id"] == ids["gA"]
    assert rows[ids["t2"]]["effective_guide_id"] == ids["gB"]                       # 指定の無いものは共通
    assert rows[ids["gA"]]["is_guide"] is True and rows[ids["gA"]]["guide_for"] == [ids["t1"]]
    assert rows[ids["gB"]]["guide"] is True                                         # `guide` は共通のガイドのまま
    assert sorted(rows[ids["gB"]]["guide_for"]) == sorted([ids["t2"], ids["gA"]])

    # 共通のガイドを外しても、トラックごとの指定は残る
    _ok(mt.set_guide_track(None))
    rows = _rows(mt)
    assert rows[ids["t1"]]["effective_guide_id"] == ids["gA"] and rows[ids["t2"]]["effective_guide_id"] is None
    # 指定を外すと共通のガイド（ここでは無し）に戻る
    r = _ok(mt.set_track_guide(ids["t1"], None))
    assert r["guide_id"] is None and r["effective_guide_id"] is None


def test_set_track_guide_defaults_to_current_track_and_validates(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    assert mt.current_track_id() == ids["t1"]
    r = _ok(mt.set_track_guide(guide_track_id=ids["gA"]))   # track_id 省略 = 編集対象
    assert r["track"] == ids["t1"] and r["effective_guide_id"] == ids["gA"]
    assert r["reopened"] is True                            # 編集対象のガイドが変わった
    assert _ok(mt.set_track_guide(ids["t2"], ids["gB"]))["reopened"] is False   # 別のトラックは開き直さない

    for kw, msg in ((dict(track_id=ids["t1"], guide_track_id=ids["t1"]), "自身"),
                    (dict(track_id=ids["t1"], guide_track_id="t99"), "トラックが無い"),
                    (dict(track_id="t99", guide_track_id=ids["gA"]), "トラックが無い")):
        r = mt.set_track_guide(**kw)
        assert r["ok"] is False and msg in r["error"], r
    # 失敗しても指定は変わらない
    assert _rows(mt)[ids["t1"]]["guide_id"] == ids["gA"]


def test_inst_tracks_cannot_be_guides_or_have_one(mcp, tmp_path):
    import shutil
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    karaoke = str(tmp_path / "media" / "karaoke.wav")
    shutil.copy(paths["gA"], karaoke)
    inst = _ok(mt.add_track(karaoke))["track"]
    assert _rows(mt)[inst]["kind"] == "inst"
    assert mt.set_track_guide(ids["t1"], inst)["ok"] is False
    assert mt.set_track_guide(inst, ids["gA"])["ok"] is False
    # ガイドを伴奏にしたら、それを指していた指定は共通のガイドに戻る
    _ok(mt.set_track_guide(ids["t2"], ids["gB"]))
    _ok(mt.set_track(ids["gB"], kind="inst"))
    rows = _rows(mt)
    assert rows[ids["t2"]]["guide_id"] is None and rows[ids["t2"]]["effective_guide_id"] is None


# ================================================================ 取り消し・やり直し
def test_set_track_guide_is_one_undoable_step(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    _ok(mt.set_guide_track(ids["gB"]))
    _devs(m, mt, ids["t1"])                                  # 共通のガイド B（テイク − B = +300）
    r = _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    h = r["session"]["history"]
    assert h["undo"]["label"] == "ガイドの指定" and h["undo"]["track"] == ids["t1"]
    n = h["size"]
    assert os.path.normcase(m._state["project"].guide["path"]) == os.path.normcase(paths["gA"])

    # 同じ値をもう一度指定しても履歴は増えない
    r = _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    assert r["session"]["history"]["size"] == n

    u = _ok(m.undo())                                        # 1 回で戻る
    assert u["undone"]["label"] == "ガイドの指定"
    assert _rows(mt)[ids["t1"]]["guide_id"] is None
    assert u["reopened"] is True
    assert os.path.normcase(m._state["project"].guide["path"]) == os.path.normcase(paths["gB"])
    assert "guide_id" not in m._state["session"].track(ids["t1"])      # キーごと外れる
    rd = _ok(m.redo())
    assert rd["redone"]["label"] == "ガイドの指定"
    assert _rows(mt)[ids["t1"]]["guide_id"] == ids["gA"]
    assert os.path.normcase(m._state["project"].guide["path"]) == os.path.normcase(paths["gA"])
    # 取り消しの先頭で外したら、次の undo はその前の操作
    _ok(mt.set_track_guide(ids["t1"], None))
    assert _ok(m.undo())["undone"]["label"] == "ガイドの指定"
    assert _rows(mt)[ids["t1"]]["guide_id"] == ids["gA"]


def test_removing_a_guide_track_drops_the_references_and_undo_restores(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    _ok(mt.set_track_guide(ids["t2"], ids["gA"]))
    _ok(mt.remove_track(ids["gA"]))
    rows = _rows(mt)
    assert rows[ids["t1"]]["guide_id"] is None and rows[ids["t2"]]["guide_id"] is None
    _ok(m.undo())                                            # トラックを外すを取り消す: 指定も戻る
    rows = _rows(mt)
    assert rows[ids["t1"]]["guide_id"] == ids["gA"] and rows[ids["t2"]]["guide_id"] == ids["gA"]
    assert rows[ids["gA"]]["is_guide"] is True


# ================================================================ .gliss の保存・読込
def test_gliss_roundtrip_keeps_track_guides(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    _ok(mt.set_track_guide(ids["t2"], ids["gB"]))
    path = str(tmp_path / "song" / "曲.gliss")
    _ok(md.save_project(path))
    with open(path, encoding="utf-8") as f:
        tr = {t["id"]: t for t in json.load(f)["session"]["tracks"]}
    assert tr[ids["t1"]]["guide_id"] == ids["gA"] and tr[ids["t2"]]["guide_id"] == ids["gB"]
    assert "guide_id" not in tr[ids["gA"]]                    # 指定の無いトラックはキーを持たない

    md._clear()
    m._state.update(project=None, session=None, track=None, document=None)
    r = _ok(md.load_project(path))
    rows = {t["id"]: t for t in r["session"]["tracks"]}
    assert rows[ids["t1"]]["guide_id"] == ids["gA"] and rows[ids["t2"]]["effective_guide_id"] == ids["gB"]
    assert r["document"]["dirty"] is False
    # 開き直しても、それぞれのガイドで測る
    a = _devs(m, mt, ids["t1"])
    b = _devs(m, mt, ids["t2"])
    assert a and b and a[0] < -250 and b[0] > 250


def test_old_gliss_without_guide_ids_loads_and_is_not_dirty(mcp, tmp_path):
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    _ok(mt.set_guide_track(ids["gA"]))
    path = str(tmp_path / "song" / "古い.gliss")
    _ok(md.save_project(path))
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "guide_id" not in text                              # 指定が無ければ古い版と同じ中身
    md._clear()
    m._state.update(project=None, session=None, track=None, document=None)
    r = _ok(md.load_project(path))
    rows = {t["id"]: t for t in r["session"]["tracks"]}
    assert all(t["guide_id"] is None for t in rows.values())
    assert rows[ids["t1"]]["effective_guide_id"] == ids["gA"]   # 共通のガイドはそのまま
    assert r["document"]["dirty"] is False
    _ok(mt.select_track(ids["t2"]))                             # 選んでも未保存にならない
    assert _ok(md.project_status())["document"]["dirty"] is False


# ================================================================ 裏の準備
def test_prep_signature_and_priority_follow_each_tracks_guide(mcp, tmp_path):
    from vocal_engine import prep
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    s = m._state["session"]
    sig0 = prep.track_sig(s, s.track(ids["t1"]))
    _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    sa = prep.track_sig(s, s.track(ids["t1"]))
    _ok(mt.set_track_guide(ids["t1"], ids["gB"]))
    sb = prep.track_sig(s, s.track(ids["t1"]))
    assert len({sig0, sa, sb}) == 3                             # ガイドが違えば別の組み合わせ
    _ok(mt.set_track_guide(ids["t1"], ids["gA"]))
    _ok(mt.set_track_guide(ids["t2"], ids["gB"]))
    assert s.guide_users() == {ids["gA"]: [ids["t1"]], ids["gB"]: [ids["t2"]]}
    assert prep.track_sig(s, s.track(ids["t1"])) == sa


# ================================================================ ARA
@pytest.fixture
def ara(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    F.set_preferred_estimator("praat")
    yield m, a
    F.set_preferred_estimator(None)
    md._clear()
    a._reset_render()


def _ara_doc(a, tmp_path, key="doc-1"):
    paths = _media(tmp_path)
    _open(a, key)
    out = {}
    for ara_id, k in (("mod-T1", "t1"), ("mod-T2", "t2"), ("mod-GA", "gA"), ("mod-GB", "gB")):
        out[ara_id] = _add(a, ara_id, paths[k], group=k)["track"]["id"]
    return paths, out


def _guides_in_session(m):
    s = m._state["session"]
    return {t["ara_id"]: next(x["ara_id"] for x in s.tracks if x["id"] == t["guide_id"])
            for t in s.tracks if t.get("guide_id")}


def test_ara_archive_carries_guides_and_ara_sync_restores_them(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    paths, tids = _ara_doc(a, tmp_path)
    arc0 = _ok(a.ara_archive())
    assert arc0["guides"] == {} and arc0["guide"] is None

    _ok(mt.select_track(tids["mod-T1"]))
    _ok(mt.set_track_guide(tids["mod-T1"], tids["mod-GA"]))
    _ok(mt.set_track_guide(tids["mod-T2"], tids["mod-GB"]))
    _ok(mt.set_guide_track(tids["mod-GB"]))                 # 共通のガイド（`guide`）も別に持てる
    arc = _ok(a.ara_archive())
    assert arc["guides"] == {"mod-T1": "mod-GA", "mod-T2": "mod-GB"} and arc["guide"] == "mod-GB"
    # 各修飾のアーカイブにはガイドを入れない（ガイドは文書の指定）
    assert arc["archives"]["mod-T1"]["archive"] is None or arc["archives"]["mod-T1"]["archive"].get("guide") is None

    # 別の作業場所（開き直し）: 修飾を足し直してから ara_sync(guides) で戻す。履歴には入らない
    from vocal_engine import mcp_document as md
    md._clear()
    m._state.update(project=None, session=None, track=None, document=None)
    a._reset_render()
    _open(a, "doc-2")
    for ara_id, k in (("mod-T1", "t1"), ("mod-T2", "t2"), ("mod-GA", "gA"), ("mod-GB", "gB")):
        _add(a, ara_id, paths[k], group=k)
    assert _guides_in_session(m) == {}
    r = _ok(a.ara_sync(guide=arc["guide"], guides=arc["guides"]))
    assert r["guides"] == arc["guides"] and r["guides_changed"] and r["guide_changed"]
    assert _guides_in_session(m) == arc["guides"]
    s = m._state["session"]
    assert s.history in (None, []) or not any(e.get("kind") == "session" for e in s.history)
    assert _ok(a.ara_archive())["guides"] == arc["guides"]
    # 同じ指定をもう一度送っても何も変わらない
    assert _ok(a.ara_sync(guides=arc["guides"]))["guides_changed"] == []
    # null / "" で外す。渡さなかった修飾はそのまま
    r = _ok(a.ara_sync(guides={"mod-T1": ""}))
    assert r["guides"] == {"mod-T2": "mod-GB"}
    # 知らない ara_id は unknown に返して当てない
    r = _ok(a.ara_sync(guides={"mod-T1": "mod-NONE", "mod-NONE": "mod-GA"}))
    assert sorted(r["unknown"]) == ["mod-NONE", "mod-NONE"] and r["guides"] == {"mod-T2": "mod-GB"}
    r = _ok(a.ara_sync(guides={"mod-T1": "mod-T1"}))         # 当てられない組は例外にせず rejected に返す
    assert r["rejected"] and r["rejected"][0]["ara_id"] == "mod-T1" and r["guides"] == {"mod-T2": "mod-GB"}
    assert a.ara_sync(guides=["x"])["ok"] is False


def test_each_modification_follows_its_own_guide_in_an_ara_document(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    paths, tids = _ara_doc(a, tmp_path)
    r = _ok(a.ara_sync(guides={"mod-T1": "mod-GA", "mod-T2": "mod-GB"}))
    assert sorted(r["guides_changed"]) == sorted([tids["mod-T1"], tids["mod-T2"]])
    assert r["reopened"] in (True, False)
    a1 = _devs(m, mt, tids["mod-T1"])
    b1 = _devs(m, mt, tids["mod-T2"])
    assert a1 and b1 and a1[0] == pytest.approx(-300, abs=15) and b1[0] == pytest.approx(300, abs=15)
    # アーカイブから復元（ara_restore）しても指定は変わらない・履歴に入らない
    arc = _ok(a.ara_archive())["archives"]["mod-T1"]["archive"]
    if arc is not None:
        _ok(a.ara_restore("mod-T1", arc))
    assert _guides_in_session(m) == {"mod-T1": "mod-GA", "mod-T2": "mod-GB"}


def test_removing_the_guide_modification_drops_the_assignment(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    paths, tids = _ara_doc(a, tmp_path)
    _ok(a.ara_sync(guides={"mod-T1": "mod-GA", "mod-T2": "mod-GA"}))
    _ok(mt.select_track(tids["mod-T2"]))
    _ok(m.analyze_take(background=False))
    _ok(a.ara_remove_modification("mod-GA"))
    assert _guides_in_session(m) == {}
    assert _ok(a.ara_archive())["guides"] == {}
    rows = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert rows[tids["mod-T1"]]["guide_id"] is None and rows[tids["mod-T2"]]["effective_guide_id"] is None
    assert m._state["project"].guide is None               # 編集対象のガイドも外れて開き直した


def test_undo_restores_a_track_guide_in_an_ara_document(ara, tmp_path):
    """DAW が所有するトラックの項目は Gliss の undo で戻さないが、ガイドの指定は Gliss のもの。"""
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    paths, tids = _ara_doc(a, tmp_path)
    _ok(mt.select_track(tids["mod-T1"]))
    _ok(mt.set_track_guide(tids["mod-T1"], tids["mod-GA"]))
    assert _guides_in_session(m) == {"mod-T1": "mod-GA"}
    u = _ok(m.undo())
    assert u["undone"]["label"] == "ガイドの指定"
    assert _guides_in_session(m) == {}
    _ok(m.redo())
    assert _guides_in_session(m) == {"mod-T1": "mod-GA"}
    # ara_sync で戻した指定は履歴に入らない（undo で戻すのは set_track_guide の分だけ）
    _ok(a.ara_sync(guides={"mod-T2": "mod-GB"}))
    _ok(m.undo())
    assert _guides_in_session(m) == {"mod-T2": "mod-GB"}


def test_old_archive_without_guides_still_loads(ara, tmp_path):
    m, a = ara
    paths, tids = _ara_doc(a, tmp_path)
    # 古いプラグインは guides を知らない: guide だけ送る
    r = _ok(a.ara_sync(guide="mod-GA"))
    assert r["guide_changed"] and r["guides"] == {}
    assert m._state["session"].guide == tids["mod-GA"]
    assert _guides_in_session(m) == {}


def test_external_ai_sets_a_track_guide_through_the_relay(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
    from vocal_engine import ara_relay as R
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine import mcp_tracks as mt
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.setenv("GLISS_CLIENT", "ara")
    monkeypatch.setenv(R.SESSIONS_ENV, str(tmp_path / "sessions"))
    monkeypatch.delenv(R.ALLOW_ENV, raising=False)
    F.set_preferred_estimator("praat")
    try:
        paths, tids = _ara_doc(a, tmp_path)
        assert mt.current_track_id() == tids["mod-T1"]
        assert "set_track_guide" in R.SESSION_TOOLS
        docs = R.documents()
        rows = {r["ara_id"]: r for r in docs[0]["tracks"]}
        assert rows["mod-T2"]["guide_id"] is None and rows["mod-T2"]["is_guide"] is False

        R.attach(ara_id="mod-T2")                             # 外部の AI は mod-T2 を選ぶ
        r = R.forward("set_track_guide", {"guide_track_id": tids["mod-GB"]})
        assert r["ok"], r
        assert r["effective_guide_id"] == tids["mod-GB"] and r["track"] == tids["mod-T2"]
        assert _guides_in_session(m) == {"mod-T2": "mod-GB"}
        assert mt.current_track_id() == tids["mod-T1"]         # プラグインの画面の編集対象は変わらない
        assert m._state["project"].guide is None               # 画面の mod-T1 はガイド無しのまま
        assert _ok(a.ara_revs())["external"]["session_seq"] >= 1
        rows = {r["ara_id"]: r for r in R.documents()[0]["tracks"]}
        assert rows["mod-T2"]["guide_id"] == tids["mod-GB"] and rows["mod-T2"]["guide_ara_id"] == "mod-GB"
        assert rows["mod-GB"]["is_guide"] is True
        # 外部の解析・ずれの計算も、その修飾の実効のガイドで
        assert R.forward("analyze_take", {"background": False})["ok"]
        dv = R.forward("list_deviations", {"threshold_cents": 0.0, "threshold_ms": 1000.0})
        assert dv["ok"] and dv["deviations"] and dv["deviations"][0]["pitch_cents"] == pytest.approx(300, abs=15)
        # 外部から undo すると指定が戻る
        assert R.forward("undo", {})["ok"]
        assert _guides_in_session(m) == {}
    finally:
        R.stop()
        R.detach()
        F.set_preferred_estimator(None)
        md._clear()
        a._reset_render()


# ================================================================ 見直しの修正
def test_add_track_as_inst_clears_the_guide_assignments(mcp, tmp_path):
    """add_track(kind="inst") で既存のトラックを伴奏にしても、set_track と同じく指定を外す。"""
    m, mt, md = mcp
    paths, ids = _session(mcp, tmp_path)
    _ok(mt.set_track_guide(ids["t2"], ids["gA"]))
    _ok(mt.set_track_guide(ids["gB"], ids["gA"]))
    _ok(mt.set_guide_track(ids["gB"]))
    _ok(mt.add_track(paths["gA"], kind="inst"))              # もうあるファイル: 種類だけ変わる
    _ok(mt.add_track(paths["gB"], kind="inst"))
    rows = _rows(mt)
    assert rows[ids["gA"]]["kind"] == "inst" and rows[ids["gB"]]["kind"] == "inst"
    assert rows[ids["t2"]]["guide_id"] is None and rows[ids["gB"]]["guide_id"] is None
    assert _ok(mt.list_tracks())["guide"] is None            # 共通のガイドも外れる
    assert all(t["effective_guide_id"] is None for t in rows.values() if t["kind"] == "inst")
    assert not any(t["is_guide"] for t in rows.values())


def test_ara_sync_skips_a_bad_guide_pair_and_applies_the_rest(ara, tmp_path):
    m, a = ara
    from vocal_engine import mcp_tracks as mt
    from vocal_engine.project.session import Session
    paths, tids = _ara_doc(a, tmp_path)
    _ok(mt.set_track(tids["mod-GB"], kind="inst"))
    r = _ok(a.ara_sync(tracks=[{"ara_id": "mod-T1", "offset_sec": 1.5}],
                       guides={"mod-T1": "mod-GB", "mod-T2": "mod-GA", "mod-GA": "mod-GA", "mod-X": "mod-GA"}))
    assert [(x["ara_id"], x["guide"]) for x in r["rejected"]] == [("mod-T1", "mod-GB"), ("mod-GA", "mod-GA")]
    assert all(x["reason"] for x in r["rejected"])
    assert r["unknown"] == ["mod-X"] and r["guides"] == {"mod-T2": "mod-GA"}
    s = m._state["session"]
    assert s.find_ara("mod-T1")["offset_sec"] == 1.5         # ほかの変更は当たる
    saved = Session.load(s.dir)                              # 未保存のまま残らない
    assert saved.find_ara("mod-T1")["offset_sec"] == 1.5 and saved.find_ara("mod-T2")["guide_id"] == tids["mod-GA"]
    # 伴奏にされたトラックの行に実効のガイドは出ない
    _ok(mt.set_guide_track(tids["mod-GA"]))
    rows = {t["id"]: t for t in _ok(mt.list_tracks())["tracks"]}
    assert rows[tids["mod-GB"]]["effective_guide_id"] is None
    assert a._row(s, s.track(tids["mod-GB"]))["effective_guide_id"] is None


def test_daw_undo_of_a_deleted_modification_restores_its_guide_assignments(ara, tmp_path):
    m, a = ara
    paths, tids = _ara_doc(a, tmp_path)
    key = {"mod-T1": "t1", "mod-T2": "t2", "mod-GA": "gA", "mod-GB": "gB"}

    def readd(ara_id):
        _ok(a.ara_set_modification(ara_id, paths[key[ara_id]], source_id="src-" + os.path.basename(paths[key[ara_id]]),
                                   name=ara_id, group=key[ara_id]))

    base = {"mod-T1": "mod-GA", "mod-T2": "mod-GA"}
    _ok(a.ara_sync(guides=dict(base)))
    # 指定を持つ本人を消して戻す
    _ok(a.ara_remove_modification("mod-T1"))
    assert _guides_in_session(m) == {"mod-T2": "mod-GA"}
    readd("mod-T1")
    assert _guides_in_session(m) == base and m._state["session"].find_ara("mod-T1")["id"] == tids["mod-T1"]
    # ガイドにされていた修飾を消して戻す（参照元の指定も戻る）
    _ok(a.ara_remove_modification("mod-GA"))
    assert _guides_in_session(m) == {}
    readd("mod-GA")
    assert _guides_in_session(m) == base
    # 両方を消して、どちらの順に戻しても戻る
    for order in (("mod-T1", "mod-GA"), ("mod-GA", "mod-T1")):
        _ok(a.ara_remove_modification("mod-T1"))
        _ok(a.ara_remove_modification("mod-GA"))
        assert _guides_in_session(m) == {}
        for ara_id in order:
            readd(ara_id)
        assert _guides_in_session(m) == base, order
    assert not m._state["session"].ara_guide_wait
    # 取り消している間に別の指定を入れたトラックは上書きしない
    _ok(a.ara_remove_modification("mod-T2"))
    _ok(a.ara_sync(guides={"mod-T1": "mod-GB"}))
    _ok(a.ara_remove_modification("mod-GA"))
    readd("mod-GA")
    readd("mod-T2")
    assert _guides_in_session(m) == {"mod-T1": "mod-GB", "mod-T2": "mod-GA"}
    # session.json に残る（エンジンを開き直しても預けた指定が消えない）
    _ok(a.ara_remove_modification("mod-T1"))
    _ok(a.ara_remove_modification("mod-GB"))
    readd("mod-T1")                                          # ガイドの修飾はまだ無い: 預ける
    from vocal_engine.project.session import Session
    assert Session.load(m._state["session"].dir).ara_guide_wait == {tids["mod-T1"]: tids["mod-GB"]}
    readd("mod-GB")
    assert _guides_in_session(m)["mod-T1"] == "mod-GB"
