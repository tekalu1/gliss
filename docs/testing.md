# テストの回し方

| テスト | 場所 | 回し方 | 要るもの |
|---|---|---|---|
| エンジン | `engine/tests`（pytest） | `cd engine` → `..\.venv\Scripts\python.exe -m pytest -q` | `.venv`（[development.md](development.md)） |
| 画面の単体テスト | `app/tests/unit`（Electron を起動しない） | `cd app` → `pnpm test:unit` | `pnpm install` |
| 画面の通しのテスト | `app/tests/*.spec.js`（Playwright で Electron を起動） | `cd app` → `pnpm test`（ウィンドウを見るなら `pnpm test:headed`） | エンジンの `.venv` |

CI（`.github/workflows/test.yml`）は、エンジンの pytest（ubuntu）と画面の単体テスト・版とリリースノートの検査（windows）、
gitleaks による全履歴の秘密情報の検査（ubuntu）を回す。
素材・重みが要るテストは CI では skip になる。

## 素材と重みが無くても回る範囲

テストの素材（実際の歌声）は**リポジトリに入れていない**（第三者の収録を含むため）。解析モデルの重み（RMVPE・HubertFA）も同梱しない（既定の F0 の方式の Gliss の F0 モデルはエンジンに入っているので、F0 の方式のテストは重み無しで回る）。
どちらも無いときは、それが要るテストは skip になる。

- エンジン: 素材も重みも無い状態で 130 件ほどが通り、残りは skip（2026-10 時点）。
- 画面: 素材を開くテストは、素材か重みが無いとファイルごと skip（`app/tests/materials.js` の `skipUnlessReady`）。
  重みが無いまま素材を開くと、画面が初回の「モデルの準備」で止まり、解析を待つテストが時間切れになるため。
  素材の要らないテスト（初回の画面・タイトルバー・更新の表示・忙しい表示など）はエンジンの `.venv` だけで回る。
- skip されるテストは、期待が古くなっても CI では落ちない（2026-10-03、`test_export.py` のツールの数と `commands.spec.js` の K1 が素材のあるときだけ落ちた）。
  MCP のツール・画面のコマンドを足したら、下の「手元の素材で全部を回す」で確かめる。

重みは画面の初回の「モデルの準備」で `%LOCALAPPDATA%\Gliss\models` に取得するか、`VOCAL_ENGINE_MODELS_DIR` で置き場を指す。

## 手元の素材で全部を回す

自分で録った（または権利を持っている）歌声を、環境変数 `GLISS_TEST_MATERIALS` で指したフォルダに置く。

```
<素材のフォルダ>\
  materials.json            素材の一覧と、素材に結び付いたデータ（歌詞など）
  *.wav                     materials.json の "clips" に書いたファイル
  regression-baseline.json  再合成の回帰値（任意。engine/tests/regression_measure.py が作る）
  praat-reference\          評価の段階の Praat の書き出し（任意。C_pitch+3.wav・A_pitch-2.wav）
```

```powershell
$env:GLISS_TEST_MATERIALS = 'C:\path\to\materials'
$env:VOCAL_ENGINE_MODELS_DIR = 'C:\path\to\models'    # 既定の %LOCALAPPDATA%\Gliss\models に入っていれば不要
cd engine; ..\.venv\Scripts\python.exe -m pytest -q
cd ..\app; pnpm test
```

テストのコードには素材のファイル名・歌詞を書かない。テストは記号（`C` など）とデータのキーで素材を引く
（`engine/tests/materials.py`・`app/tests/materials.js`）。

既定の F0 の方式は Gliss の F0 モデル。ノートの ID（`n007` など）・数・区切りの位置を RMVPE の解析の結果で書いた素材のテスト
（編集・接続・ガイド・書き出し・音素）は、F0 を RMVPE で解析させて回す（エンジンは `conftest.py` の `rmvpe_f0` を
`pytestmark` で使う、画面は起動の env に `materials.js` の `RMVPE_ENV` を足す）。どれも前から RMVPE の重みが無いと skip になるテスト。
同じ素材でも Gliss の F0 モデルでは区切りが変わる（有声と判定する割合が数ポイント低く、短いノートが 1 割ほど少ない）。
新しく書く素材のテストは、ID ではなく時刻や種類でノートを引くと、方式に依らずに回る。

### materials.json

```json
{
  "clips": { "A": "a.wav", "C": "c.wav", "C2": "c2.wav", "SONG": "D:\\music\\song.wav" },
  "data": {
    "C.part1": "前半のフレーズの歌詞",
    "C.part2": "後半のフレーズの歌詞",
    "C.lyrics": "前半のフレーズの歌詞 後半のフレーズの歌詞"
  }
}
```

`clips` の値は素材のフォルダからの相対パスか絶対パス。

**素材の記号**（テストが前提にしている性質）:

