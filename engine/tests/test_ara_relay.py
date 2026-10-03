# -*- coding: utf-8 -*-
"""外部の AI から DAW の Gliss（ARA プラグイン）の文書を操作する中継（`vocal_engine/ara_relay.py`。engine/docs/MCP.md §3-5）。

- プラグインのエンジン（`GLISS_CLIENT=ara`）が `ara_open` で 127.0.0.1 の口を開き、記録（接続先・トークン・DAW・文書の鍵）を書く
- 外部の一覧 → 選ぶ → 編集が、選んだ修飾に効き、プラグインの画面の編集対象を変えない。`ara_revs` の版と外部の変更の番号が進む
- 許可（`GLISS_ARA_AI`）・禁止のツール・トークン無し／違いの拒否・死んだ pid の記録の無視・閉じたら記録を消す
- 画面の呼び出しと外部の呼び出しが同時に来ても、それぞれのトラックに当たる
- stdio 越し: プラグインのエンジンと外部のエンジン（どちらも `python -m vocal_engine.mcp`）の 2 本
"""
import json
import os
import socket
import threading

import numpy as np
import pytest

from conftest import VENV_PY
from test_ara_tools import Cache, _add, _ok, _open, _voice, _wav


@pytest.fixture
def relay(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m     # 先に読む（mcp_ara などは mcp_server の末尾から読まれる）
    from vocal_engine import ara_relay as R
    from vocal_engine import mcp_ara as a
    from vocal_engine import mcp_document as md
    from vocal_engine.analysis import f0 as F
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    monkeypatch.setenv("GLISS_CLIENT", "ara")
    monkeypatch.setenv(R.SESSIONS_ENV, str(tmp_path / "sessions"))
    monkeypatch.delenv(R.ALLOW_ENV, raising=False)
    F.set_preferred_estimator("praat")
    yield m, a, R
    R.stop()
    R.detach()
    F.set_preferred_estimator(None)
    md._clear()
    a._reset_render()


def _doc_with_two(a, tmp_path):
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    _open(a)
    r1 = _add(a, "mod-A", src, offset_sec=0.0, group="Vox 1", name="テイク A")
    r2 = _add(a, "mod-B", src, offset_sec=8.0, group="Vox 2", name="テイク B")
    return src, r1["track"]["id"], r2["track"]["id"]


def _records(R):
    d = R.sessions_dir()
    return [n for n in os.listdir(d) if n.endswith(".json")] if os.path.isdir(d) else []


def _raw(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8")) if buf else None


# ================================================================ プラグインのエンジンの口と記録
def test_ara_open_writes_the_record_and_stop_removes_it(relay, tmp_path):
    m, a, R = relay
    _doc_with_two(a, tmp_path)
    names = _records(R)
    assert names == ["%d.json" % os.getpid()]
    with open(os.path.join(R.sessions_dir(), names[0]), encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["format"] == "gliss-ara-relay" and rec["host"] == "127.0.0.1" and rec["port"] > 0
    assert len(rec["token"]) == 64 and rec["work_key"] == "doc-1" and rec["document"] == "曲"
    assert rec["pid"] == os.getpid() and rec["daw_pid"] == os.getppid()
    assert rec["allow"] == {"relay": True, "edit": True, "save": False}
    # 口は 127.0.0.1 だけ（ほかのアドレスに bind していない）
    assert R._srv_state["server"].server_address[0] == "127.0.0.1"
    assert _ok(a.ara_revs())["external"] == {"seq": 0, "session_seq": 0, "track_id": None}
    R.stop()
    assert _records(R) == [] and _ok(a.ara_revs())["external"] is None


def test_off_does_not_open_and_app_engine_does_not_open(relay, tmp_path, monkeypatch):
    m, a, R = relay
    monkeypatch.setenv(R.ALLOW_ENV, "off")
    _doc_with_two(a, tmp_path)
    assert _records(R) == [] and R.external() is None
    monkeypatch.setenv(R.ALLOW_ENV, "edit")
    monkeypatch.setenv("GLISS_CLIENT", "app")          # 単体の画面のエンジンは口を開かない
    assert R.start("doc-1", "曲", str(tmp_path)) is None and _records(R) == []


def test_token_is_required(relay, tmp_path):
    m, a, R = relay
    _doc_with_two(a, tmp_path)
    port = R._srv_state["port"]
    r = _raw(port, b'{"op": "list"}\n')
    assert r["ok"] is False and r["permission"] == "token"
    r = _raw(port, json.dumps({"op": "call", "tool": "shift_pitch", "token": "0" * 64,
                               "args": {"cents": 100, "start_sec": 0, "end_sec": 5}}).encode() + b"\n")
    assert r["ok"] is False and r["permission"] == "token"
    assert _raw(port, b"not json\n")["ok"] is False
    assert _ok(a.ara_revs())["external"]["seq"] == 0


def test_dead_pid_records_are_ignored_and_removed(relay, tmp_path):
    m, a, R = relay
    os.makedirs(R.sessions_dir(), exist_ok=True)
    stale = os.path.join(R.sessions_dir(), "999999.json")
    with open(stale, "w", encoding="utf-8") as f:
        json.dump({"format": "gliss-ara-relay", "version": 1, "pid": 999999, "port": 1, "token": "x",
                   "work_key": "gone", "document": "閉じた曲"}, f)
    assert R.documents() == [] and not os.path.exists(stale)
    with pytest.raises(RuntimeError, match="開いている Gliss の文書が無い"):
        R.attach()


# ================================================================ 一覧 → 選ぶ → 編集
def test_list_attach_and_edit_the_selected_modification(relay, tmp_path):
    m, a, R = relay
    from vocal_engine import mcp_tracks as mt
    src, ta, tb = _doc_with_two(a, tmp_path)
    assert mt.current_track_id() == ta                   # プラグインの画面は mod-A を開いている
    docs = R.documents()
    assert len(docs) == 1
    d = docs[0]
    assert d["document"] == "曲" and d["work_key"] == "doc-1" and d["engine_pid"] == os.getpid()
    rows = {r["ara_id"]: r for r in d["tracks"]}
    assert rows["mod-B"]["name"] == "テイク B" and rows["mod-B"]["daw_track"] == "Vox 2"
    assert rows["mod-B"]["duration_sec"] == pytest.approx(6.2, abs=1e-3)
    assert rows["mod-A"]["editing_in_plugin"] and not rows["mod-B"]["editing_in_plugin"]
    assert rows["mod-B"]["analyzed"] is False

    with pytest.raises(RuntimeError, match="その修飾が無い"):
        R.attach(ara_id="mod-X")
    d2, row = R.attach(ara_id="mod-B")
    assert row["track_id"] == tb and R.selection()["ara_id"] == "mod-B"

    rev0 = _ok(a.ara_revs())["revs"]["mod-B"]
    r = R.forward("analyze_take", {"background": False})
    assert r["ok"] and r["ara"]["ara_id"] == "mod-B"
    notes = R.forward("list_notes", {"kind": "note"})["notes"]
    assert len(notes) == 5
    r = R.forward("shift_pitch", {"cents": 100, "note_id": notes[1]["id"]})
    assert r["ok"], r
    # プラグインの画面の編集対象は mod-A のまま。編集は mod-B に入り、作者は AI
    assert mt.current_track_id() == ta and os.path.normcase(m._state["project"].dir) == os.path.normcase(
        m._state["session"].project_dir_of(m._state["session"].track(ta)))
    s = m._state["session"]
    pb, _ = mt._track_project(s, s.track(tb))
    pb.reload_if_changed()
    assert len(pb.edits) == 1 and pb.changesets[-1].author == "ai"
    pa, _ = mt._track_project(s, s.track(ta))
    assert len(pa.edits) == 0
    revs = _ok(a.ara_revs())
    assert revs["revs"]["mod-B"] != rev0
    assert revs["external"]["seq"] >= 2 and revs["external"]["track_id"] == tb
    # 差分の再合成（プラグインが 1 秒ごとの ara_revs で拾って呼ぶもの）に当たっている
    cache = Cache(src)
    cache.sync(a, "mod-B")
    assert not np.array_equal(cache.buf, cache.orig)
    # 保存用の写し（DAW のソングに入る）にも入っている
    assert len(_ok(a.ara_archive())["archives"]["mod-B"]["archive"]["changesets"]) == 1

    # 読むだけのツールは外部の変更の番号を進めない
    seq = revs["external"]["seq"]
    R.forward("list_deviations", {})
    R.forward("get_pitch", {"start_sec": 0.5, "end_sec": 1.0})
    assert _ok(a.ara_revs())["external"]["seq"] == seq
    # undo も選んだ修飾の側で（曲の履歴の最後 = 外部の shift_pitch）。セッションの番号も進む
    r = R.forward("undo", {})
    assert r["ok"], r
    pb.reload_if_changed()
    assert len(pb.edits) == 0
    ext = _ok(a.ara_revs())["external"]
    assert ext["seq"] == seq + 1 and ext["session_seq"] == 1
    assert mt.current_track_id() == ta

    # 外部の select_track は外部の選択だけを変える
    r = R.forward("select_track", {"track_id": ta})
    assert r["ok"] and R.selection()["ara_id"] == "mod-A" and mt.current_track_id() == ta
    assert R.forward("list_tracks", {})["ok"]
    assert R.detach()["ara_id"] == "mod-A" and R.selection() is None


def test_attach_picks_the_only_or_the_one_open_in_the_plugin(relay, tmp_path):
    m, a, R = relay
    src, ta, tb = _doc_with_two(a, tmp_path)
    d, row = R.attach()
    assert row["track_id"] == ta                         # プラグインの画面で開いているもの
    assert R.attach(document="doc-1", ara_id="mod-B")[1]["track_id"] == tb
    with pytest.raises(RuntimeError, match="その文書が無い"):
        R.attach(document="other")


def test_permissions_and_forbidden_tools(relay, tmp_path, monkeypatch):
    m, a, R = relay
    src, ta, tb = _doc_with_two(a, tmp_path)
    R.attach(ara_id="mod-B")
    for tool, args in (("load_project", {}), ("add_track", {"path": src}), ("save_project", {"path": "x.gliss"}),
                       ("remove_track", {"track_id": tb}), ("ara_revs", {}), ("ara_restore", {"ara_id": "mod-B",
                                                                                              "archive": {}}),
                       ("export_wav", {"path": str(tmp_path / "o.wav")})):
        r = R.forward(tool, args)
        assert r["ok"] is False and r["permission"] == "ara", (tool, r)
    assert len(m._state["session"].tracks) == 2
    # 保存・書き出し（ユーザーのファイルに書く）は既定で断る
    out = tmp_path / "region.wav"
    r = R.forward("render_region", {"path": str(out), "start_sec": 0.5, "end_sec": 1.0})
    assert r["ok"] is False and r["permission"] == "save" and not out.exists()
    # 読むだけ（read）: 編集を断る。外部の変更の番号は進まない
    monkeypatch.setenv(R.ALLOW_ENV, "read")
    _ok(R.forward("analyze_take", {"background": False}))
    seq = _ok(a.ara_revs())["external"]["seq"]
    r = R.forward("shift_pitch", {"cents": 50, "start_sec": 0.5, "end_sec": 2.0})
    assert r["ok"] is False and r["permission"] == "edit" and "GLISS_ARA_AI" in r["error"]
    assert _ok(a.ara_revs())["external"]["seq"] == seq
    # save: 書き出しも許す
    monkeypatch.setenv(R.ALLOW_ENV, "save")
    r = R.forward("render_region", {"path": str(out), "start_sec": 0.5, "end_sec": 1.0})
    assert r["ok"], r
    assert out.exists()
    # 引数の誤り・知らないツール
    assert R.forward("shift_pitch", {"nope": 1})["ok"] is False
    assert R.forward("no_such_tool", {})["ok"] is False


def test_screen_and_external_calls_at_the_same_time(relay, tmp_path):
    """プラグインの画面（このスレッド）と外部（中継のスレッド）が同時に編集しても、それぞれのトラックに当たる。"""
    m, a, R = relay
    from vocal_engine import mcp_tracks as mt
    src, ta, tb = _doc_with_two(a, tmp_path)
    _ok(m.analyze_take(background=False))
    notes_a = [n["id"] for n in _ok(m.list_notes(kind="note"))["notes"]]
    R.attach(ara_id="mod-B")
    _ok(R.forward("analyze_take", {"background": False}))
    notes_b = [n["id"] for n in R.forward("list_notes", {"kind": "note"})["notes"]]
    errors = []

    def external():
        try:
            for i in range(6):
                r = R.forward("shift_pitch", {"cents": 10, "note_id": notes_b[i % len(notes_b)]})
                if not r.get("ok"):
                    errors.append(r)
        except Exception as e:                           # noqa: BLE001
            errors.append(e)

    th = threading.Thread(target=external)
    th.start()
    for i in range(6):
        _ok(m.shift_pitch(-10, note_id=notes_a[i % len(notes_a)], author="human"))
        assert mt.current_track_id() == ta
        _ok(m.list_notes(kind="note"))
    th.join(120)
    assert not th.is_alive() and not errors, errors
    s = m._state["session"]
    pa, _ = mt._track_project(s, s.track(ta))
    pb, _ = mt._track_project(s, s.track(tb))
    pa.reload_if_changed()
    pb.reload_if_changed()
    assert len(pa.edits) == 6 and all(c.author == "human" for c in pa.changesets)
    assert len(pb.edits) == 6 and all(c.author == "ai" for c in pb.changesets)
    assert mt.current_track_id() == ta


def test_forward_reports_a_closed_document(relay, tmp_path):
    m, a, R = relay
    _doc_with_two(a, tmp_path)
    R.attach(ara_id="mod-B")
    R.stop()                                             # DAW が文書を閉じた（エンジンが終わった）
    r = R.forward("list_notes", {})
    assert r["ok"] is False and "ara_documents" in r["error"] and r["ara"]["ara_id"] == "mod-B"


def test_external_engine_forwards_through_the_tool_wrapper(relay, tmp_path, monkeypatch):
    """外部の AI のエンジン（GLISS_CLIENT なし）: ara_attach の後は普段のツールが転送される。ara_detach で戻る。"""
    m, a, R = relay
    src, ta, tb = _doc_with_two(a, tmp_path)
    # 同じプロセスで外部のエンジンとして呼ぶ（中継のスレッドの中はプラグインのエンジンとして動かしたいので、
    # 呼ぶ側のスレッドだけ外部に見せる）
    from vocal_engine import bridge
    real = bridge.client
    me = threading.get_ident()
    monkeypatch.setattr(bridge, "client", lambda: "" if threading.get_ident() == me else real())
    assert not bridge.is_app()
    r = _ok(a.ara_documents())
    assert r["documents"][0]["work_key"] == "doc-1" and r["attached"] is None
    r = _ok(a.ara_attach(ara_id="mod-B"))
    assert r["attached"]["ara_id"] == "mod-B" and r["track"]["track_id"] == tb
    r = m.analyze_take(background=False)
    assert r["ok"] and r["ara"]["ara_id"] == "mod-B"
    notes = m.list_notes(kind="note")["notes"]
    r = m.shift_pitch(80, note_id=notes[0]["id"])
    assert r["ok"] and r["ara"]["ara_id"] == "mod-B"
    from vocal_engine import mcp_document as md
    assert md.load_project()["permission"] == "ara"
    monkeypatch.setattr(bridge, "client", real)
    assert _ok(a.ara_revs())["external"]["seq"] >= 2
    monkeypatch.setattr(bridge, "client", lambda: "" if threading.get_ident() == me else real())
    _ok(a.ara_detach())
    assert R.selection() is None
    monkeypatch.setattr(bridge, "client", real)
    assert a.ara_attach()["ok"] is False                 # プラグインのエンジンでは使えない


# ================================================================ stdio（プラグインのエンジンと外部のエンジンの 2 本）
@pytest.mark.skipif(not os.path.exists(VENV_PY), reason=".venv の python が無い")
async def test_two_engines_over_stdio(tmp_path):
    from mcp import StdioServerParameters
    from mcp.client import Client
    from test_mcp_stdio import payload
    src = _wav(tmp_path / "src" / "a.wav", _voice())
    sessions = str(tmp_path / "sessions")
    base = {k: v for k, v in os.environ.items() if k not in ("GLISS_CLIENT", "GLISS_ARA_AI")}
    base.update(PYTHONIOENCODING="utf-8", GLISS_F0_ESTIMATOR="praat", VOCAL_ENGINE_AUTO_LYRICS="0",
                VOCAL_ENGINE_PREP="0", GLISS_ARA_SESSIONS_DIR=sessions)
    engine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plug = StdioServerParameters(command=VENV_PY, args=["-m", "vocal_engine.mcp"], cwd=engine,
                                 env=dict(base, GLISS_CLIENT="ara", VOCAL_ENGINE_WORK_DIR=str(tmp_path / "work")))
    ext = StdioServerParameters(command=VENV_PY, args=["-m", "vocal_engine.mcp"], cwd=engine,
                                env=dict(base, VOCAL_ENGINE_WORK_DIR=str(tmp_path / "ext-work"),
                                         GLISS_BRIDGE=str(tmp_path / "bridge.json")))

    async def call(c, name, args=None):
        r = payload(await c.call_tool(name, args or {}))
        assert r.get("ok") is not False, (name, r)
        return r

    async with Client(plug, read_timeout_seconds=120) as p:
        await call(p, "ara_open", {"work_key": "stdio-doc", "name": "ソング"})
        await call(p, "ara_set_modification", {"ara_id": "mod-A", "source_path": src, "source_id": "s1",
                                               "name": "Vox", "group": "Lead"})
        rev0 = (await call(p, "ara_revs"))["revs"]["mod-A"]
        assert len(os.listdir(sessions)) == 1
        async with Client(ext, read_timeout_seconds=120) as e:
            docs = (await call(e, "ara_documents"))["documents"]
            assert [d["document"] for d in docs] == ["ソング"]
            assert docs[0]["tracks"][0]["ara_id"] == "mod-A" and docs[0]["tracks"][0]["daw_track"] == "Lead"
            await call(e, "ara_attach", {"ara_id": "mod-A"})
            await call(e, "analyze_take", {"background": False})
            notes = (await call(e, "list_notes", {"kind": "note"}))["notes"]
            r = await call(e, "shift_pitch", {"cents": 150, "note_id": notes[2]["id"]})
            assert r["ara"]["ara_id"] == "mod-A"
            revs = await call(p, "ara_revs")
            assert revs["revs"]["mod-A"] != rev0 and revs["external"]["seq"] >= 2
            cache = Cache(src)
            while True:
                r = await call(p, "ara_render_dirty", {"ara_id": "mod-A", "since": cache.rev})
                cache.apply(r)
                if not r["more"]:
                    break
            assert not np.array_equal(cache.buf, cache.orig)
            # 単体の Gliss の曲（bridge.json）に戻る
            await call(e, "ara_detach")
            r = payload(await e.call_tool("list_notes", {}))
            assert r["ok"] is False and "ara" not in r
    # プラグインのエンジンが終わったら記録は消える
    assert os.listdir(sessions) == []
