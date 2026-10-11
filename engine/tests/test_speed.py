# -*- coding: utf-8 -*-
"""開く・F0 の方式の往復の速さの回帰（2026-10-10 承認）。

素材 C（テイク）と C2（ガイド）を、新しいエンジンのプロセスで測る（F0 のモデルの読み込みを含む）。

- 開く（初回）: 空の作業場所で open_project → analyze_take（テイクとガイドの F0・音符・対応付け・発音の頭）
- 開く（2 回目）: 同じ作業場所を別のプロセスで開き直す（キャッシュを読むだけ）
- 方式の往復: RMVPE → Gliss → RMVPE。戻すときは方式ごとの F0 の保存（`cache/f0-<方式>.json`）と
  方式ごとのガイドの解析・対応付けから戻す
- 1 ノートのタイミングの割合（2026-10-11 承認）: 合成の 3 分の曲（36 フレーズ × 6 ノート）を曲全体でガイドに
  合わせた後、1 ノートの割合を変える計画（plan_edit）と確定（apply_plan）。素材は使わない（解析は測らない）

上限は、開発機（16 コア）で 3 回測った中央値（MEDIAN_SEC）の 1.5 倍。0.1 秒に満たない処理はディスクの揺れで倍を超えるので、
上限は中央値 + FLOOR_SEC を下回らせない。遅い PC では GLISS_SPEED_SCALE（倍率）で緩める。
GLISS_SPEED=0 で skip。素材（割合のテストは要らない）・RMVPE の重みが無ければ skip（CI は回さない）。
歌詞の自動推定は切って測る（聞き取りの重みに左右されない）。
"""
import json
import os
import subprocess
import sys
import time

import numpy as np
import pytest
import soundfile as sf

if __name__ == "__main__":      # 測る側のプロセス: この worktree の vocal_engine を読む（conftest が読む前に）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import GUIDE, TAKE, needs_material, needs_model

# 開発機で 3 回測った中央値（秒）。上限はこの 1.5 倍
MEDIAN_SEC = {"open_cold": 2.42, "open_warm": 0.08, "to_gliss": 1.83, "back_rmvpe": 0.02,
              "ratio_plan": 0.16, "ratio_commit": 0.06}
FLOOR_SEC = 0.25
SCALE = float(os.environ.get("GLISS_SPEED_SCALE") or 1.0)

pytestmark = [needs_model, pytest.mark.skipif(os.environ.get("GLISS_SPEED") == "0", reason="GLISS_SPEED=0")]
needs_c = needs_material("C", "C2")


def _child(phase, pdir):
    """新しいプロセスの中: 測った秒を JSON で 1 行出す。"""
    t0 = time.perf_counter()
    from vocal_engine import mcp_server as m
    from vocal_engine.project.store import recorded_estimator
    out = {}

    def ok(r):
        assert r.get("ok") is not False, r.get("error")
        return r

    ok(m.open_project(TAKE, guide_path=GUIDE, project_dir=pdir))
    ok(m.analyze_take(background=False))
    out["open"] = time.perf_counter() - t0
    out["estimator"] = recorded_estimator(m._state["project"].analysis)
    if phase == "warm":
        t = time.perf_counter()
        ok(m.analyze_take(estimator="gliss", background=False))
        out["to_gliss"] = time.perf_counter() - t
        t = time.perf_counter()
        ok(m.analyze_take(estimator="rmvpe", background=False))
        out["back_rmvpe"] = time.perf_counter() - t
    print("SPEED " + json.dumps(out))


def _run(phase, pdir, tmp):
    env = dict(os.environ)
    for k in ("GLISS_F0_ESTIMATOR", "ELECTRON_RUN_AS_NODE"):
        env.pop(k, None)
    from vocal_engine.analysis.f0 import RMVPE_PATH
    env.update(VOCAL_ENGINE_WORK_DIR=os.path.join(tmp, "work"), VOCAL_ENGINE_LOG_DIR=os.path.join(tmp, "logs"),
               VOCAL_ENGINE_MODELS_DIR=os.path.dirname(RMVPE_PATH), VOCAL_ENGINE_PREP="0",
               VOCAL_ENGINE_AUTO_LYRICS="0", LOCALAPPDATA=os.path.join(tmp, "local"),
               APPDATA=os.path.join(tmp, "roaming"), PYTHONUTF8="1")
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), phase, pdir], cwd=os.path.dirname(here),
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    lines = [x for x in r.stdout.splitlines() if x.startswith("SPEED ")]
    assert r.returncode == 0 and lines, (r.stdout[-2000:], r.stderr[-2000:])
    return json.loads(lines[-1][6:])


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    tmp = str(tmp_path_factory.mktemp("speed"))
    pdir = os.path.join(tmp, "proj")
    cold = _run("cold", pdir, tmp)
    warm = _run("warm", pdir, tmp)
    return {"open_cold": cold["open"], "open_warm": warm["open"], "to_gliss": warm["to_gliss"],
            "back_rmvpe": warm["back_rmvpe"], "estimator": cold["estimator"]}


