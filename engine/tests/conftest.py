# -*- coding: utf-8 -*-
import os
import shutil
import tempfile

import pytest

import materials as M

# テストは画面（Gliss）が起動したエンジンとして動かす（画面の「AI に許可」に左右されない。bridge.py）。
# AI 側の動き（許可・引数なしの load_project）は test_bridge.py が環境変数を差し替えて確かめる。
# 人の %APPDATA%\Gliss\bridge.json は読まない
os.environ["GLISS_CLIENT"] = "app"
os.environ["GLISS_BRIDGE"] = os.path.join(tempfile.gettempdir(), "gliss-test-bridge", "bridge.json")
# 裏の準備（issue #63。prep.py）は既定で止める（計算の回数を数えるテストが、裏のスレッドの分で揺れないように）。
# 準備そのものは test_prep.py が有効にして確かめる。GLISS_TEST_PREP=1 で全部のテストを準備ありで流せる
# new_project / save_project を呼ぶテストは VOCAL_ENGINE_WORK_DIR を tmp_path に向けること。向けないと
# 人の %LOCALAPPDATA%\Gliss\work に作業場所を作ってしまう。
os.environ.setdefault("VOCAL_ENGINE_PREP", "1" if os.environ.get("GLISS_TEST_PREP") == "1" else "0")

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
ROOT = os.path.dirname(ENGINE)
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PY):
    # git worktree には .venv が無い: テストを走らせている python で stdio のサーバーも起こす
    import sys
    VENV_PY = sys.executable

# 素材（実際の歌声）は同梱しない。GLISS_TEST_MATERIALS で手元の素材のフォルダを指す（materials.py）。
# 記号の意味: C = テイク、C2 = C と同じ歌詞の別テイク（ガイド）、A = 無声の子音を挟むフレーズ、
# E = 短い音節の繰り返し（docs/testing.md に一覧）
TAKE = M.clip("C")
GUIDE = M.clip("C2")
CLIP_A = M.clip("A")
CLIP_E = M.clip("E")


def stem(path):
    """トラックの既定の名前（ファイル名から拡張子を除いたもの）。"""
    return os.path.splitext(os.path.basename(path))[0]


def _model_ok():
    from vocal_engine.analysis.f0 import RMVPE_PATH
    return os.path.exists(RMVPE_PATH)


needs_model = pytest.mark.skipif(not _model_ok(),
                                 reason="解析モデルの重み（rmvpe.onnx）が無い（重みは同梱しない。VOCAL_ENGINE_MODELS_DIR）")
needs_clips = pytest.mark.skipif(not M.available("C", "C2", "A", "E"),
                                 reason="テスト素材が無い（GLISS_TEST_MATERIALS）")


def needs_material(*symbols):
    """指定した記号の素材が要るテスト（無ければ skip）。"""
    return pytest.mark.skipif(not M.available(*symbols),
                              reason="テスト素材 %s が無い（GLISS_TEST_MATERIALS）" % ", ".join(symbols))


@pytest.fixture(scope="module")
def rmvpe_f0():
    """このモジュールのテストは F0 を RMVPE で解析する（`pytestmark` の `usefixtures("rmvpe_f0")`）。
    ノートの ID・数・区切りの位置を RMVPE の解析で書いた、編集・接続・ガイド・書き出しのテスト用（F0 の方式を
    確かめるテストではない）。既定の Gliss の F0 モデルでは、同じ素材でも区切りが変わる（`docs/testing.md`）。
    子のプロセス（stdio のサーバー）にも効くように、環境変数でも渡す。"""
    from vocal_engine.analysis import f0 as F
    before = F._preferred
    mp = pytest.MonkeyPatch()
    mp.setenv(F.ESTIMATOR_ENV, "rmvpe")
    F.set_preferred_estimator("rmvpe")
    yield
    F.set_preferred_estimator(before)
    mp.undo()


@pytest.fixture(scope="session")
def f0_take():
    """テイクの F0（画面と同じ既定の方式。GLISS_F0_ESTIMATOR で替えられる）。"""
    from vocal_engine.analysis.f0 import estimate_f0
    from vocal_engine.audio import read_mono
    x, sr = read_mono(TAKE)
    return estimate_f0(x=x, sr=sr, estimator=None), x, sr


@pytest.fixture
def project(tmp_path):
    from vocal_engine.project import Project
    p = Project.open(TAKE, GUIDE, project_dir=str(tmp_path / "proj"))
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)
