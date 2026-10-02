# -*- coding: utf-8 -*-
"""任意機能のアドオン（配布版のエンジン exe に入れていない Python の依存を、後から取得して読む）。

配布版の exe（PyInstaller）には、漢字の歌詞の読み（pyopenjtalk-plus・SudachiPy の辞書）のような
大きくて任意の依存を入れていない。画面が「アドオン」として取得し（`app/addons.mjs`）、ここに置く:

    %LOCALAPPDATA%\\Gliss\\addons\\<id>\\
        gliss-addon.json     manifest（scripts/build-addon.mjs が作る）
        LICENSES.txt
        site-packages\\       exe に無いパッケージだけ（wheel を展開したもの）

エンジンは起動時に `activate()` で、**互換のある**アドオンの site-packages を `sys.path` の**末尾**に足す
（exe の中のパッケージが先に見つかる。numpy などはアドオンに入れていない）。画面が起動したエンジンにも、
AI クライアントに登録したエンジンにも、環境変数なしで効く。取得した直後は画面が `engine_info(reload_addons=True)`
で読み直させる（再起動しなくてよい）。

互換: manifest の Python の版（ABI）・OS と、exe と共有するパッケージ（`requires`。numpy・pydantic など）の版が
このエンジンと**完全に一致**するときだけ読む。合わなければ読まず、理由を `status()` で返す（画面が「更新が要る」と出す）。

開発版（`.venv` の python）では読まない（venv に入っていればそれを使う）。`GLISS_ADDONS_DIR` を
指定したときだけ読む（テスト）。そのときも、manifest の `modules` がすでに import できる（venv に入っている）
アドオンは足さない（`source: "environment"`）。

削除: 読み込んだ拡張モジュール（.pyd）は Windows ではエンジンが動いている間は消せない。画面は消せなければ
`gliss-addon.remove` を置き、ここはそれがあるアドオンを読まない（次に画面が起動したときに消す）。
"""
import importlib
import importlib.util
import json
import os
import re
import sys
import sysconfig

from . import config

ADDONS_ENV = "GLISS_ADDONS_DIR"
MANIFEST = "gliss-addon.json"
REMOVE_MARK = "gliss-addon.remove"
FORMAT = 1
ENGINE_PACKAGES = "gliss-engine-packages.json"   # 固めた exe の中（vocal-engine.spec が作る）

_status = []          # 最後に activate() で見たもの
_added = {}           # id -> sys.path に足した場所


def addons_dir():
    """アドオンの置き場。`GLISS_ADDONS_DIR` → `%LOCALAPPDATA%\\Gliss\\addons`。"""
    v = os.environ.get(ADDONS_ENV)
    if v:
        return os.path.normpath(v)
    return os.path.join(config.user_data_dir(), "addons")


def enabled():
    """アドオンを読むか（配布版の exe、または置き場を明示したとき）。"""
    return config.frozen() or bool(os.environ.get(ADDONS_ENV))


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def python_tag():
    return "cp%d%d" % sys.version_info[:2]


def platform_tag():
    return sysconfig.get_platform().replace("-", "_").replace(".", "_")


_engine_packages = None


def engine_packages():
    """このエンジンに入っているパッケージの版 {正規化した名前: 版}。

    固めた exe には dist-info がほとんど入らない（importlib.metadata で引けない）ので、spec がビルドの venv から
    書いた一覧（`gliss-engine-packages.json`）を読む。開発版は importlib.metadata。
    """
    global _engine_packages
    if _engine_packages is not None:
        return _engine_packages
    out = {}
    meipass = getattr(sys, "_MEIPASS", None)
    path = os.path.join(meipass, ENGINE_PACKAGES) if meipass else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out = {normalize(k): v for k, v in json.load(f).items()}
    else:
        from importlib import metadata
        for d in metadata.distributions():
            name = d.metadata.get("Name")
            if name:
                out.setdefault(normalize(name), d.version)
    _engine_packages = out
    return out


def incompatibility(manifest, packages=None):
    """このエンジンで読めない理由（読めるなら None）。"""
    if manifest.get("format") != FORMAT:
        return "アドオンの形式（%s）がこの版のエンジンと違う" % manifest.get("format")
    if manifest.get("python") != python_tag():
        return "Python の版が違う（アドオン %s・エンジン %s）" % (manifest.get("python"), python_tag())
    if manifest.get("platform") != platform_tag():
        return "OS が違う（アドオン %s・エンジン %s）" % (manifest.get("platform"), platform_tag())
    packages = engine_packages() if packages is None else packages
    diff = []
    for name, version in sorted((manifest.get("requires") or {}).items()):
        have = packages.get(normalize(name))
        if have != version:
            diff.append("%s %s（エンジンは %s）" % (name, version, have or "無し"))
    if diff:
        return "エンジンの依存の版が違う: " + "、".join(diff)
    return None


def _importable(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _read(folder):
    with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
        return json.load(f)


def activate():
    """置き場のアドオンを調べ、互換のあるものを sys.path に足す。何度呼んでもよい（取得の後に読み直す）。

    返り値は status() と同じ。
    """
    global _status
    root = addons_dir()
    found = []
    if enabled() and os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            # 画面が展開・削除の途中に使う一時フォルダ（.staging-* / .trash-*）は読まない
            if name.startswith(".") or not os.path.isfile(os.path.join(folder, MANIFEST)):
                continue
            st = {"id": name, "dir": folder, "active": False, "compatible": False,
                  "source": None, "reason": None, "key": None, "title": None}
            found.append(st)
            if os.path.exists(os.path.join(folder, REMOVE_MARK)):
                st["reason"] = "削除の待ち（Gliss を次に起動したときに消す）"
                continue
            try:
                m = _read(folder)
            except (OSError, ValueError) as e:
                st["reason"] = "manifest を読めない: %s" % e
                continue
            st.update(key=m.get("key"), title=m.get("title"), packages=m.get("packages") or {})
            if m.get("id") != name:
                st["reason"] = "フォルダ名と manifest の id（%s）が違う" % m.get("id")
                continue
            why = incompatibility(m)
            if why:
                st["reason"] = why
                continue
            st["compatible"] = True
            site = os.path.join(folder, "site-packages")
            if name in _added:
                st.update(active=True, source="addon")
                continue
            modules = m.get("modules") or []
            if modules and all(_importable(mod) for mod in modules):
                st["source"] = "environment"     # venv などにすでに入っている
                continue
            sys.path.append(site)
            _added[name] = site
            st.update(active=True, source="addon")
    # 消えたアドオンを sys.path から外す（読み込み済みのモジュールはそのまま）
    present = {s["id"] for s in found if s["active"]}
    for name in list(_added):
        if name not in present:
            try:
                sys.path.remove(_added.pop(name))
            except ValueError:
                pass
    importlib.invalidate_caches()
    _status = found
    return status()


def status():
    """[{id, title, key, dir, active, compatible, source, reason, packages}]"""
    return [dict(s) for s in _status]


def summary():
    """engine_info に載せるもの。"""
    return {"dir": addons_dir(), "enabled": enabled(), "python": python_tag(),
            "platform": platform_tag(), "installed": status()}
