# -*- coding: utf-8 -*-
"""中継（外部の AI → プラグインのエンジン）で呼んだジョブが、選んだ修飾の音で再合成する。

中継はツールを呼ぶ間だけ編集対象を選んだ修飾に切り替え、すぐプラグインの画面の編集対象に戻す。ジョブはその後で
走るので、`measure_against_guide(render=True)` のジョブが `_state` の再合成器（画面の修飾のもの・前のジョブが
置いていったもの）を使い、別の修飾の音を測っていた（`after` が null・数百セント）。同期（background=false）は正しかった。

音は合成（`test_ara_tools._voice`）。テイク 2 本は音高が違い（0・+5 半音）、それぞれのガイドはテイクより 1 半音上。
"""
import time

import pytest

from test_ara_tools import _add, _ok, _open, _voice, _wav


@pytest.fixture
def relay(tmp_path, monkeypatch):
    from vocal_engine import mcp_server as m
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
    R._srv_state.update(seq=0, session_seq=0, track_id=None)   # 外部の変更の番号は口を閉じても戻らない（後の試験は 0 から数える）
    F.set_preferred_estimator(None)
    md._clear()
    a._reset_render()
    m._invalidate_renderer()


def _doc(a, mt, tmp_path):
    d = tmp_path / "media"
    media = {"mod-T1": _voice(transpose=0, seed=1), "mod-T2": _voice(transpose=5, seed=2),
             "mod-G1": _voice(transpose=1, seed=3), "mod-G2": _voice(transpose=6, seed=4)}
    _open(a)
    tids = {k: _add(a, k, _wav(d / (k + ".wav"), x), group=k)["track"]["id"] for k, x in media.items()}
    _ok(mt.set_track_guide(tids["mod-T1"], tids["mod-G1"]))
    _ok(mt.set_track_guide(tids["mod-T2"], tids["mod-G2"]))
    return tids


def _forward_job(R, tool, args):
    r = R.forward(tool, args)
    assert r.get("job_id"), r                    # ジョブで返る
    t0 = time.time()
    jid = r["job_id"]
    while r.get("status") == "running" and time.time() - t0 < 120:
        time.sleep(0.05)
        r = R.forward("get_job", {"job_id": jid})
    assert r["status"] == "done", r
    return r                                     # get_job は結果を返り値に広げて返す


def _after(res):
    return res["summary"]["pitch_cents"]["after"]


def test_measure_job_through_relay_renders_the_selected_modification(relay, tmp_path):
    m, a, R = relay
    from vocal_engine import mcp_tracks as mt
    tids = _doc(a, mt, tmp_path)
    assert mt.current_track_id() == tids["mod-T1"]       # プラグインの画面は mod-T1 を開いている
    results = {}
    for ara_id in ("mod-T1", "mod-T2"):
        R.attach(ara_id=ara_id)
        assert R.forward("analyze_take", {"background": False})["ok"]
        assert R.forward("correct_to_guide", {"pitch_strength": 1.0})["ok"]
        job = _forward_job(R, "measure_against_guide", {"render": True, "background": True})
        sync = R.forward("measure_against_guide", {"render": True, "background": False})
        assert sync["ok"], sync
        results[ara_id] = (job, sync)
    for ara_id, (job, sync) in results.items():
        assert _after(sync)["n"] > 0 and _after(sync)["abs_median"] < 50, (ara_id, _after(sync))
        # ジョブでも同期と同じ音を測る（直す前は mod-T2 のジョブが mod-T1 の音を測り、約 500 セント外れた）
        assert _after(job)["n"] == _after(sync)["n"], (ara_id, _after(job), _after(sync))
        assert _after(job)["abs_median"] == pytest.approx(_after(sync)["abs_median"], abs=1.0), ara_id
    # 画面の編集対象と、その再合成器の置き場はジョブに変えられていない
    assert mt.current_track_id() == tids["mod-T1"]
    p = m._state["project"]
    for rr in m._state["region"].values():
        assert rr.f0r is p.take_f0


def test_preview_job_through_relay_renders_the_selected_modification(relay, tmp_path):
    """render_preview のジョブも、選んだ修飾の音（同期と同じサンプル）。"""
    import soundfile as sf
    m, a, R = relay
    from vocal_engine import mcp_tracks as mt
    _doc(a, mt, tmp_path)
    R.attach(ara_id="mod-T2")
    assert R.forward("analyze_take", {"background": False})["ok"]
    assert R.forward("correct_to_guide", {"pitch_strength": 1.0})["ok"]
    job = _forward_job(R, "render_preview", {"background": True})
    sync = R.forward("render_preview", {"background": False})
    assert sync["ok"], sync
    assert (sf.read(job["path"])[0] == sf.read(sync["path"])[0]).all()