def _check(measured, name):
    limit = max(1.5 * MEDIAN_SEC[name], MEDIAN_SEC[name] + FLOOR_SEC) * SCALE
    print("%s %.2f s（上限 %.2f s）" % (name, measured[name], limit))
    assert measured[name] <= limit, "%s が %.2f s（上限 %.2f s）" % (name, measured[name], limit)


@needs_c
def test_open_cold(measured):
    _check(measured, "open_cold")


@needs_c
def test_open_warm(measured):
    _check(measured, "open_warm")


@needs_c
def test_estimator_round_trip(measured):
    _check(measured, "to_gliss")
    _check(measured, "back_rmvpe")
    assert measured["back_rmvpe"] < measured["to_gliss"]     # 戻すときは計算し直さない


SR = 44100
PHRASES, PER, NOTE_SEC, REST_SEC = 36, 6, 0.6, 1.4


def _song():
    """(頭の秒, 長さ, MIDI) の並び。続けて歌う 6 ノートと 1.4 秒の休みを 36 回（約 3 分）。隣は同じ高さにしない。"""
    rng = np.random.default_rng(7)
    notes = []
    for k in range(PHRASES):
        t0 = 0.5 + k * (PER * NOTE_SEC + REST_SEC)
        for j in range(PER):
            prev = notes[-1][2] if j else None
            notes.append((t0 + j * NOTE_SEC, NOTE_SEC,
                          int(rng.choice([m for m in (57, 59, 60, 62, 64, 65, 67) if m != prev]))))
    return notes, list(rng.uniform(-0.05, 0.05, len(notes)))


def _voice(notes, dur, transpose=0.0, seed=0, vib_hz=5.5, vib_c=30.0, shift=None):
    y = np.zeros(int(SR * dur))
    for (st, d, midi), sh in zip(notes, shift or [0.0] * len(notes)):
        st, d = st + sh, d - sh
        a, b = int(st * SR), int((st + d) * SR)
        t = np.arange(b - a) / SR
        f0 = 440.0 * 2 ** ((midi + transpose - 69) / 12)
        vib = vib_c * np.sin(2 * np.pi * vib_hz * t) * np.clip((t - 0.12) / 0.1, 0, 1)
        ph = 2 * np.pi * np.cumsum(f0 * 2 ** (vib / 1200)) / SR
        s = sum((0.6 ** k) * np.sin((k + 1) * ph) for k in range(8))
        env = np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.04))
        y[a:b] += 0.25 * s * env
    y += np.random.default_rng(seed).normal(scale=1e-4, size=len(y))
    return y.astype("float32")


def test_note_timing_ratio(tmp_path, monkeypatch):
    """曲全体をガイドに合わせた後、1 ノートのタイミングの割合を変える（ノート 3 つの中央値）。"""
    from vocal_engine import mcp_server as m
    from vocal_engine.project import Project
    monkeypatch.setenv("VOCAL_ENGINE_WORK_DIR", str(tmp_path / "work"))
    notes, shift = _song()
    dur = PHRASES * (PER * NOTE_SEC + REST_SEC) + 0.5
    take, guide = str(tmp_path / "take.wav"), str(tmp_path / "guide.wav")
    sf.write(take, _voice(notes, dur), SR, subtype="FLOAT")
    sf.write(guide, _voice(notes, dur, 0.5, 2, 4.5, 60.0, shift), SR, subtype="FLOAT")
    p = Project.open(take, guide, project_dir=str(tmp_path / "proj"), memo=False)
    p.analyze()
    monkeypatch.setitem(m._state, "project", p)

    def ok(r):
        assert r.get("ok") is not False, r.get("error")
        return r

    ids = [n.id for n in p.take_notes if n.kind == "note"]
    assert len(ids) > 180
    # 音程は Hz で寄せる（線の形で寄せると、この合成の曲では 1 点だけの鉛筆ができて止まる。0.4.0 からの別の問題）
    ok(m.correct_to_guide(pitch_strength=0.7, timing_strength=1.0, match_pitch_shape=False))
    plan, commit = [], []
    for k in (len(ids) // 3, len(ids) // 2, 2 * len(ids) // 3):
        t = time.perf_counter()
        r = ok(m.plan_edit(op="guide", note_ids=[ids[k]]))
        plan.append(time.perf_counter() - t)
        assert [c["note"] for c in r["timing_ratio"]["changed"]] == [ids[k]]
        t = time.perf_counter()
        ok(m.apply_plan(r["plan_id"], timing=0.4))
        commit.append(time.perf_counter() - t)
    measured = {"ratio_plan": float(np.median(plan)), "ratio_commit": float(np.median(commit))}
    _check(measured, "ratio_plan")
    _check(measured, "ratio_commit")


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2])
