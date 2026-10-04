"""初回起動画面が参照するエンジンの重み有無の判定を確かめる。"""
import json
import os
import subprocess
import sys


def _info(models_dir, **extra):
    env = {**os.environ, "VOCAL_ENGINE_MODELS_DIR": str(models_dir)}
    env.pop("GLISS_F0_ESTIMATOR", None)
    env.update(extra)
    code = "import json; from vocal_engine.mcp_server import engine_info; print(json.dumps(engine_info()))"
    result = subprocess.run([sys.executable, "-c", code], env=env, check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


def test_engine_info_reports_missing_and_ready_models(tmp_path):
    missing = _info(tmp_path)
    assert missing["models_dir"] == str(tmp_path)
    assert not missing["rmvpe_model_found"]
    assert not missing["phonemes"]["model_found"]
    # 既定は同梱の Gliss の F0 モデル（RMVPE の重みは任意）
    assert missing["gliss_f0_model_found"]
    assert missing["f0_estimator"] == "gliss" and missing["f0_estimator_effective"] == "gliss"
    assert missing["f0_estimator_default"] == "gliss" and missing["f0_estimator_chosen"] is None
    # RMVPE を選んでいても、重みが無ければ Gliss の F0 モデルで解析する
    chosen = _info(tmp_path, GLISS_F0_ESTIMATOR="rmvpe")
    assert chosen["f0_estimator"] == "rmvpe" and chosen["f0_estimator_effective"] == "gliss"

    (tmp_path / "rmvpe.onnx").write_bytes(b"fake")
    hubert = tmp_path / "hubertfa" / "1218_hfa_model_new_dict"
    hubert.mkdir(parents=True)
    for name in ("model.onnx", "vocab.json", "config.json"):
        (hubert / name).write_bytes(b"fake")
    ready = _info(tmp_path)
    assert ready["rmvpe_model_found"]
    assert ready["phonemes"]["model_found"]
    assert ready["f0_estimator_effective"] == "gliss"         # 重みがあっても既定は Gliss
    assert _info(tmp_path, GLISS_F0_ESTIMATOR="rmvpe")["f0_estimator_effective"] == "rmvpe"
