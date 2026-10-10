# -*- coding: utf-8 -*-
"""歌詞の直し・曲を開くときに音素の解析を待たせないことの、速さの回帰の試験（2026-10-10 承認）。

曲全体の素材（SONG。158 秒・歌詞 4 区間）で、エンジンの MCP のツールを画面と同じ順に呼んで測る。
- 歌詞を直す: set_lyrics → analyze_take（キャッシュを読むだけ）→ export_view_data が返るまで（編集の応答）と、
  直した区間を裏で解析し終えるまで（操作の間を待つ `PHONEME_IDLE_SEC` は 0 にして、計算だけを見る）
- 音素をまだ解析していない曲を開く: open_project → analyze_take → export_view_data が返るまでと、裏で解析し終えるまで

上限は直した後の実測の中央値の 1.5 倍（3 回ずつを 3 度。ほかの作業で CPU が 40% ほど使われている開発機で測った）。
直す前（テイク全体を表で合わせ直す）は、同じ手順で歌詞の直し 0.38〜0.47 秒・開くまで 0.46 秒なので、戻れば落ちる。
素材か重みが無ければ skip。
"""
import os
import shutil
import statistics
import time

import pytest

import materials as M
from conftest import needs_model

pytestmark = [
    needs_model,
    pytest.mark.skipif(not M.available("SONG"),
                       reason="テスト素材 SONG（曲全体 158 秒）が無い（GLISS_TEST_MATERIALS）"),
]

# 秒。直した後の実測の中央値の 1.5 倍
LIMIT_EDIT = 0.23       # 0.155 × 1.5
LIMIT_EDIT_BG = 0.35    # 0.231 × 1.5
LIMIT_OPEN = 0.21       # 0.140 × 1.5
LIMIT_OPEN_BG = 0.95    # 0.630 × 1.5


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    from vocal_engine import mcp_server as m
    from vocal_engine import prep
    from vocal_engine.phoneme.lyrics import normalize
    tmp = tmp_path_factory.mktemp("speed")
    mp_ = pytest.MonkeyPatch()
    mp_.setenv("VOCAL_ENGINE_PREP", "1")
    mp_.setenv("VOCAL_ENGINE_AUTO_LYRICS", "0")
    mp_.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp / "work"))
    mp_.setattr(prep, "PHONEME_IDLE_SEC", 0.0)
    sdir = str(tmp / "song")
    entries = normalize(M.data("SONG.lyrics"))
    _ok(m.open_project(M.clip("SONG"), project_dir=sdir))
    _ok(m.analyze_take(background=False))
    _ok(m.set_lyrics(entries=entries))
    _ok(m.analyze_phonemes(wait=True))           # 4 区間をそろえておく（HubertFA も読み込む）
    yield m, prep, sdir, entries
    prep.reset()
    m._state.update(project=None, session=None, track=None)
    m._invalidate_renderer()
    mp_.undo()


def _ok(r):
    assert r.get("ok") is not False, r.get("error")
    return r


def _wait_phonemes(m, timeout=60.0):
    """裏の解析が済み、描画データに未解析の区間が無くなるまで（画面と同じく描画データを取り直す）。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        m.export_view_data()
        st = m.phoneme_status()
        if not st.get("pending_entries") and st.get("state") not in ("queued", "preparing"):
            return
        time.sleep(0.02)
    raise AssertionError("裏の音素の解析が終わらない")


def _report(name, xs, limit):
    med = statistics.median(xs)
    print("%s: %s（中央値 %.3f 秒・上限 %.3f 秒）" % (name, ["%.3f" % x for x in xs], med, limit))
    return med


def test_lyrics_edit_does_not_wait_for_phonemes(song):
    m, prep, sdir, entries = song
    e = entries[1]
    resp, bg = [], []
    for k in range(3):
        text = e["text"][:k + 1] + e["text"][k + 2:]     # 毎回ちがう歌詞（前の結果を使い回せない）
        t0 = time.perf_counter()
        r = _ok(m.set_lyrics(text, start_sec=e["start_sec"], end_sec=e["end_sec"]))
        _ok(m.analyze_take(background=True))
        _ok(m.export_view_data())
        resp.append(time.perf_counter() - t0)
        assert r["phonemes_state"] == "analyzing" and r["pending_entries"] == [1]
        _wait_phonemes(m)
        bg.append(time.perf_counter() - t0)
    assert _report("歌詞の直しの応答", resp, LIMIT_EDIT) <= LIMIT_EDIT
    assert _report("直した区間の裏の解析", bg, LIMIT_EDIT_BG) <= LIMIT_EDIT_BG


def test_open_does_not_wait_for_phonemes(song):
    """歌詞はあるが音素をまだ解析していない曲を開く（音素のキャッシュを消して開き直す）。"""
    m, prep, sdir, entries = song
    opened, bg = [], []
    for _ in range(3):
        prep.reset()
        m._state.update(project=None, session=None, track=None)
        m._invalidate_renderer()
        for root, dirs, _files in os.walk(sdir):
            if os.path.basename(root) == "phonemes":
                shutil.rmtree(root)
                dirs[:] = []
        for root, _dirs, files in os.walk(sdir):
            for f in files:
                if f.endswith("-phonemes.json"):
                    os.remove(os.path.join(root, f))
        t0 = time.perf_counter()
        _ok(m.open_project(M.clip("SONG"), project_dir=sdir))
        _ok(m.analyze_take(background=False))
        _ok(m.export_view_data())
        opened.append(time.perf_counter() - t0)
        assert m.phoneme_status()["pending_entries"]             # 開いたときは未解析（裏で解析する）
        _wait_phonemes(m)
        bg.append(time.perf_counter() - t0)
    assert _report("開くまで", opened, LIMIT_OPEN) <= LIMIT_OPEN
    assert _report("開いた後の裏の解析", bg, LIMIT_OPEN_BG) <= LIMIT_OPEN_BG
