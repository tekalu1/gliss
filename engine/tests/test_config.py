# -*- coding: utf-8 -*-
"""配布版（単体 exe）の置き場と、版の一致。"""
import json
import os
import re

import pytest

from vocal_engine import config

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_frozen_models_dir_is_user_data(monkeypatch):
    """単体 exe の既定の重みの置き場は、画面の取得先（%LOCALAPPDATA%\\Gliss\\models）と同じ。
    Claude Code に登録した exe には画面の環境変数が渡らないので、既定が揃っている必要がある。"""
    monkeypatch.delenv("VOCAL_ENGINE_MODELS_DIR", raising=False)
    monkeypatch.delenv("VOCAL_ENGINE_MODELS", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert config.models_dir() == os.path.join(r"C:\Users\x\AppData\Local", "Gliss", "models")
    monkeypatch.setenv("VOCAL_ENGINE_MODELS_DIR", r"C:\elsewhere")
    assert config.models_dir() == os.path.normpath(r"C:\elsewhere")      # 環境変数が優先
    monkeypatch.delenv("GLISS_ASR_MODELS_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert config.asr_models_dir() == os.path.join(r"C:\Users\x\AppData\Local", "Gliss", "models", "asr")


def test_frozen_projects_root_is_outside_install_dir(monkeypatch):
    from vocal_engine.project import store
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert store._default_projects_root() == os.path.join(r"C:\Users\x\AppData\Local", "Gliss", "projects")


def _pep440(version):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?", version)
    assert m, "版は 0.1.0 か 0.1.0-beta.1 の形: %s" % version
    major, minor, patch, pre, n = m.groups()
    return "%s.%s.%s%s" % (major, minor, patch, ({"alpha": "a", "beta": "b", "rc": "rc"}[pre] + n) if pre else "")


def test_versions_match_app_package_json():
    """版の正本は app/package.json。ずれたら `node scripts/sync-version.mjs` で直す。"""
    package = os.path.join(REPO, "app", "package.json")
    if not os.path.exists(package):
        pytest.skip("app/package.json が無い（engine だけを取り出した環境）")
    with open(package, encoding="utf-8") as f:
        version = json.load(f)["version"]
    from vocal_engine import __version__
    assert __version__ == version
    with open(os.path.join(REPO, "engine", "pyproject.toml"), encoding="utf-8") as f:
        m = re.search(r'^version = "([^"]*)"$', f.read(), re.M)
    assert m and m.group(1) == _pep440(version)
