# -*- coding: utf-8 -*-
"""テスト素材（リポジトリに入れない実際の歌声）の場所と、素材に結び付いたデータ。

素材は同梱しない。手元の素材のフォルダを環境変数 `GLISS_TEST_MATERIALS` で指すと、素材が要るテストも回る
（指していなければ、そのテストは skip になる）。フォルダの中身:

    materials.json   素材の一覧（記号 → ファイル名）と、素材に結び付いたデータ（歌詞・期待値など）
    *.wav            materials.json の "clips" に書いたファイル

materials.json の形（キーの意味は docs/testing.md）:

    {"clips": {"A": "a.wav", "C": "c.wav", ...},
     "data": {"C.lyrics": "...", "C.syllables": 17, ...}}

素材の歌詞やファイル名はテストのコードに書かない（公開リポジトリに第三者の歌詞を残さないため）。
コードは記号（"C" など）とデータのキーで引く。
"""
import json
import os

ENV = "GLISS_TEST_MATERIALS"
DIR = os.path.normpath(os.environ[ENV]) if os.environ.get(ENV) else ""
_MISSING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_no_materials")


def _load():
    if not DIR:
        return {}
    p = os.path.join(DIR, "materials.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load()


def available(*symbols):
    """素材のフォルダがあり、指定した記号の WAV が全部そろっているか。"""
    return bool(_DATA) and all(os.path.exists(clip(s)) for s in symbols)


def clip(symbol):
    """記号の素材の WAV のパス。素材が無いときは存在しないパスを返す（skip の判定に使える）。"""
    name = _DATA.get("clips", {}).get(symbol)
    if not name:
        return os.path.join(_MISSING, symbol + ".wav")
    return os.path.join(DIR, name)


def path(name):
    """素材のフォルダの中のファイル（素材から作った回帰値など）。素材が無いときは存在しないパス。"""
    return os.path.join(DIR or _MISSING, name)


def data(key, default=None):
    """素材に結び付いたデータ（歌詞・期待値など）。素材が無いときは default（テストは skip される前提）。"""
    return _DATA.get("data", {}).get(key, default)


def text(key):
    """文字列のデータ。素材が無いときは空文字（モジュールの読み込みで落ちないように）。"""
    v = data(key)
    return v if isinstance(v, str) else ""
