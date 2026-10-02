# -*- coding: utf-8 -*-
"""歌詞 → 音素 → 強制アラインメント（段階2）。

  g2p.py       かな→ローマ字音節→音素（pyopenjtalk-plus / 自前テーブル。長音は吸収）
  hubertfa.py  HubertFA v0.0.7 ONNX の薄い推論（依存は onnxruntime + numpy だけ）
  model.py     PhonemeSpan / Boundary / PhonemeResult
  analyze.py   上をつないで確信度と警告を付ける
"""
from .model import Boundary, PhonemeResult, PhonemeSpan, MIN_PHONEME_MS  # noqa: F401

__all__ = ["Boundary", "PhonemeResult", "PhonemeSpan", "MIN_PHONEME_MS"]
