"""初回起動画面のテスト用に小さな zip を作る。実際の重みは取得しない。"""
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1], "w", compression=zipfile.ZIP_DEFLATED) as out:
    if sys.argv[2] == "rmvpe":
        out.writestr("rmvpe-onnx/rmvpe.onnx", b"fake-rmvpe")
    else:
        for name in ("model.onnx", "vocab.json", "config.json", "VERSION", "japanese_dict_full.txt"):
            out.writestr("1218_hfa_model_new_dict/" + name, b"fake-" + name.encode())
