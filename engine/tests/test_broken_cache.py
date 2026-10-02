# -*- coding: utf-8 -*-
"""壊れた準備済みキャッシュの検知と再準備に関するテスト（#63）。"""
import json
import os

import pytest

from test_prep import FREQ, _job, _ok, _open, _ready, _until, env  # noqa: F401


def _corrupt_same_stat(path):
    """同じサイズ・同じ更新時刻のまま中身を壊す（ハッシュの使い回しでは見えないケース）。"""
    st = os.stat(path)
    with open(path, "r+b") as f:
        f.write(b"!")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (st.st_size, st.st_mtime_ns, st.st_ino)


@pytest.mark.parametrize("entry", ["analyze_take", "select_track", "export_view_data"])
def test_broken_prepared_cache_is_reprepared(env, entry):
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.project import store

    r = _open(m, paths, sdir, guide="g1")
    t1 = r["session"]["current"]
    _ok(m.analyze_take())
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    s = m._state["session"]
    t = s.track(tb)
    pdir = s.project_dir_of(t)
    sig = prep.track_sig(s, t)
    assert prep.stamp_ok(pdir, sig)                # 内容ハッシュを使い回しに載せる
    take_cache = os.path.join(pdir, "cache", "take-analysis.json")
    _corrupt_same_stat(take_cache)
    if not prep.stamp_ok(pdir, sig) and os.name != "nt":
        # 使い回しの照合には st_ctime_ns も入る（prep.py）。Windows では作成時刻なので書き込みで変わらないが、
        # Linux などでは書き込みで変わり、使い回しが外れて壊れたことがその場で見つかる（このテストの前提を作れない）
        pytest.skip("書き込みで st_ctime が変わる OS では、壊れたのに準備済みに見える状態を作れない")
    assert prep.stamp_ok(pdir, sig)                # 使い回しのせいで、壊れたのに準備済みに見える状態を作る
    store.forget_open()                            # メモリの Project を捨て、ディスクから読む状態にする
    n = fake.f0_count("b")

    r = _ok(mt.select_track(tb))
    if entry == "analyze_take":                    # 画面: 選んで analyze_take(background)
        a = _ok(m.analyze_take(background=True))
        if a.get("status") == "running":
            a = _ok(_job(m, a["job_id"]))
            assert a.get("status") == "done", a
    elif entry == "select_track":                  # Claude Code: 選んで同期の analyze_take
        a = _ok(m.analyze_take())
    else:                                          # 選んだだけで描画データ（analyze_take を呼ばない）
        a = None
    v = _ok(m.export_view_data())
    with open(v["path"], encoding="utf-8") as f:
        vd = json.load(f)
    assert vd["notes"], "描画データにノートが無い"
    assert fake.f0_count("b") == n + 1             # 準備し直した（テイクの F0 を取り直した）
    with open(take_cache, encoding="utf-8") as f:
        json.load(f)                               # 読めるキャッシュに戻った
    assert prep.stamp_ok(pdir, sig)
    _until(_ready(prep, tb))
    p = m._state["project"]
    assert abs(float(p.take_f0.f0[p.take_f0.voiced].mean()) - FREQ["b"]) < 5
    if a is not None:
        assert a["notes"]["total"] > 0
