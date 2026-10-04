# -*- coding: utf-8 -*-
"""単体 exe（PyInstaller）の入口（DAW 連携 段階 0 §4。`docs/daw-stage0.md`）。

    vocal-engine.exe            MCP サーバー（stdio）。`python -m vocal_engine.mcp` と同じ
    vocal-engine.exe --check    依存が入っているか・torch を読み込んでいないか・アドオンを読めるかを JSON で出して終わる

重みは Gliss の F0 モデル（RMVPE の重みが無いときのピッチ検出）のほかは同梱しない。環境変数 VOCAL_ENGINE_MODELS_DIR か、既定の %LOCALAPPDATA%\\Gliss\\models（`vocal_engine/config.py`）。
"""
import json
import os
import sys


def check():
    import importlib
    import time
    # 固めた exe は PYTHONIOENCODING を見ないので、日本語（アドオンの理由・読みの結果）を UTF-8 で出す
    sys.stdout.reconfigure(encoding="utf-8")
    out = {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0]}
    t0 = time.perf_counter()
    for name in ("numpy", "scipy", "soundfile", "soxr", "librosa", "matplotlib", "onnxruntime",
                 "parselmouth", "mcp", "vocal_engine.mcp_server", "vocal_engine.render.region",
                 "vocal_engine.phoneme.hubertfa"):
        print("import", name, file=sys.stderr, flush=True)
        try:
            m = importlib.import_module(name)
            out[name] = getattr(m, "__version__", "ok")
        except Exception as e:                           # noqa: BLE001
            out[name] = "ERROR: %s" % e
    out["import_sec"] = round(time.perf_counter() - t0, 3)
    from vocal_engine import config
    from vocal_engine.render.base import list_backends
    out["models_dir"] = config.models_dir()
    out["backends"] = {b["name"]: b["available"] for b in list_backends()}
    # 同梱の Gliss の F0 モデル: 0.5 秒の 220 Hz の合成音で推論まで通るか
    try:
        import numpy as np
        from vocal_engine.analysis import f0 as f0mod
        y = 0.3 * np.sin(2 * np.pi * 220 * np.arange(24000) / 48000)
        r = f0mod.estimate_f0(x=y, sr=48000, estimator="gliss")
        hz = float(np.median(r.f0[r.voiced])) if r.voiced.any() else 0.0
        out["f0_gliss"] = ("%.1f Hz" % hz) if abs(hz - 220.0) < 5.0 else "ERROR: 220 Hz を %.1f Hz と測った" % hz
    except Exception as e:                               # noqa: BLE001
        out["f0_gliss"] = "ERROR: %s: %s" % (type(e).__name__, e)
    # 任意機能のアドオン（vocal_engine/addons.py）。読めたものは import と、漢字の読みを 1 回試す
    from vocal_engine import addons
    out["addons_dir"] = addons.addons_dir()
    out["addons"] = addons.activate()
    imports = {}
    for a in out["addons"]:
        if not a["active"]:
            continue
        try:
            modules = addons._read(a["dir"]).get("modules") or []
        except (OSError, ValueError) as e:
            imports[a["id"]] = "ERROR: %s" % e
            continue
        for name in modules:
            print("import", name, "(addon)", file=sys.stderr, flush=True)
            try:
                m = importlib.import_module(name)
                imports[name] = getattr(m, "__version__", "ok")
            except Exception as e:                       # noqa: BLE001
                imports[name] = "ERROR: %s: %s" % (type(e).__name__, e)
    out["addon_imports"] = imports
    if "pyopenjtalk" in imports and not str(imports["pyopenjtalk"]).startswith("ERROR"):
        from vocal_engine.phoneme.g2p import text_to_kana
        try:
            out["addon_probe"] = {"夜空に歌う": text_to_kana("夜空に歌う")}
        except Exception as e:                           # noqa: BLE001
            out["addon_probe"] = "ERROR: %s: %s" % (type(e).__name__, e)
    # 聞き取りのアドオン: GLISS_CHECK_ASR_MODEL（faster-whisper 形式の重みのフォルダ。小さい tiny で足りる）を
    # 渡したときだけ、エンジンと同じ引数で 2 秒の合成音を CPU で聞き取る（import だけでなく推論まで通るか）
    asr_model = os.environ.get("GLISS_CHECK_ASR_MODEL")
    if asr_model and "faster_whisper" in imports and not str(imports["faster_whisper"]).startswith("ERROR"):
        try:
            import numpy as np
            from faster_whisper import WhisperModel
            t1 = time.perf_counter()
            model = WhisperModel(asr_model, device="cpu", compute_type="int8", cpu_threads=4)
            y = (0.2 * np.sin(2 * np.pi * 220 * np.arange(32000) / 16000)).astype(np.float32)
            segments, _info = model.transcribe(y, language="ja", beam_size=5, condition_on_previous_text=False,
                                               vad_filter=False, word_timestamps=True)
            out["addon_asr_probe"] = {"segments": len(list(segments)),
                                      "sec": round(time.perf_counter() - t1, 2)}
        except Exception as e:                           # noqa: BLE001
            out["addon_asr_probe"] = "ERROR: %s: %s" % (type(e).__name__, e)
    out["torch_loaded"] = any(k == "torch" or k.startswith("torch.") for k in sys.modules)
    try:
        importlib.import_module("torch")
        out["torch_importable"] = True
    except Exception:                                    # noqa: BLE001
        out["torch_importable"] = False
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        check()
    else:
        from vocal_engine.mcp_server import main
        main()
