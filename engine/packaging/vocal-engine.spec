# -*- mode: python ; coding: utf-8 -*-
# 単体 exe（PyInstaller）。配布版が resources/engine/ に同梱する（app/electron-builder.yml の extraResources）。
# 普段は scripts/build-engine.mjs から呼ぶ（`pnpm build:engine`。.venv-exe の作成・依存の固定・--check まで）。
# 手で呼ぶなら:
#   cd engine\packaging
#   ..\..\.venv-exe\Scripts\pyinstaller.exe vocal-engine.spec --noconfirm        （フォルダ形式 = dist\vocal-engine\）
#   set "VE_ONEFILE=1" && ..\..\.venv-exe\Scripts\pyinstaller.exe vocal-engine.spec --noconfirm  （1 ファイル = dist\vocal-engine-onefile.exe。実測用。配布はフォルダ形式）
# .venv-exe は torch を入れていない環境（本体の .venv とは別。requirements-exe.txt）。重みは Gliss の F0 モデル（RMVPE の重みが無いときのピッチ検出）だけ同梱する。
import os
import hashlib
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ENGINE = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEFILE = (os.environ.get("VE_ONEFILE") or "").strip() == "1"   # cmd の `set X=1 & ...` は "1 " になる

hidden = (collect_submodules("vocal_engine")
          + collect_submodules("librosa")          # lazy_loader で遅れて import する
          + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli")))
datas = collect_data_files("librosa") + collect_data_files("parselmouth")
# 同梱の重み: 現行モデルと、旧 ARA アーカイブのノート ID 復元用 v2。
# ほかの重み（RMVPE・HubertFA）は同梱しない
for model_name in ("gliss-f0.onnx", "gliss-f0-v2.onnx"):
    datas.append((os.path.join(ENGINE, "vocal_engine", "analysis", "models", model_name),
                  os.path.join("vocal_engine", "analysis", "models")))

# 実際にパックする Python ソースから版を作る。__version__ を上げ忘れても描画キャッシュを分ける。
_build_hash = hashlib.sha256()
_source_root = os.path.join(ENGINE, "vocal_engine")
for root, dirs, files in os.walk(_source_root):
    dirs.sort()
    for filename in sorted(files):
        if filename.endswith(".py"):
            path = os.path.join(root, filename)
            _build_hash.update(os.path.relpath(path, ENGINE).encode("utf-8"))
            with open(path, "rb") as source:
                _build_hash.update(source.read())
_version_path = os.path.join(SPECPATH, "build", "gliss-build-version.txt")
os.makedirs(os.path.dirname(_version_path), exist_ok=True)
with open(_version_path, "w", encoding="ascii") as version_file:
    version_file.write(_build_hash.hexdigest()[:24])
datas.append((_version_path, "."))

# exe に入れたパッケージの版（固めた exe には dist-info がほとんど入らない）。アドオンの互換の判定に使う
# （vocal_engine/addons.py。アドオンの manifest の requires と照らす）
import json
from importlib import metadata as _metadata
_packages = {}
for _dist in _metadata.distributions():
    _name = _dist.metadata.get("Name")
    if _name:
        _packages.setdefault(_name, _dist.version)
_packages_path = os.path.join(SPECPATH, "build", "gliss-engine-packages.json")
with open(_packages_path, "w", encoding="utf-8") as _f:
    json.dump(dict(sorted(_packages.items(), key=lambda kv: kv[0].lower())), _f, indent=1)
datas.append((_packages_path, "."))

a = Analysis(
    [os.path.join(SPECPATH, "vocal_engine_entry.py")],
    pathex=[ENGINE],
    datas=datas,
    hiddenimports=hidden,
    excludes=["torch", "torchfcpe", "torchaudio", "torchvision", "tkinter", "pytest",
              "IPython", "jupyter", "notebook", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
# VC++ ランタイムは System32 のもの（新しい方）を入れる。PATH の上の別のアプリ（実測では JDK 11 の
# msvcp140.dll 14.16）を拾うと、onnxruntime（14.3x 以上が要る）が import で落ちる（Segmentation fault）
_SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_VCRT = {"msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll",
         "vcruntime140_1.dll", "concrt140.dll", "vcomp140.dll"}


def _vcrt_from_system(entry):
    name, src, typ = entry
    base = os.path.basename(name).lower()
    if os.path.dirname(name) == "" and base in _VCRT:
        cand = os.path.join(_SYS32, base)
        if os.path.exists(cand):
            return (name, cand, typ)
    return entry


a.binaries = [_vcrt_from_system(b) for b in a.binaries]
pyz = PYZ(a.pure)
if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="vocal-engine-onefile",
              console=True, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vocal-engine", console=True,
              upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="vocal-engine", upx=False)
