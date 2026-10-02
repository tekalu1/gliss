# -*- coding: utf-8 -*-
"""任意機能のアドオン（vocal_engine/addons.py）: 互換の判定・sys.path への足し方・削除の待ち。"""
import json
import os
import sys

import pytest

from vocal_engine import addons, config


@pytest.fixture
def addon_root(tmp_path, monkeypatch):
    """GLISS_ADDONS_DIR を一時フォルダにし、終わったら sys.path とモジュールを元に戻す。"""
    monkeypatch.setenv(addons.ADDONS_ENV, str(tmp_path))
    path0 = list(sys.path)
    added0 = dict(addons._added)
    yield tmp_path
    sys.path[:] = path0
    addons._added.clear()
    addons._added.update(added0)
    for name in [m for m in sys.modules if m.startswith("gliss_test_addon")]:
        del sys.modules[name]


def engine_numpy():
    return addons.engine_packages()["numpy"]


def make_addon(root, name, module="gliss_test_addon_a", **override):
    folder = root / name
    site = folder / "site-packages"
    site.mkdir(parents=True)
    (site / ("%s.py" % module)).write_text("VALUE = %r\n" % name, encoding="utf-8")
    manifest = {"format": 1, "id": name, "title": "テスト", "key": "k1",
                "python": addons.python_tag(), "platform": addons.platform_tag(),
                "requires": {"numpy": engine_numpy()}, "packages": {"gliss-test": "1"},
                "modules": [module]}
    manifest.update(override)
    (folder / addons.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_incompatibility_reasons():
    ok = {"format": 1, "python": addons.python_tag(), "platform": addons.platform_tag(),
          "requires": {"numpy": "2.4.6"}}
    assert addons.incompatibility(ok, {"numpy": "2.4.6"}) is None
    assert "Python" in addons.incompatibility({**ok, "python": "cp312"}, {"numpy": "2.4.6"})
    assert "OS" in addons.incompatibility({**ok, "platform": "other_os"}, {"numpy": "2.4.6"})  # 今の OS と必ず違う値
    why = addons.incompatibility(ok, {"numpy": "2.5.0"})
    assert "numpy 2.4.6（エンジンは 2.5.0）" in why
    assert "無し" in addons.incompatibility({**ok, "requires": {"pydantic": "2"}}, {"numpy": "2.4.6"})
    assert "形式" in addons.incompatibility({**ok, "format": 2}, {"numpy": "2.4.6"})
    # 名前の書き方の違い（pydantic_core と pydantic-core）は同じものとみなす
    assert addons.incompatibility({**ok, "requires": {"pydantic_core": "2.46.5"}}, {"pydantic-core": "2.46.5"}) is None


def test_activate_adds_compatible_addon_to_end_of_sys_path(addon_root):
    make_addon(addon_root, "lyrics-ja")
    st = addons.activate()
    assert [(s["id"], s["active"], s["source"]) for s in st] == [("lyrics-ja", True, "addon")]
    site = str(addon_root / "lyrics-ja" / "site-packages")
    assert sys.path[-1] == site                      # exe の中のパッケージが先に見つかる
    import gliss_test_addon_a
    assert gliss_test_addon_a.VALUE == "lyrics-ja"
    # 何度呼んでも二重に足さない
    addons.activate()
    assert sys.path.count(site) == 1
    info = addons.summary()
    assert info["dir"] == str(addon_root) and info["installed"][0]["active"]


def test_incompatible_addon_is_not_loaded(addon_root):
    make_addon(addon_root, "lyrics-ja", requires={"numpy": "0.0.1"})
    st = addons.activate()[0]
    assert not st["active"] and not st["compatible"]
    assert "numpy 0.0.1" in st["reason"]
    assert str(addon_root / "lyrics-ja" / "site-packages") not in sys.path


def test_remove_mark_and_removed_folder(addon_root):
    folder = make_addon(addon_root, "lyrics-ja")
    addons.activate()
    site = str(folder / "site-packages")
    assert site in sys.path
    (folder / addons.REMOVE_MARK).write_text("", encoding="utf-8")
    st = addons.activate()[0]
    assert not st["active"] and "削除" in st["reason"]
    assert site not in sys.path
    # 消えたら一覧からも消える
    import shutil
    shutil.rmtree(folder)
    assert addons.activate() == []


def test_already_installed_modules_win(addon_root):
    """開発の venv に入っているもの（ここでは numpy）はアドオンより venv を使う。"""
    make_addon(addon_root, "lyrics-ja", modules=["numpy"])
    st = addons.activate()[0]
    assert st["compatible"] and not st["active"] and st["source"] == "environment"


def test_folder_name_must_match_manifest(addon_root):
    make_addon(addon_root, "lyrics-ja", id="other")
    st = addons.activate()[0]
    assert not st["active"] and "id" in st["reason"]


def test_disabled_in_dev_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv(addons.ADDONS_ENV, raising=False)
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    assert not addons.enabled()
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    assert addons.enabled()
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert addons.addons_dir() == os.path.join(r"C:\Users\x\AppData\Local", "Gliss", "addons")


def test_engine_info_reports_and_reloads_addons(addon_root):
    from vocal_engine.mcp_server import engine_info
    info = engine_info()
    assert info["addons"]["dir"] == str(addon_root)
    make_addon(addon_root, "lyrics-ja")
    info = engine_info(reload_addons=True)
    assert [a["id"] for a in info["addons"]["installed"] if a["active"]] == ["lyrics-ja"]


def test_kanji_hint_points_to_addon_when_frozen(monkeypatch):
    from vocal_engine.phoneme import g2p
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    assert "追加の機能" in g2p.kanji_hint()
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    assert "pyopenjtalk-plus" in g2p.kanji_hint()
