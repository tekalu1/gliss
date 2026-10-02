# -*- coding: utf-8 -*-
"""音声認識の重みの一覧（issue #54）。

重みは配布物に入れない。利用者が「聞き取る」を初めて使うとき、大きさ・保存先・ライセンスを
確かめてからダウンロードする（`download.py`）。版は固定し、各ファイルの大きさと SHA-256 を
ここに持つ（途中で切れた・差し替わったファイルを使わない）。候補の比較は issue #54。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..config import asr_models_dir

DOWNLOAD_BASE_ENV = "GLISS_ASR_DOWNLOAD_BASE"
DEFAULT_DOWNLOAD_BASE = "https://huggingface.co"
FAKE_ENV = "GLISS_ASR_FAKE"          # テスト用の偽の認識器（`fake.py`）の設定 JSON のパス


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True)
class AsrModel:
    id: str
    label: str
    backend: str                       # "faster-whisper" / "fake"
    repo: str
    revision: str
    files: tuple[ModelFile, ...]
    license: str
    license_url: str
    source_url: str
    upstream: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def size(self):
        return sum(f.size for f in self.files)

    def url(self, f, base=None):
        base = (base or os.environ.get(DOWNLOAD_BASE_ENV) or DEFAULT_DOWNLOAD_BASE).rstrip("/")
        return "%s/%s/resolve/%s/%s" % (base, self.repo, self.revision, f.name)

    def path(self, root=None):
        return os.path.join(root or asr_models_dir(), self.id)

    def installed(self, root=None):
        """全ファイルがそろい、大きさが合っている（SHA-256 はダウンロード時に確かめた）。"""
        d = self.path(root)
        for f in self.files:
            p = os.path.join(d, f.name)
            if not os.path.isfile(p) or os.path.getsize(p) != f.size:
                return False
        return True

    def info(self, root=None):
        return {"id": self.id, "label": self.label, "backend": self.backend,
                "size_bytes": self.size, "size_text": size_text(self.size),
                "license": self.license, "license_url": self.license_url,
                "source_url": self.source_url, "upstream": self.upstream,
                "revision": self.revision, "note": self.note,
                "path": self.path(root), "installed": self.installed(root)}


def size_text(n):
    if n >= 1e9:
        return "%.2f GB" % (n / 1e9)
    if n >= 1e6:
        return "%.0f MB" % (n / 1e6)
    return "%.0f KB" % (n / 1e3)


# GPU では large-v3 が歌の誤りが最も少なかった（歌のかな誤り率 16.5%、音声 10 秒あたり 1.1 秒）。
# CPU でも動くが遅い（同 約 29 秒）。CPU 向けの ReazonSpeech k2-v2 は残課題。
WHISPER_LARGE_V3 = AsrModel(
    id="whisper-large-v3",
    label="Whisper large-v3（faster-whisper 版）",
    backend="faster-whisper",
    repo="Systran/faster-whisper-large-v3",
    revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
    files=(
        ModelFile("config.json", 2394,
                  "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9"),
        ModelFile("preprocessor_config.json", 340,
                  "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
        ModelFile("tokenizer.json", 2480617,
                  "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca"),
        ModelFile("vocabulary.json", 1068114,
                  "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
        ModelFile("model.bin", 3087284237,
                  "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
    ),
    license="MIT",
    license_url="https://huggingface.co/openai/whisper-large-v3",
    source_url="https://huggingface.co/Systran/faster-whisper-large-v3",
    upstream="OpenAI Whisper large-v3（MIT）を CTranslate2 形式に変換したもの（MIT）",
)

DEFAULT_MODEL = WHISPER_LARGE_V3.id
_REAL = (WHISPER_LARGE_V3,)


def fake_config():
    """テスト用の偽の認識器の設定（`GLISS_ASR_FAKE` が指す JSON）。無ければ None。"""
    path = os.environ.get(FAKE_ENV)
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fake_model(cfg):
    size = int(cfg.get("file_size", 4_000_000))
    return AsrModel(id="fake-asr", label="偽の認識器（テスト用）", backend="fake",
                    repo="gliss/fake-asr", revision="test",
                    files=(ModelFile("model.bin", size, None),),
                    license="MIT", license_url="https://example.invalid/license",
                    source_url="https://example.invalid/fake-asr",
                    upstream="テスト専用", extra={"fake": cfg})


def models():
    cfg = fake_config()
    return (_fake_model(cfg),) if cfg is not None else _REAL


def default_model_id():
    return models()[0].id


def get(model_id="auto", root=None):
    """モデル id（"auto" = 入っているもの、無ければ既定）→ AsrModel。"""
    ms = models()
    if model_id in (None, "", "auto"):
        return next((m for m in ms if m.installed(root)), ms[0])
    for m in ms:
        if m.id == model_id:
            return m
    raise KeyError("そんな音声認識のモデルは無い: %s（使えるもの: %s）"
                   % (model_id, ", ".join(m.id for m in ms)))
