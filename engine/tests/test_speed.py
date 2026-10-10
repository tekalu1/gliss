# -*- coding: utf-8 -*-
"""開く・F0 の方式の往復の速さの回帰（2026-10-10 承認）。

素材 C（テイク）と C2（ガイド）を、新しいエンジンのプロセスで測る（F0 のモデルの読み込みを含む）。

- 開く（初回）: 空の作業場所で open_project → analyze_take（テイクとガイドの F0・音符・対応付け・発音の頭）
- 開く（2 回目）: 同じ作業場所を別のプロセスで開き直す（キャッシュを読むだけ）
- 方式の往復: RMVPE → Gliss → RMVPE。戻すときは方式ごとの F0 の保存（`cache/f0-<方式>.json`）と
  方式ごとのガイドの解析・対応付けから戻す

上限は、開発機（16 コア）で 3 回測った中央値（MEDIAN_SEC）の 1.5 倍。0.1 秒に満たない処理はディスクの揺れで倍を超えるので、
上限は中央値 + FLOOR_SEC を下回らせない。遅い PC では GLISS_SPEED_SCALE（倍率）で緩める。
GLISS_SPEED=0 で skip。素材・RMVPE の重みが無ければ skip（CI は回さない）。
歌詞の自動推定は切って測る（聞き取りの重みに左右されない）。
"""
import json
import os
import subprocess
import sys
import time

import pytest

if __name__ == "__main__":      # 測る側のプロセス: この worktree の vocal_engine を読む（conftest が読む前に）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import GUIDE, TAKE, needs_material, needs_model

# 開発機で 3 回測った中央値（秒）。上限はこの 1.5 倍
MEDIAN_SEC = {"open_cold": 2.42, "open_warm": 0.08, "to_gliss": 1.83, "back_rmvpe": 0.02}
FLOOR_SEC = 0.25
SCALE = float(os.environ.get("GLISS_SPEED_SCALE") or 1.0)

pytestmark = [needs_material("C", "C2"), needs_model,
              pytest.mark.skipif(os.environ.get("GLISS_SPEED") == "0", reason="GLISS_SPEED=0")]


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


def test_open_cold(measured):
    _check(measured, "open_cold")


def test_open_warm(measured):
    _check(measured, "open_warm")


def test_estimator_round_trip(measured):
    _check(measured, "to_gliss")
    _check(measured, "back_rmvpe")
    assert measured["back_rmvpe"] < measured["to_gliss"]     # 戻すときは計算し直さない


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2])
