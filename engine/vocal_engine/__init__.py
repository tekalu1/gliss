# -*- coding: utf-8 -*-
"""vocal_engine — Gliss（歌声のピッチ・タイミング編集ツール）の Python エンジン（段階1）。

構成:
  analysis/  F0（RMVPE ONNX を正。重みが無ければ Gliss の F0 モデル（同梱）。Praat・FCPE も選べる）、音符のかたまり、音素（未実装）、ガイド対応付け
  project/   元音声の参照＋解析キャッシュ＋編集リスト（非破壊）＋取り消し履歴
  render/    バックエンド抽象（既定は Praat の TD-PSOLA `praat`。自前 TD-PSOLA `psola`、`world` も選べる）
  view/      matplotlib(Agg) のピアノロール PNG
  mcp_server 測る／直す／確かめる を MCP（stdio）で公開

設計の約束:
  - 編集は非破壊。編集していない区間は元のサンプルをそのまま出す。
  - MCP の返り値に生の数値列・画像を入れない（要約統計＋ファイルパス）。
  - ログは stderr ではなく <project>/engine.log に自前で書く。
"""

__version__ = "0.1.0-beta.12"

HOP_S = 0.010          # 解析の共通ホップ（10 ms）
XFADE_MS = 20.0        # 編集区間と原音のつなぎ目のクロスフェード長