| 記号 | 性質 |
|---|---|
| `C` | テイク（約 4 秒、48 kHz・モノラル）。無音で 2 つのフレーズに割れ（0.30〜1.60 秒と 2.20〜3.52 秒）、間に息がある。多くのテストの既定の素材 |
| `C2` | `C` と同じ歌詞の別テイク（ガイドに使う。数半音高い） |
| `A` | 音程ノートの間に無声の子音が挟まるフレーズ（約 5 秒） |
| `B` | `A` と同じ歌詞の別テイク |
| `D` | 漢字混じりの歌詞のフレーズ（約 4 秒） |
| `E` | 短い音節を繰り返す叫び（約 4 秒。音節の頭で F0 が約 1 オクターブ跳ぶ。0.83 秒付近に境目） |
| `G` | `C` と別の歌（ガイドとの対応が取れない例） |
| `H` | 短いせりふ（約 2 秒） |
| `W`・`W0` | 囁き（ノイズ除去の後・前） |
| `SONG` | 曲全体（約 158 秒。最後の 4 フレーズだけ歌う） |
| `S3_*` | 別の曲 S3（約 165 秒・48 kHz）。テイクとガイドを**同じ DAW の時間軸**で書き出したもの（`engine/tests/test_guide_align_song.py`・[guide-coverage.md](guide-coverage.md) の「同じ時間軸の素材」）。テイク: `S3_M1`・`S3_M2`（主旋律。曲の一部だけ歌う）・`S3_RS`（主旋律の録り直し。生の録音で 1.2 秒長い）・`S3_HU`・`S3_HL`（上・下のハモリ）・`S3_A1`・`S3_A2`（息の多いパートの録り直し）・`S3_AS`（息の多いパート）。ガイド: `S3_GM`（主旋律）・`S3_GA`（息の多いパート。音程の取れない区間がある）・`S3_GU`・`S3_GL`（ハモリ）。`S3_MID`: 主旋律・ハモリ・オクターブ違いの 5 トラックの MIDI（テンポのメタイベント無し、Intro と間奏のラップは無い） |

**データのキー**:

| キー | 中身 |
|---|---|
| `C.part1` / `C.part2` | `C` の前半・後半のフレーズの歌詞（かな） |
| `C.lyrics` | `C.part1` と `C.part2` を空白でつないだもの |
| `C.lyrics_marked` | `C.lyrics` のフレーズの終わりに「！」を付けたもの |
| `C.part1_phonemes` / `C.part2_phonemes` | 前半・後半の音素の並び（空白区切り。例 `s a k u r a`） |
| `C.phonemes` / `C.syllables` | `C.lyrics` の音素の数・音節の数 |
| `C.first_syllables` | 最初の 4 音節のかなの配列 |
| `C.kana` | 聞き取り（Whisper）の正解のかな（空白なし） |
| `C.lyrics_tsu` / `C.part1_tsu` / `C.part2_tsu` | 歌詞に促音「っ」を足したもの（issue #58。前半・後半は推定の読みとして使う） |
| `E.lyrics` | `E` の歌詞 |
| `SONG.duration_sec` | `SONG` の長さ（秒） |
| `SONG.lyrics` | `SONG` の 4 区間の歌詞（`[{"start_sec", "end_sec", "text"}]`。2 番目と 4 番目の区間を編集する） |
| `SONG.kana` | アラインの結果のかなに含まれるはずの文字列の配列 |
| `S3.midi_bpm` / `S3.midi_start_sec` | `S3_MID` を読むテンポと、譜面の 0 拍のタイムライン上の秒（正解を作るときにずらす量） |
| `S3.midi_track` | ガイドの記号 → そのガイドに当たる `S3_MID` のトラックの番号 |
| `S3.pairs` | 評価の組 `[名前, テイク, ガイド]` の配列 |
| `S3.expect` | テストの期待値（組ごとの DTW の外れの上限・被覆率の下限・前の値） |

回帰値（`regression-baseline.json`）は素材が変われば変わるので、素材を差し替えたら作り直す:
`python engine/tests/regression_measure.py`（素材と重みが要る）。

## テストの決まりごと

- **テストは音を一切出さない**: `app/playwright.config.js` が `VOCAL_EDITOR_MUTE=1` を入れ、各テストも `--mute` で起動する
  （Chromium の `--mute-audio`・`setAudioMuted`・出力の音量 0 の三重）。プレビュー音のテストは「鳴らそうとしたもの
  （ノート・区間・高さ）」の記録で確かめる。
- **テストのウィンドウは画面に出ず、前面も奪わない**: `VOCAL_EDITOR_HIDDEN=1` で、ウィンドウを透明（opacity 0）のまま
  `showInactive` で出す（タスクバーにも出さず、人のマウスは素通し）。PC を操作しながら回せる。
- テストが撮るスクリーンショットは `app/screenshots/`（git 管理外。素材が写るので入れない）。
- エンジンのテストは、利用者の `%APPDATA%\Gliss\bridge.json` や `%LOCALAPPDATA%\Gliss\work` に触れない
  （`engine/tests/conftest.py`）。

## git worktree で回す

worktree には `.venv` が無い。worktree の直下に、本体の `.venv` の python と worktree の `engine` を指す `.mcp.json`
（git 管理外。例は `engine/.mcp.json.example`）を置けば、画面のテストは worktree のエンジンを起動する。
エンジンのテストは本体の `.venv` の python で `engine` から回す（`python -m pytest` は作業ディレクトリのパッケージを読む）。
重みは `%LOCALAPPDATA%\Gliss\models` を共有するか、`VOCAL_ENGINE_MODELS_DIR` で指す。
