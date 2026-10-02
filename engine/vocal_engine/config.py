# -*- coding: utf-8 -*-
"""環境変数と既定の置き場（重み）。

**重みの置き場の環境変数は `VOCAL_ENGINE_MODELS_DIR` に統一する**（DAW 連携 段階 0 で整理）。
段階 2 までは F0（RMVPE）が `VOCAL_ENGINE_MODELS_DIR`、音素（HubertFA）が `VOCAL_ENGINE_MODELS`
を見ていて、片方だけ設定すると重みが半分しか見つからなかった。旧名 `VOCAL_ENGINE_MODELS` も
後方互換として読む（`VOCAL_ENGINE_MODELS_DIR` が優先）。

既定: `%LOCALAPPDATA%\\Gliss\\models`（開発版・単体 exe（PyInstaller。配布版）とも）。画面が重みを取得する
場所と同じ。画面が起動したエンジンにも、Claude Code などに登録したエンジンにも、環境変数なしで同じ場所を見せるため。
開発版でも初回の画面のダウンロードで重みが入り、git worktree・配布版と共有できる。
"""
import os
import sys

MODELS_ENV = "VOCAL_ENGINE_MODELS_DIR"
MODELS_ENV_LEGACY = "VOCAL_ENGINE_MODELS"          # 後方互換（段階 2 までの HubertFA 側の名前）


def frozen():
    """PyInstaller などで固めた単体 exe の中で動いているか。"""
    return bool(getattr(sys, "frozen", False))


def user_data_dir():
    """利用者ごとのデータの置き場（Windows: `%LOCALAPPDATA%\\Gliss`）。重み・作業場所・プロジェクトの既定。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Gliss")


def default_models_dir():
    return os.path.join(user_data_dir(), "models")


def models_dir():
    """重みの置き場。`VOCAL_ENGINE_MODELS_DIR` → 旧名 `VOCAL_ENGINE_MODELS` → 既定。"""
    for k in (MODELS_ENV, MODELS_ENV_LEGACY):
        v = os.environ.get(k)
        if v:
            return os.path.normpath(v)
    return default_models_dir()


# 音声認識（issue #54）の重み。利用者が初回に明示してダウンロードするもので、解析モデルの置き場の
# 下の asr/ に入れる（Windows: %LOCALAPPDATA%\Gliss\models\asr）。
ASR_MODELS_ENV = "GLISS_ASR_MODELS_DIR"


def asr_models_dir():
    """音声認識の重みの置き場。`GLISS_ASR_MODELS_DIR` → OS ごとの既定。"""
    v = os.environ.get(ASR_MODELS_ENV)
    if v:
        return os.path.normpath(v)
    return os.path.join(user_data_dir(), "models", "asr")
