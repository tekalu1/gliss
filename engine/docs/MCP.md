# Gliss の MCP サーバー

Gliss（歌声のピッチ・タイミング編集ツール）の Python エンジンを MCP（stdio）で公開する。サーバーの名前は `gliss`（Python のパッケージは内部名の `vocal_engine` のまま）。
Claude Code などの MCP クライアントから
「測る／直す／確かめる」ができる。ツールは 69 個（うちトラック（複数トラックのセッション）・テンポの 9 個は §3-2、
プロジェクトのファイル（新規・開く・保存）の 5 個は §3-3、DAW（ARA プラグイン）専用の 8 個は §3-4、区間の聞き取り（音声認識）の 3 個は §1-1）。

起動:

```
<repo>\.venv\Scripts\python.exe -m vocal_engine.mcp      （cwd は <repo>\engine。<repo> はこのリポジトリの場所）
```

## 0. 使い方の流れ

```
load_project()                                                 ← Gliss の画面で今開いている曲を開く（§4-1）
load_project(path.gliss) / new_project(take_path?)             ← プロジェクトを開く・作る（§3-3。issue #33）
open_project(take_path, guide_path?, lyrics?, guide_lyrics?)   ← テイクの WAV から開く（旧形式の projects/…）
  └ analyze_take()                        ← F0 → 音符 → 推定の読み → 音素
     ├ list_utterances()                  ← 発声区間・ノート・推定の読み・確信度
     ├ get_lyrics()                       ← 現在の歌詞と推定/確定
     ├ set_note_syllable(note_id,kana)    ← 1 音節を修正
     ├ set_lyrics(entries=[{start_sec,end_sec,text},…]) ← 歌詞を確定して修正
     ├ inspect_lyrics_score(path.svp) / import_lyrics(path.svp, dry_run?) ← 譜面の歌詞を録音へ対応付ける
     ├ transcribe(start_sec, end_sec)     ← 区間を聞き取って歌詞の候補（確定しない。§1-1）
     ├ list_notes() / get_pitch(range)    ← 測る
     ├ get_phonemes(range?)               ← 音素と境界（歌詞があるときだけ）
     ├ list_deviations()                  ← ガイドとのずれ（ガイドがあるときだけ）
     ├ shift_pitch / set_pitch_curve(mode=offset|draw)      ← 直す（ピッチ。draw = 鉛筆）
     ├ set_transition                                       ← ノートの変わり目のなだらかさ
     ├ split_note / merge_notes                             ← ノートを分ける / つなぐ
     ├ mute_notes                                           ← ノートを無音にする（長さは変えない）
     ├ set_fade                                             ← ノートのフェードイン／アウト（音量だけ）
     ├ move_boundary / stretch / move_note                  ← 直す（タイミング。後ろはずらさない）
     ├ list_connections / set_connection                    ← 隣との接続 / 切り離し（となだらかさ）
     ├ correct_to_guide(pitch_strength, timing_strength, match_pitch_shape) ← ガイドへ寄せる
     ├ plan_edit → apply_plan                               ← 計画を作って確定（画面が使う）
     └ render_preview / render_region / render_audition / render_view / remeasure / list_changes   ← 確かめる
          └ undo() / redo()               ← 曲で 1 本の履歴を戻す（画面の Ctrl+Z と同じ。§2-9）
```

**歌詞を与えると何が変わるか**（段階2）:

- `get_phonemes` が音素と境界を返す（画面の歌詞・音素レーンに文字が出る）。
- タイミング編集の単位が**ノートから音素境界**になる（`move_boundary`）。
- タイミングの編集（`stretch` / `move_note` / `correct_to_guide`）が**母音だけを伸縮し、子音の長さを保つ**ようになる。
- `list_notes` の各ノートに、重なる音素（`phonemes`）と読み（`text`）が付く。

### 自動分析から AI が直す

`analyze_take()` は歌詞を渡さなくても発声区間ごとにかなを推定し、音素を付ける。推定は誤りが多いため、`origin: "estimated"` の間は確認対象とする。1音節を直すと `confirmed_syllables` に区間内の番号が入り、未修正の音節は推定のまま残る。区間全体、または全音節が修正されると `origin: "confirmed"` になる。修正済みの歌詞は再解析しても上書きしない。推定値の精度は [評価](../../docs/lyrics-auto.md) を参照。

推定の読みは自動分割のノートにだけ適用する。手動の分割・結合・タイミング変更を受けたノートでは推定音節を画面・`list_notes`・`get_phonemes` から外し、推定結果自体は `get_lyrics` に残す。これらの変更を `undo` すると表示も戻る。「ガイドに合わせる」のタイミングは推定音節の区間でノート単位、確定音節の区間で音素単位。1 ノートに両方がある場合は確定音節の頭を優先し、推定音節に独立した補正点を置かない。

```text
open_project(take_path="...")
analyze_take()
list_utterances()             # 時刻、note_ids、estimated_reading、current_lyrics、confidence
get_lyrics()                  # 区間ごとの現在の歌詞
set_note_syllable(note_id="n003", kana="か", syllable_index=4)
get_phonemes()                # 修正後の音素と境界
undo()                        # 修正を戻す
```

`set_note_syllable` の `kana` は1音節（「しゃ」「ん」など）。1ノートに複数音節が重なる場合は `list_utterances().utterances[].syllables[].index` を `syllable_index` に渡す。同じ一覧の `syllables[].confirmed` で各音節の確認状態が分かる。区間全体を直すときは `set_lyrics(text, start_sec, end_sec)` を使う。`get_lyrics` は現在の区間と、修正前の `estimate.reading`・`estimate.confidence` を返す。漢字混じりの歌詞で1音節だけ直した場合、元の `text` を保ち、アラインメント用の `reading` を更新する。

推定値は初回解析のベースとして保存され、通常の編集履歴には入らない。修正・削除は `undo` で戻せる。`.gliss` を開いてから自動推定された歌詞は未保存の変更になるので、残す場合は `save_project` を呼ぶ。

守っていること（MCP クライアントの制約）:

- **結果に画像を入れない。** `render_view` は PNG を書いて**パスだけ**返す。クライアントは画像を読む
  ツール（Claude Code なら `Read`）でそのパスを開く。PNG は毎回ユニークなファイル名にしている。
- **生の数値列を返さない。** F0 は要約統計。配列が要るときは `get_pitch(write_npy=true)` で
  NPY のパスを返す。
- **30 秒以内に返す。** 超えそうな処理（長い素材の `analyze_take` / `render_preview` /
  `remeasure` / `export_wav`）は `{"status":"running","job_id":...}` を返すので
  `get_job(job_id)` で取りに行く。**`get_job` と `engine_info`・`prep_status` はエンジンのロックを取らない**
  （取るとジョブが終わるまで進捗を見に行った側が止まり、ジョブにした意味が無くなる）。
- **ログは `<project>/engine.log`。** stdio の stderr はクライアントに捨てられるので、エンジンが自分で書く。
- **`notifications/initialized` が来なくても動く。** mcp 2.x のサーバーは `initialize` を
  受け取った時点で受付を開く（`mcp/server/connection.py` の `initialize_accepted`）。
- **エラーは例外ではなく JSON。** `{"ok": false, "error": "...", "tool": "...", "log": "..."}` を返す。

## 1. 測る

### `open_project(take_path, guide_path?, project_dir?, reuse?, lyrics?, guide_lyrics?, offset_sec?, length_sec?, source_id?, guide_offset_sec?, guide_length_sec?)`

| 引数 | 型 | 既定 | 意味 |
|---|---|---|---|
| `take_path` | string | 必須 | 編集したい音声（WAV）の絶対パス |
| `guide_path` | string | null | ガイドボーカル。あると `list_deviations` / `correct_to_guide` が使える |
| `project_dir` | string | null | 省略時は `<repo>/projects/<名前>-<sha8>/`（配布版のエンジン exe は `%LOCALAPPDATA%\Gliss\projects`） |
| `reuse` | bool | true | 同じ素材なら既存プロジェクト（編集リスト）を開き直す |
| `lyrics` | string | null | テイクの歌詞。**かな・カナ・漢字混じりのどれでもよい**（段階2） |
| `guide_lyrics` | string | null | ガイドの歌詞。**あるとタイミング補正の精度が上がる**（§1 の `set_lyrics`） |
| `offset_sec` / `length_sec` | number | null | **ファイルの途中から切り出したクリップ**を編集する（ソース内の開始秒と長さ。省略時はファイル全体）。プロジェクトの秒は**クリップの頭が 0**（DAW 連携 段階 0。`docs/daw-stage0.md`） |
| `source_id` | string | null | ソースの ID（DAW のオーディオソースの ID など）。省略時は `sha256:<中身の SHA-256 の頭 16 桁>` |
| `guide_offset_sec` / `guide_length_sec` | number | null | ガイドもファイルの一部なら同じように |

返り値: `{ok, project_dir, log, take:{path,sha256,sr,channels,frames,duration_sec,subtype,source_id,source_kind,source_name,source_frames,source_duration_sec,offset_frames,offset_sec}, guide, edits, changesets, analyzed, session, next}`
（`session` はトラックの一覧。§3-2。`session.last_current` は開く前に最後に選ばれていたトラック。`guide_note` はガイドが付かなかった理由。`open_project` はテイクのプロジェクトのディレクトリに**セッション**を開き、
テイクのトラックを編集対象にする。`guide_path` を渡すとそのファイルのトラックをガイドに指定する。省くとセッションのガイドのまま）
（`frames` / `duration_sec` はクリップの長さ。`source_*` はソース全体、`offset_*` がソース内の開始位置）

```json
{"ok": true, "project_dir": "C:\\…\\projects\\take-1a2b3c4d",
 "log": "...\\engine.log",
 "take": {"path": "...\\take.wav", "sr": 48000, "channels": 1,
          "duration_sec": 3.84, "sha256": "..."},
 "edits": 0, "analyzed": false}
```

### `set_lyrics(text?, source?, start_sec?, end_sec?, entries?, from_text?, mode?, reanalyze?, author?)`

歌詞を**区間ごと**に与える。**音素アラインメントの入力**になる。**取り消せる**（`undo` で前の歌詞に戻る。§2-9）。

曲全体のテイク（例: 158 秒あって歌うのはアウトロの 4 フレーズだけ）を想定しているので、
歌詞は `{start_sec, end_sec, text}` の**配列**で持つ。アラインメントは**区間ごとに**掛かり、
**区間の外には音素を付けない**（無音・息・歌詞の無い発声として扱う）。

| 呼び方 | 動き |
|---|---|
| `set_lyrics(entries=[{start_sec, end_sec, text}, ...])` | **配列をまるごと置き換える**（推奨） |
| `set_lyrics(text, start_sec, end_sec)` | その区間に 1 件入れる（重なる区間は消える） |
| `set_lyrics("", start_sec, end_sec)` | その区間の歌詞を**消す** |
| `set_lyrics(text)` | 範囲なし = 素材全体の歌詞 1 件（段階2 までと同じ） |
| `set_lyrics("")` | 全部消す |
| `set_lyrics(from_text=...)` | 歌詞テキストの中身をそのまま渡す（1 行 = `開始 終了 歌詞`） |

| 引数 | 型 | 既定 | 意味 |
|---|---|---|---|
| `text` | string | null | 1 区間ぶんの歌詞。**かな・カナ・漢字混じりのどれでもよい** |
| `entries` | array | null | 区間の配列。`[{"start_sec":149.19,"end_sec":150.56,"text":"さくら…"}]` |
| `start_sec` / `end_sec` | float | null | `text` を入れる区間（両方そろえる。50 ms 以上） |
| `from_text` | string | null | 歌詞ファイルの中身。1 行 = `開始 終了 歌詞`（`2:24.30` 形式も可）。`#` と空行は飛ばす |
| `source` | string | `"take"` | `"take"` か `"guide"`。**ガイドにも入れるとタイミング補正が直接アラインになる** |
| `mode` | string | `"replace"` | `"replace"`（重なる区間を消してから入れる）/ `"add"`（重なりを許さず足す） |
| `reanalyze` | bool | true | すぐアラインし直す。false なら次の `analyze_take` に任せる |

歌詞の書き方（どれでも通る）:

```
set_lyrics("さくらさくら やよいのそらは")                ← かな（素材全体）
set_lyrics("サクラサクラ！", 149.19, 150.56)              ← カナ・記号つき（区間）
set_lyrics("青い空は晴れていますか〜！")                  ← 漢字混じり（pyopenjtalk-plus の読み）
set_lyrics("トマー！")                                    ← 長音は直前の母音に吸収して t o m a
```

- 記号・空白は無音（`SP`）の手がかりとして使い、音素には出ない。
- **長音（`ー 〜 ～ ~ -`）は直前の母音に吸収する。** 辞書どおり `トマー → t o m a a` と
  書くと、HubertFA も SOFA も 2 つ目の母音を 8〜10 ms に潰す（段階2 の評価で実測）。
  画面のかなは `まー` のまま残る。
- 促音 `っ` は `cl`（閉鎖＝無音区間）、撥音 `ん` は `N`。
- **区間は重ねられない。** 「区間つきの歌詞」と「素材全体の歌詞」も混ぜられない。
- 切り出しは前後 **200 ms** 広げてからアラインする（頭の子音・語尾の解放が切れないように。
  隣の区間には食い込まない）。区間と区間の間は `SP` で埋まる。

返り値: `{ok, source, entries[], n_entries, lyrics, kana, phonemes, boundaries, confidence,
warnings[], aligner, rtf, elapsed_sec, aligned_sec, per_entry[], next}`

```json
{"ok": true, "source": "take", "n_entries": 4,
 "entries": [{"start_sec": 144.3, "end_sec": 148.77, "text": "やよいのそらは…"},
             {"start_sec": 149.19, "end_sec": 150.56, "text": "さくらさくら"}],
 "phonemes": 95, "boundaries": 104, "confidence": 0.70,
 "aligner": "hubertfa", "rtf": 0.026, "elapsed_sec": 0.25, "aligned_sec": 9.6,
 "per_entry": [{"index": 1, "start_sec": 149.19, "end_sec": 150.56,
                "kana": "サクラサクラ", "phonemes": 12}]}
```

> **アラインするのは歌詞を付けた区間だけ。** 158 秒のテイクでも、4 区間 9.6 秒ぶんを
> 1.2 秒で処理する（区間の外の 148 秒は触らない）。

### `inspect_lyrics_score(path)` / `import_lyrics(path, track?, dry_run?, song_start_sec?, reanalyze?, author?)`

`.svp` または歌詞付き `.mid` / `.midi` を読む。`inspect_lyrics_score` はプロジェクトを開かずに、形式・テンポ・拍子とトラックごとの音符数・歌詞付き音符数を返す。複数の歌詞付きトラックがあれば `import_lyrics` の `track` に 0 始まりの番号または名前を指定する。

`import_lyrics` はテンポ変更を含め譜面の時刻を秒に直し、録音 WAV の bext TimeReference と音高列で曲上の原点を検算する。譜面のフレーズと録音の発声区間は `list_utterances` と同じ区切りで対応付け、発音の頭で時刻を微修正する。対応を確認できた部分だけ `set_lyrics(entries=…)` と同じ経路で **`origin: confirmed`** の歌詞として登録し、推定歌詞も全件置き換える。`undo` で戻せる。`dry_run=true` は歌詞を変えず、`entries`・`matched`（対応した `utterance_index`・`note_ids` を含む）・`unmatched`（保留した譜面の区間と理由）を返す。`song_start_sec` は録音ファイル頭の曲上の秒を手動指定するときに使う。`reanalyze` の既定は true。

```text
inspect_lyrics_score(path="D:/music/song.svp")
import_lyrics(path="D:/music/song.svp", dry_run=true)
import_lyrics(path="D:/music/song.svp")
import_lyrics(path="D:/music/lyrics.mid", track=1)
```

一致の基準、録音の bext が誤っていた実例、独立した発音検出による精度は [`docs/import-svp.md`](../../docs/import-svp.md)。

### 1-1. 区間の聞き取り（音声認識。issue #54）: `transcribe` / `asr_status` / `prepare_asr_model`

歌っている言葉を耳の代わりに確かめる。**結果は候補で、確定の歌詞は書き換えない**。取り込むのは AI（`set_lyrics` / `set_note_syllable`）か人（画面の Enter）。精度の目安（issue #54 の評価）: Whisper large-v3 で歌のかな誤り率 16.5%、叫びは約 67% と弱い。

| ツール | 何をするか |
|---|---|
| `asr_status(model?, probe?)` | 使えるか（`available`。faster-whisper が無ければ false と `reason`）・モデル（`model.size_text`・`license`・`path`＝保存先・`source_url`）・入っているか（`installed`）。`probe=true` で GPU を使えるか（`device: cuda / cpu`、`device_note`＝CPU になる理由） |
| `prepare_asr_model(model?, background?)` | モデルをダウンロードする（初回だけ。Whisper large-v3 は 3.09 GB）。取り消せるジョブ。版を固定し、各ファイルの大きさと SHA-256 を確かめる。**利用者に大きさ・保存先・ライセンスを見せてから呼ぶ**。AI のプロセスからは画面の「AI に許可: 保存・書き出し」が要る（§4-1） |
| `transcribe(start_sec, end_sec, source?, model?, background?)` | 区間（編集前の秒。120 秒まで）を聞き取り、候補を返す |

`transcribe` の返り値:

| キー | 中身 |
|---|---|
| `text` / `lyrics` | 聞き取った文字（漢字混じり）／`set_lyrics` にそのまま渡せる形（空白・文末の句点を落とす） |
| `kana` | かなの読み（`set_lyrics` のアラインメントと同じ g2p） |
| `words` | 語ごとの `start_sec` / `end_sec`（プロジェクトの秒）/ `probability` |
| `confidence` | 語の確率の平均と最小。`calibrated: false`（モデル内部の値で、正解率ではない） |
| `warnings` | 要確認の理由（同じ音の繰り返し＝叫び、無音らしい所の認識＝幻覚のおそれ、確信の低い語、CPU で処理した など） |
| `current` | その区間に今ある歌詞（確定・推定）と候補の読みの違い（`char_errors`・`char_error_rate`・`same`） |
| `device` / `elapsed_sec` / `model` | `cuda` か `cpu`（CPU は GPU の約 26 倍遅い）・掛かった秒・モデル |

- **重みは同梱しない。勝手にダウンロードしない。** 入っていなければ `transcribe` は `{"ok": false, "code": "model_missing", "model": {...}}` を返す。faster-whisper が無ければ `code: "unavailable"`（`uv pip install faster-whisper`。engine の extras `asr`）。
- 置き場は `%LOCALAPPDATA%\Gliss\models\asr\<model>`（`GLISS_ASR_MODELS_DIR` で変えられる）。
- GPU（CUDA）は FP16、CPU は INT8。Windows の GPU には cuBLAS（CUDA 12）が要る（extras `asr-cuda` = `nvidia-cublas-cu12`）。無ければ CPU に落として `warnings` と `device_note` で知らせる。
- 読み込みが要る・CPU・20 秒を超える区間はジョブ（`get_job` / `cancel_job`）。読み込み済みの GPU で短い区間ならその場で返す。ジョブはエンジンのロックを握らないので、聞き取っている間も他のツールは動く。

**手順の例: 聞き取り → 手元の歌詞・SVP と突き合わせ → 確定**

```text
list_utterances()                                  # 発声区間（start_sec / end_sec）と今の歌詞・推定
asr_status()                                       # available / installed。入っていなければ利用者に大きさとライセンスを伝える
prepare_asr_model()  → get_job(job_id)             # 利用者が了承したら 1 回だけ（3.09 GB）
transcribe(start_sec=12.30, end_sec=15.84)         # → get_job(job_id)（初回はモデルの読み込みでジョブになる）
  # text「桜桜弥生の空わ」 kana「さくらさくらやよいのそらわ」
  # current.kana「さくらさくらやよいのそらは」 char_errors 1 → 違う所を見る
import_lyrics(path="song.svp", dry_run=true)       # 手元の SVP の該当区間と比べる（歌詞は変えない）
set_lyrics("さくらさくらやよいのそらは", start_sec=12.30, end_sec=15.84)   # 確かめた歌詞を確定（undo 可）
set_note_syllable(note_id="n012", kana="ま", syllable_index=3)   # 1 音節だけ直すとき
```

候補と手元の歌詞が食い違ったら、**どちらが正しいかを決めるのは人**。候補の `warnings` に叫び・幻覚・確信の低い語が出ている区間は特に、利用者に聞いて確かめてから確定する。候補をそのまま確定したいときも `set_lyrics(text=result.lyrics, start_sec, end_sec)` を明示的に呼ぶ。

### `analyze_take(force?, estimator?, confidence_sweep?, background?)`

F0（10 ms ホップ）→ 音符のかたまり →（ガイドがあれば）DTW →（歌詞があれば）音素。
**DTW の既定は MFCC + librosa**（段階2 で chroma から切り替えた。C↔C2 で境界のずれの
中央値が 26.8 → 10.9 ms）。結果は `<project>/cache/` に保存し、
2 回目以降は読み直すだけ。

`estimator`（F0 の方式）を省くと、選んでいる方式（画面の 編集 > ピッチ検出の方式・`set_f0_estimator`）で解析する。

| `estimator` | 中身 |
|---|---|
| `rmvpe` | 既定・正。RMVPE（ONNX）。重みは同梱せず、利用者が取得する（`%LOCALAPPDATA%\Gliss\models\rmvpe.onnx`） |
| `gliss` | Gliss の F0 モデル（試作）。条件のはっきりした学習データだけで学習した小さなモデル（SwiftF0 と同じ構造、135 KB。エンジンに同梱）。16 kHz・16 ms ごとの出力を 10 ms の格子に載せ、確信度 0.5 以上を有声にする |
| `praat` | Praat（parselmouth）の自己相関法を歌声向けに調整したもの（octave cost 0.1、有声は strength のヒステリシス）。重みは要らない |
| `fcpe` | 代替（torch。開発版だけ） |
| `auto` | `rmvpe` → 落ちたら `gliss` |

- どの方式も、最後の有声の判定は同じ（方式が F0 を出している かつ フレームの RMS が −55 dBFS より大きい）。
  `rmvpe` の `confidence` は 0/1 の代用値、`gliss`・`praat` は 0..1 の確度。
- **選んでいる方式が `rmvpe` で重みが無いときは `gliss` で解析する**（`engine_info().f0_estimator_effective`）。
  `estimator="rmvpe"` と名前を指定したときは、重みが無ければエラー。
- 保存した解析（`cache/take-analysis.json`）が別の方式・別の版のものなら、テイクの F0 から解析し直す。
  ガイドの解析と対応付けは方式ごとの鍵付きの保存（`cache/guide/`）なので、方式を戻したときは読むだけで済む。
- 方式を替えると音符の切れ目が変わることがある。編集済みのテイクで替えると、ノートに付けた編集の当たり方が変わりうる。

```json
{"ok": true,
 "f0": {"n_frames": 385, "n_voiced": 241, "voiced_ratio": 0.626, "median_hz": 349.2,
        "median_note": "F4", "iqr_semitones": 4.2, "estimator": "rmvpe", "hop_ms": 10.0},
 "notes": {"total": 17, "pitched": 14, "labels": {"silence": 2, "sung": 6, "short": 8}},
 "guide": {"path": "...", "notes": 15},
 "alignment": {"n_points": 191, "offset_median_ms": 0.0, "offset_max_abs_ms": 120.0},
 "next": "list_deviations でずれの一覧を見る"}
```

ガイドとの対応付け（DTW）は、ほぼ無音の区間（MFCC の下限より静か。部分的に録り直したテイク・ガイドの歌っていない所）が
あっても落ちない（issue #32。前は `DTW cost matrix C has NaN values` で解析全体が失敗し、ガイドが重ならなかった）。
それでも対応付けが失敗したときは、解析全体は落とさず**タイムライン上の位置のままの対応**で続け、返り値に
`guide_warning`（理由）、`alignment.dtw.stage = "fallback"` を入れる（キャッシュには書かず、次の解析で取り直す）。

**裏の準備に合流する**（issue #63。§3-2 の「裏の準備」）: セッションのトラックで、そのトラックの準備がまだ済んで
いなければ、準備を最優先にして終わりを待ち、キャッシュを読むだけの解析をして返す（最初からやり直さない。計算は 1 回）。

- ジョブのとき（`background=true`・長い素材）は**待つ間エンジンのロックを握らない**（ほかのツールは動く）。
  `get_job` の `joined: true`、`stage`（`take_f0` / `lyrics` / `guide_f0` / `alignment` / `onsets` / `phonemes` /
  `commit` / `view` / `load`）と `stage_label`（「テイクの音程」「歌詞の推定」「ガイドの音程」「ガイドとの対応付け」「発音の頭」
  「音素」「書き込み」「描画データ」「読み込み」）、`progress` が準備の進み具合。
- 合流したジョブを `cancel_job` すると待つのをやめる（`canceled`）。**裏の準備そのものは続く**。

**キャッシュを読むだけで済むときは、`background=true` でもジョブにせずその場で返す**（issue #63 の 3。準備済みの
トラック・一度組んだことのあるガイド）。見るのは、テイクの解析・歌詞の推定（済みか推定できない）・ガイドの解析と
対応付けの鍵付きの保存・発音の頭・今の歌詞の音素。合流もしない。解析の要約（時刻を除く）と歌詞が変わらなければ
`project.json` を書かない。
- 同期のとき（短い素材の既定・`background=false`）は、準備を最大 15 秒待つ。終わらなければ
  `{ok:false, preparing:true, error:"準備中…"}` を返す。準備は続くので後で再試行できる。
- `force=true`・`estimator` が選んでいる方式以外・`confidence_sweep=true` のときは合流せず、今までどおりその場で計算する
  （その間、裏の準備はそのトラックに触らない）。
- 準備が失敗していたら、もう一度準備してから、それでも駄目ならその場で解析する（返り値は今までどおりの成功か失敗）。

### `get_pitch(start_sec?, end_sec?, source?, write_npy?)`

範囲の F0 の**要約統計**。`source` は `take` / `guide`。`write_npy: true` で
`(3, n_frames) = [f0_hz, confidence, voiced]` の NPY を書いてパスを返す。

### `list_notes(start_sec?, end_sec?, kind?, limit?)`

`kind`: `note`（既定。音程のあるもの）/ `all` / `unvoiced` / `breath` / `silence`。

```json
{"ok": true, "total": 14, "returned": 14,
 "notes": [{"id": "n004", "start_sec": 0.54, "end_sec": 0.83, "duration_sec": 0.29,
            "kind": "note", "label": "sung", "note": "F4", "pitch_hz": 349.6,
            "iqr_semitones": 1.2, "rms_peak_db": -12.4, "text": null, "confidence": 0.74}]}
```

**歌詞があると各ノートに `phonemes`（重なる音素）と `text`（読み）が付く**（段階2）。
音符の境目と音素の境目は一致しないので、**重なりで持たせている**。

```json
{"phonemes_attached": true,
 "notes": [{"id": "n004", "text": "らぽ",
            "phonemes": [{"id": "ph006", "text": "r", "kana": "ら", "label": "consonant",
                          "start_sec": 0.607, "end_sec": 0.618},
                         {"id": "ph007", "text": "a", "kana": "ら", "label": "vowel",
                          "start_sec": 0.618, "end_sec": 0.816}]}]}
```

### `list_deviations(threshold_cents?, threshold_ms?, start_sec?, end_sec?, limit?)`

ガイドとのずれ。`pitch_cents` は **+ がテイクの方が高い**、`timing_ms` は **+ がテイクの方が遅い**
（`timing_detrend_ms` は引いてある基準のずれ。ふだんは 0 = タイムライン上のガイドの位置と比べる。
全体のずれが 150 ms を超える置き場所の違う素材だけ、その全体のずれ。issue #61）。

```json
{"ok": true, "total": 14, "timing_detrend_ms": 5.0,
 "deviations": [{"note_id": "n006", "start_sec": 0.97, "end_sec": 1.55,
                 "guide_note_id": "g007", "take_note": "F4", "guide_note": "A#4",
                 "pitch_cents": -499.2, "timing_ms": 225.0, "confidence": 0.84,
                 "reason": null}]}
```

**歌詞があると `timing`（音素境界ごとのずれ）が付く**（段階2）。
`correct_to_guide` のタイミングが見ているのは発音の頭どうしの組（`plan_edit(op="guide")` の JSON の `timing`。
`docs/guide-timing.md`）。ここの timing_ms はノートの頭と、ガイドのノートの頭を画面に描く位置に置いたものとの差。

```json
{"timing": {"unit": "音素境界", "source": "guide_phonemes", "matched": 35, "total": 35,
            "timing_detrend_ms": -0.9, "abs_median_ms": 10.9, "abs_max_ms": 92.6,
            "over_threshold": 8,
            "boundaries": [{"boundary_id": "b021", "index": 21, "take_sec": 2.196,
                            "kind": "consonant_vowel", "before_text": "t", "after_text": "o",
                            "guide_sec": 2.288, "target_take_sec": 2.289,
                            "timing_ms": -92.6, "confidence": 0.71}]}}
```

### `get_phonemes(start_sec?, end_sec?, source?, limit?)`

音素と境界。歌詞が無いときは `{"supported": true, "phonemes": [], "reason": "…set_lyrics…"}`。

返り値の中身:

| キー | 中身 |
|---|---|
| `phonemes` | 音素。**時間範囲＋テキスト（音素と対応するかな）＋ラベル＋確信度** |
| `syllables` | かな 1 音節 = 1 ブロック（画面の歌詞レーン） |
| `boundaries` | **タイミング編集の制御点**。`move_boundary` の引数はこの `id` |
| `warnings` | 長すぎる母音（制御点が無い）・短すぎる音素・塊の数が合わないなど |
| `aligner` / `g2p` | アライナー名・RTF・§7.6 の検算、かなと `.lab` |

```json
{"ok": true, "supported": true, "lyrics": "さくらさくら！ やよいのそらは！",
 "kana": "サクラサクラ! ヤヨイノソラハ!", "confidence": 0.748, "total": 33,
 "phonemes": [
   {"id": "ph001", "index": 1, "start_sec": 0.309, "end_sec": 0.32, "duration_sec": 0.011,
    "text": "s", "kana": "さ", "romaji": "sa",
    "label": "consonant", "detail": "consonant_unvoiced",
    "confidence": 0.82, "stretchable": false, "syllable_index": 0, "flags": []}],
 "syllables": [{"index": 0, "kana": "さ", "romaji": "sa",
                "start_sec": 0.309, "end_sec": 0.368, "phoneme_indices": [1, 2]}],
 "boundaries": [
   {"id": "b002", "index": 2, "time_sec": 0.32, "edited_sec": 0.32,
    "kind": "consonant_vowel", "before_index": 1, "after_index": 2,
    "confidence": 0.33, "movable": true}],
 "warnings": [{"kind": "long_vowel_no_control_point", "phoneme_index": 7,
               "message": "母音 'a' が 1029 ms あり内部に制御点が無い…"}]}
```

- `label` は **`consonant` / `vowel` / `breath` / `silence`** の 4 値。
  細かい内訳は `detail`（`consonant_voiced` / `consonant_unvoiced` / `vowel` /
  `moraic_nasal` / `closure` / `breath` / `silence` / `unknown`）。
- **`stretchable`** が「タイミング補正で長さを変えてよいか」。母音・息・無音が true、子音は false。
  撥音 `N` は母音扱い、促音 `cl` は無音扱い。
- `boundaries[].kind` は `onset` / `consonant_vowel` / `vowel_consonant` / `phoneme` / `offset`。
  素材の端（片側に音素が無い）だけは `move_boundary` できない。
- `confidence` はアライナーの確からしさ・**隣の母音に対するエネルギーの相対落差**・
  長さの妥当性・発声塊との整合、の 4 つの合議。低いところだけ人が見ればよい、という運用のため。

## 2. 直す

すべて非破壊。編集リストに行を足すだけで、音は `render_preview` のときに作る。
どれも `author`（`human` / `ai`、既定 `ai`）を渡せる。

### 2-0. タイミングは後ろをずらさない（接続 / 切り離し）

Melodyne と同じく、**タイミングの編集はそのノートと隣以外を 1 サンプルも動かさない**
（書き出しで範囲外がサンプル一致することを `tests/test_connection.py` が確かめている）。
隣り合う音程ノートの境目ごとに **接続 / 切り離し** の状態がある:

| 状態 | 端を動かしたとき | 既定 |
|---|---|---|
| **接続** | 境目を共有して動く。片方が短くなった分、隣がそのまま長くなる。挟まった子音は長さを保って一緒に動く | 隙間 0（ノート分割で隣接）か、間が無声（子音）だけで 0.30 秒未満 |
| **切り離し** | 自分だけ伸び縮みし、**隙間**が増減する。縮めてできた隙間は無音（20 ms のクロスフェード）、隙間へ伸ばしたぶんは隙間の音を切り取る。隙間の中身は元の位置のまま。次のノートの頭の子音（アタック）はそのノートと一緒に動く | 息・無音を挟む |

- 伸び縮みは**母音（と息・無音）だけ**で吸収し、子音の長さは保つ（歌詞が無ければノート全体）。
- 隣を追い越す手前、ノートが 20 ms を切る手前、伸縮比（元の長さの 0.05〜20 倍）の手前で止まる
  （結果の `clamped` が true）。
- 状態は `connection` 編集として編集リストに入る（既定と違うものだけ）。`undo` で位置と一緒に戻る。
- 内部の編集は `stretch`（比）/ `crop`（切り取り）/ `silence`（無音）の 3 種類に組み直される
  （`list_changes` に見える）。
- **子音・息（音程の無いノート。`list_notes(kind="all")` の `unvoiced` / `breath`）も幅とタイミングを変えられる**
  （issue #35。`move_note` / `stretch(note_id)` / `plan_edit(op="edge" | "move")` にその id を渡す。無音 `silence` は動かさない）。
  そのノートを操作するときだけ骨組みのノートに入れる（ほかの操作は今までどおり、境目の子音は長さを保って一緒に動く）。
  接続の既定は音程ノートどうしと同じ（接していれば接続 = 子音の端を動かすと接した隣が伸び縮み）。ただし挟んでいる
  音程ノートの組をユーザーが切り離していれば子音の両側も切り離し（次のノートの頭に接した 0.30 秒以下の無声は次とつながる）、
  つないでいれば両側も接続。Alt（`detach`）・吸着は音程ノートと同じく `connection` 編集に残る。ピッチは変えない
  （音程が無いので `shift_pitch` などの対象外）。

| ツール | 引数 | 意味 |
|---|---|---|
| `shift_pitch` | `cents`（+100 = 1 半音上）、`note_id` / 範囲 | 音程をずらす |
| `set_pitch_curve` | `points = [[区間頭からの秒, セント], ...]` | ピッチ曲線を与える（2 点以上）。`mode="draw"` は鉛筆（§2-1） |
| `move_note` | `ms`（+ が遅く）、`note_id` / **`note_ids`** / 範囲 | 横に動かす。接続側の隣が伸び縮み、切り離し側は隙間が吸収。子音・息・無音の区間も可（範囲で選ぶときは音程のあるノートだけ） |
| `stretch` | `ratio`（0.25〜4.0）、`note_id` / 範囲 / **`phoneme_id`** | `note_id`: ノートの**尻**を動かす（画面の右端ドラッグと同じ）。`phoneme_id`: その音素の後ろの境界を動かす（`move_boundary` と同じ）。範囲: 1 つのノートの中だけ（同じノートの残りが吸収） |
| **`move_boundary`** | `boundary_id`、`ms`（+ が遅く） | **音素の境目を動かす**（段階2）。2 音素の和を保つ局所編集 |
| **`list_connections`** | `start_sec?` / `end_sec?` | 隣り合う音程ノートの `connected` / `default` / `gap_ms` と、接続された境目の `transition`（なだらかさ）の一覧 |
| **`set_connection`** | `note_a`, `note_b`, `connected` | 接続を変える（音は変わらない。次の編集の動き方が変わる） |

返り値（`move_note` / `stretch`）: `{ok, changeset, x, clamped, range, snapped, window_sec, total_edits}`。
`range` は動かせる範囲（秒）、`window_sec` は組み直した範囲。`changeset` が `undo` の単位。

### 2-1. ノートの変わり目・鉛筆・分割

#### `set_transition(value?, note_ids? | note_a+note_b | start_sec/end_sec, replaces?)`

**ノートの変わり目のなだらかさ**（Melodyne のピッチトランジション相当）。**移動量に段差がある時刻**で、
段差を raised-cosine の曲線で渡す（原音の移り変わりの形はそのまま、差だけを補間）。段差の置き場は
**接続された境目**（隙間があれば、その中の段差の合計。隙間の中で続いている曲線は段差にしない）と
**ノートの中の段差**（結合した元の境目・範囲の編集の端。`list_connections` の `steps`）。
切り離しの境目・ノートの外は段差のまま。**何もしなくても自動で効いている**（既定 = 自動）。
値は**時刻で**持つので、**分割・結合しても線と音は変わらない**（issue #6。以前は結合で最大 50 セント変わった）。
0.1 セント未満の差は段差として扱わない。

| value | 意味 |
|---|---|
| 0 | 段差（補間しない。以前の挙動） |
| 0.5（既定）/ null | **自動**: 原音の境目で音程が実際に移り変わっている長さ（F0 の 10〜90%）を幅にする（30〜250 ms。段差の無い境目は 80 ms） |
| 1 | かなりゆっくり（max(400 ms, 自動 × 3)） |

窓の片側はそのノートの長さの半分まで。対象: `note_a`+`note_b`（その境目）/ `note_ids`（そのノートの両側と、ノートの中の段差）/
範囲（段差の時刻が入るもの）/ 省略で全体。値は編集リストの `transition`（範囲 = 段差の時刻）として project.json に残る（0.5 は上書きを外すだけ）。
`replaces` は画面のポップアップの当て直し（前回の changeset を取り消して捨てる）。

```json
// set_transition(value=0.8, note_ids=["n010"])
{"ok": true, "changeset": "c004", "pairs": 2, "value": 0.8, "total_edits": 3}
// list_connections() の 1 行
{"a": "n010", "b": "n011", "connected": true, "default": true, "gap_ms": 0.0, "at_sec": 2.45,
 "transition": {"value": 0.5, "auto": true, "auto_ms": 100.0, "width_ms": 100.0,
                "step_cents": -100.0, "smoothing": true}}
// list_connections() の steps の 1 つ（n004 と n005 を結合した後の、元の境目の段差）
{"note": "n004", "at_sec": 1.23, "transition": {"value": 0.8, "auto": false, ...}}
```

結合（`merge_notes`）は、境目にあった無音の挿入（切り離して縮めた隙間の `silence`）も外す。外したぶんは結合した
ノート全体を伸ばして埋め、頭と尻の編集後の位置は変えない（後ろはずらさない）。返り値の `removed_silence`。

#### `set_pitch_curve(points, mode="draw", ramp_ms?)` — 鉛筆

`points = [[素材の秒（編集前）, MIDI ノート番号], ...]`。その時間範囲のピッチを**描いた音程に置き換え**、
両端は `ramp_ms`（既定 40 ms）かけて元の曲線へなだらかにつなぐ。範囲は**有声のフレームに切り詰める**
（無声には音程が無いので描いても効かない。有声が無ければ `ok: false`）。上から何度でも描き直せる
（すっぽり覆われた前の線は外す）。描いた後に動かしたノートのピッチは描いた線にも足される。
戻すのは `reset_to_original`（そのノートにかかる部分だけ外す）か `undo`。

```json
// set_pitch_curve(points=[[149.30, 64.0], [149.45, 65.2]], mode="draw")
{"ok": true, "changeset": "c005", "start_sec": 149.3, "end_sec": 149.45, "points": 2,
 "replaced": 0, "clipped": false, "total_edits": 4}
```

#### `split_note(sec, note_id?, snap_ms?)` / `merge_notes(note_a, note_b, group?)`

`split_note`: ノートを `sec`（**編集前の秒**）で 2 つに分ける。左は元の id、右は `<元の id>@<ミリ秒>`（例 `n006@920`）。
新しい境目は隙間なし = **接続**。両端から 20 ms 以上内側。`snap_ms` 以内に音素境界があればそこで切る。
`merge_notes`: 接して並ぶ 2 つの音程ノートを 1 つに（左の id）。分割した境目ならその分割を外すだけ、
解析でできた境目も結合できる。`group` に同じ文字列を渡した続けての結合は、取り消しの履歴で 1 回にまとめる
（画面で 3 つ以上を選んで Ctrl+J）。

- 分割・結合は**時刻で**編集リストに入る（`split` / `merge`）。**解析し直しても残る**
- そのノートを対象にしていた編集は同じ区間の範囲対象に直し、接続・なだらかさは新しい id の組へ付け替える
  （**分割・結合だけでは音は変わらない**）
- 分割したノートはピッチ・タイミング・なだらかさ・ガイドに合わせる・オリジナルに戻すの全部でふつうのノート

```json
// split_note(sec=0.92)
{"ok": true, "changeset": "c006", "sec": 0.92, "snapped": false, "left": "n006",
 "right": "n006@920", "left_span": [0.86, 0.92], "right_span": [0.92, 1.0], "total_edits": 1}
```

### `move_boundary(boundary_id, ms, author?, note?)`

`boundary_id` は `get_phonemes` の `boundaries[].id`。**1 本動かすと隣り合う 2 つの音素の長さが
同時に変わり、2 つ合わせた長さは変わらない**（= 後ろはずれない。もともと局所編集）。
どちらの音素も **20 ms** は下回らない（下回る指定は自動で切り詰め、`moved.clamped` が true になる）。

`ms` は**編集後の時間軸**での移動量（画面でつまんだ距離そのもの）なので、
同じ境界を何度動かしても素直に積み上がる。編集リストには 1 件だけ載り、`undo` 1 回で戻る。

```json
{"ok": true, "changeset": "c003", "total_edits": 1,
 "moved": {"boundary_id": "b012", "kind": "consonant_vowel", "ms": 7.0,
           "requested_ms": 7.0, "clamped": false,
           "left":  {"id": "ph006", "text": "r", "label": "consonant",
                     "ms_before": 10.9, "ms_after": 17.9},
           "right": {"id": "ph007", "text": "a", "label": "vowel",
                     "ms_before": 197.8, "ms_after": 190.8},
           "min_phoneme_ms": 20.0}}
```

### `correct_to_guide(start_sec?, end_sec?, pitch_strength?, timing_strength?, threshold_cents?, threshold_ms?, note_ids?, match_pitch_shape?)`

画面の「ガイドに合わせる」と**同じ計画**（`plan_edit(op="guide")`）を作り、強度を掛けて確定する。
1 つの changeset。強度 0〜1（1 = ガイドどおり）。既定は `pitch_strength=0.7` / `timing_strength=0.0`
（「ガイドどおり＝正解」ではないため）。

- **音程の対応付け**: テイクの音程ノート ↔ ガイドの音程ノート（DTW でテイク時間に写した位置。
  同じ時間軸の素材は画面に描く位置）。お互いに「重なりが最大の相手」を取り、つながったものを 1 つの組にする。
  対応の無いノートは音程を動かさない。
- **ピッチ 100%**: 既定の `match_pitch_shape=true` は、対応する発音の頭を基準にノート内の時刻を比例で写し、
  有声フレームの F0 をガイドの F0 へ近づける。強度は Hz で線形に掛ける。ガイドが無声・対応が不確かな区間は動かさず、境目はなだらかにつなぐ。
  `false` では従来どおり、ノートの中心を一定量ずらして揺れの形を保つ。
- **タイミング 100%**（issue #12 で変更。`docs/guide-timing.md`）: テイクの**発音の頭**（音の立ち上がり。
  両方に歌詞があれば音節の頭）を、1 対 1 に対応する**タイムライン上のガイドの発音の頭**へ（トラックの位置
  ずらしも含む。issue #61）。全体のずれ（頭どうしの差の最頻値）が 150 ms を超える、置き場所の違う素材だけ
  「ガイドの時刻 + 全体のずれ」へ（計画の `info.basis` = `timeline` / `offset`、`info.measured_offset_ms`）。
  どの頭とどの頭が同じかは DTW で決め、位置の基準には使わない。対応が決まらない頭は動かさない。
  §2-0 の規則で動かすので後ろはずれない。
- **基準点と補間**（issue #53。`docs/guide-coverage.md`）: 確かな頭の組を**基準点**にし、基準点の間にある
  ノート（発音の頭が無い音程の変わり目など）の頭・尻は、同じフレーズ（0.5 秒以上の隙間で区切る）の
  前後の基準点に合わせて**比例**で動かす（VocAlign と同じ。隙間は中身を保って端だけ動く）。フレーズの
  端の基準点より外は、その基準点と同じだけ平行に。前後の基準点が 1 秒より離れている所と、基準点が 1 つも
  無いフレーズは動かさない。テイク・ガイドとも**確定の**歌詞がある所は、休みの手前の音節の終わりも
  基準点にする（推定の読みは使わない）。
- **しきい値**（既定 0 = 全部）は計画を作るときに掛ける。プレビューと確定で同じに効く。

```json
{"ok": true, "changeset": "c001", "x": 0.7, "pairs": 10, "matched_notes": 14,
 "unmatched_notes": [], "confirmed_notes": 9, "timing_notes": 14, "timing_anchor_notes": 5,
 "timing_interp_notes": 9, "timing_reached_notes": 14, "timing_possible": true,
 "correspondence": [{"note": "n003", "guide": ["g003"], "confirmed": true, "pitch": true,
                     "timing": "interp", "reached": true},
                    {"note": "n004", "guide": ["g004"], "confirmed": false, "pitch": false,
                     "timing": "interp", "reached": true,
                     "reason": "時間の重なりだけの候補（確かな発音の頭の組で確かめられない）"}],
 "pitch_notes": 14, "repaired": 0, "reach": 1.0,
 "window_sec": [0.0, 3.48], "next": "remeasure で残ったずれを見る。戻すなら undo('c001')"}
```

`repaired` は 100% で守れない制約（追い越し・短すぎ）を直した回数、`reach` は直しきれずに
全体を縮めたときの到達率（1.0 = ガイドどおりに届く）。

`correspondence` は対象の音程ノートごとの対応（画面の対応線・破線の丸・ホバーの説明と同じ中身）:

| キー | 意味 |
|---|---|
| `guide` | 音程で対応するガイドのノート（1 対多は同じ組の範囲） |
| `confirmed` | その組が確かな発音の頭の組で裏付けられている（画面は線を引く。false は時間の重なりだけの候補） |
| `pitch` | この計画で音程がガイドへ寄る |
| `timing` | `anchor`（頭が基準点）/ `interp`（前後の基準点に合わせて比例）/ `null`（動かさない） |
| `reached` | 100% で目標に届く（追い越し・伸縮の上限で戻した頭は false） |
| `reason` | 対応が無い・タイミングが無い理由（あるときだけ。「テイクの発音の頭を検出できない」など） |

`timing_possible=false`（確かな頭の組が 1 つも無いガイド。別の演奏など）のときは `timing_message`
（「このガイドとはタイミングを合わせられない…」）が付き、タイミングは 1 つも動かない。

### `plan_edit(op, note_id?, note_ids?, side?, detach?, start_sec?, end_sec?, threshold_cents?, threshold_ms?, match_pitch_shape?)` / `apply_plan(plan_id, x?, pitch?, replaces?)`

**計画を作る／確定する**。画面はドラッグの開始（端・ノートの移動）と「ガイドに合わせる」を
開いた時点で `plan_edit` を呼び、離したら `apply_plan` を呼ぶ。

計画 = 節（ノートの頭・尻、ノートの中の音素境界、アタックの頭）ごとの
**「編集後の秒 = cur + d × x」**。画面は x を動かしながら同じ式で描き、確定は同じ計画を
同じ x で組み直すので、**ドラッグ中の見た目と離した後の結果が一致する**。

| op | 対象 | x |
|---|---|---|
| `edge` | `note_id` + `side`（`start` / `end`）。`detach=true` で接続を切って自分だけ（画面の Alt） | 秒（+ が後ろ） |
| `move` | `note_id` / `note_ids` / 範囲 | 秒 |
| `guide` | `note_ids` / 範囲 / 省略で全体 | タイミングの強度 0〜1（ピッチは `apply_plan` の `pitch`） |

`plan_edit` の返り値: `{plan_id, path, x_range, snap_x, moving_knots, pitch_notes, pairs, info}`
（guide では `correspondence` も。`correct_to_guide` と同じ）。
`path` の JSON（画面が読む。MCP の結果に数値列を載せないため）の中身:
`knots = [[編集前の秒, cur, d, "l"|"r"], ...]`、`pieces = ["s"|"k"|"g", ...]`（stretch / keep / gap）、
`pitch = {note_id: 100% でのセント}`、`pitch_curve = [[テイクの秒, 現在の Hz, ガイドの Hz, 境目の重み], ...]`（guide の形状モード）、`pairs = [{take: [...], guide: [...]}]`（音程の対応。画面で濃くする
ガイドノート。`confirmed` = 確かな頭の組で裏付けられた組）、`timing = [{take, guide, take_sec, guide_sec, offset_sec, target_sec, mode, note, cur_sec, d_sec, reached}]`
（guide のみ。基準点ごとの目標と、100% で届くか。確定の歌詞の音節の終わりは `kind: "end"`）、
`notes = [{note, group, guide, take_span, confirmed, pitch, timing, reached, reason, timing_reason}]`
（guide のみ。ノートごとの対応。`group` は `pairs` の番号）。
`snap_x` は切り離された端が隣にぶつかる x（そこで確定すると接続になる）。
計画の JSON（path）には、確定で変わる接続 `set_connections`（`[[a, b, connected]]`。Alt の切り離し）と、吸着したときに生まれるつなぎ `snap_transition`（`transitions` と同じ形）も入る。画面はこれで、ドラッグ中の曲線に「切ると消えるなだらかさ」「つながると現れるなだらかさ」を反映する（どちらも x ≠ 0 のときだけ。`apply_plan` と同じ規則）。

`apply_plan` の `replaces` は先に取り消す changeset（ポップアップ内の当て直し）。取り消した後の
状態は計画を作ったときと同じなので、同じ計画がそのまま当たる。取り消した changeset は「捨てた」印
（`discarded`）が付き、`redo` で戻らない。外から編集されて状態が変わって
いたら同じ引数で計画を作り直す（`replanned: true`）。

#### `mute_notes(note_ids?, start_sec?, end_sec?)`（issue #17）

ノートを**無音にする**（画面の右クリック「無音にする」・Del）。編集リストに `mute`（範囲 = ノートの頭〜尻）が入る。
長さ・位置は変えず（後ろはずらさない）、その区間の音だけを消す。隣とは 20 ms で無音へフェードする。
ピッチなどの編集は残る。もう無音のノートは飛ばす。`export_view_data` の `notes[].muted` が true になる。
戻すのは `undo` か `reset_to_original`。

#### `set_fade(note_ids, fade_in_sec?, fade_out_sec?, author?)`（issue #20）

ノートの**フェードイン／アウト**（画面の帯の上の角のつまみ。DAW のクリップフェードと同じ）。1 つの changeset（「フェード」。
両方 0 で消すと「フェードを消す」）。

- **音量だけ**を変える（ピッチ・なだらかさは変えない）。形は等パワー（イン sin・アウト cos）。
- `fade_in_sec`: ノートの頭から 0 → 元の音量まで、`fade_out_sec`: ノートの尻の手前から 0 まで（**編集後の秒**。
  0 = 消す、省略 = そのまま）。
- 隣のノートは変えない（接続された境目にも付けられる）。フェードの外は元のサンプルのまま（書き出しも再生も）。
- ノートの長さを変えてもフェードの秒は保つ。ノートより長くはしない（片側だけ変えたときはもう片側を残して収め、
  両方渡してノートより長いときは比を保って縮める。後でノートを短くしたときも比を保って縮めて当てる）。
- 編集リストには `fade`（範囲 = ノートの頭〜尻、`side` = in / out、`sec`）が入る。錨はノートの端の時刻なので、
  分割するとインは左の片、アウトは右の片に残る。結合で消えた境目のフェードは効かない（結合を取り消すと戻る）。
- `list_notes` / `export_view_data` の `fade_in_sec` / `fade_out_sec` に今の値（ノートに収めたもの）が出る。
  `reset_to_original` でそのノートのフェードも外れる。

### `reset_to_original(note_ids?, start_sec?, end_sec?, boundary_ids?)`

`reset_to_original` はピッチの編集を外し（範囲のピッチ編集と鉛筆は、そのノートにかかる部分だけ外す）、タイミングは頭・尻を元の位置へ戻す計画で組み直す
（接続された隣は伸び縮みで合わせる。後ろはずれない）。戻したノートは原音のサンプルそのもの。
無音にした（`mute_notes`）ノートは音が戻る。フェード（`set_fade`）も外れる（そのノートの頭のイン・尻のアウト）。
`boundary_ids`（`get_phonemes` の `boundaries[].id`）を渡すと、その音素の境目を動かした編集（`move_boundary`）を外す
（画面の音素の右クリック「子音｜母音の境目を元に戻す」。`export_view_data` の `phonemes.boundaries[].moved`）。
これも取り消せる（`undo`）。

### 2-9. 取り消し `undo(changeset_id?)` / `redo()` — 曲で 1 本の履歴（issue #16）

**曲に保存されるものを変えた操作はすべて取り消せる**。履歴は**トラックをまたいで 1 曲で 1 本**
（セッションの `session.json` の `history`）で、画面の Ctrl+Z / Ctrl+Shift+Z と Claude Code の `undo` / `redo` が
**同じ履歴**を使う（どちらの操作も入る。項目の `author` が human / ai）。

| 取り消せる | 取り消せない（履歴に入らない） |
|---|---|
| ノートのピッチ・長さ・移動・分割・結合・接続／切り離し・なだらかさ・鉛筆・ガイドに合わせる・オリジナルに戻す・音素の境界・**歌詞**・トラックの追加・外す・位置・名前・種類（伴奏／ボーカル）・ガイドの指定 | ミュート／ソロ（聴き比べの操作。DAW と同じ）・表示・選択・編集対象の切り替え（`select_track`）・書き出し |

- `undo()`（引数なし）: 履歴の最後の操作を取り消す。**別のトラックの操作なら、そのトラックを編集対象にしてから戻す**
  （返り値の `switched_to`。切り替わったら `analyze_take` を呼ぶ）。トラックの操作は前の状態（並び・位置・名前・種類・ガイド）に戻す
  （ミュート／ソロは今のまま）。外したトラックは同じ id・同じ並びで戻り、そのトラックの編集もそのまま
- `redo()`: 直近に取り消した操作をやり直す。新しい操作を入れると、やり直しの列は捨てる
- `shift_pitch(group=…)`: 同じ group の続けての呼び出しは 1 回の取り消しにまとめる（画面の複数ノートのピッチのドラッグ）
- `shift_pitch(label=…)` / `apply_plan(label=…)`: 取り消しの履歴に出す名前（既定「ピッチ」「ノートの長さ」など。画面の「半音に合わせる」（Q）・右クリックの「つなぐ」が使う）
- `merge_notes(group=…)`: 同じ group の続けての結合は 1 回の取り消しにまとめる（画面で 3 つ以上を選んで Ctrl+J）
- ポップアップの当て直し（`apply_plan` / `set_transition` の `replaces`）は、前回の項目と入れ替わる（ポップアップ 1 回 = 取り消し 1 回）
- `undo(changeset_id)`: 編集対象のトラックの**その changeset だけ**を取り消す（前の版と同じ。順は問わない。
  後の編集が対象にしているノートが無くなる取り消しはできない）。履歴の項目も取り消し済みにする
- セッションの無いプロジェクト（サンプル列で開いた DAW 連携など）は、プロジェクトの changeset を末尾から戻す（前の版と同じ）
- **後方互換**: 履歴の無い session.json（前の版）は、各トラックの project.json の changeset を作った時刻の順に並べて作る。
  履歴を知らない経路（前の版のエンジン・プロジェクトを直接）で入った changeset も、次の `undo` の前に末尾に足す。
  外で（別のプロセスが project.json を書き換えて）もう取り消された項目は飛ばす
- 歌詞の変更は changeset（`{"op": "lyrics", "source", "before", "after"}`）。取り消すと前の歌詞に戻り、
  音素は歌詞ごとのキャッシュ（`cache/phonemes/`、8 件まで）から戻す（アラインし直さない）

```json
// undo() — 別のトラックの操作だった
{"ok": true, "undone": {"id": "h12", "label": "ピッチ", "kind": "edit", "track": "t2",
 "track_name": "take2", "author": "human"}, "switched_to": "t2", "reopened": true,
 "can_undo": true, "can_redo": true, "history": {...}, "session": {...},
 "next": "analyze_take を呼ぶ（編集対象が変わった）"}
```

編集・トラックのツールと `export_view_data` / `list_tracks` / `list_changes` の結果には `history`
（`{can_undo, can_redo, undo: {label, track, track_name, author}, redo: {...}, size}`）が付く。画面の
「元に戻す: ○○」はこれ。label は短い名前（ピッチ・ノートの長さ・ノートの長さ（接続の変更）・ノートの移動・分割・結合・つなぐ・
切り離し・なだらかさ・鉛筆・ガイドに合わせる・オリジナルに戻す・音素の境界・歌詞・無音にする・フェード・フェードを消す・
トラックの追加・トラックを外す・トラックの位置・トラックの名前・伴奏／ボーカルの扱い・ガイドの指定・テンポ・拍子・テンポと拍子・
1 小節目の位置・テンポを消す）。

### 画面と同時に使うときの注意

エンジンは **編集を適用する前に `project.json` がディスク上で変わっていれば読み直す**
（各編集ツール / `undo` / `redo` / `export_view_data` / `list_changes` の頭）。
Electron の画面と Claude Code が同じプロジェクトを同時に触る場合、保存時に別の書き込みを検出すると
`{ok:false, conflict:true, preparing:false, error:"別のエンジンが project.json を更新した…"}` を返す。エンジンは最新の
`project.json` を読み直す。Claude Code は返り値の `conflict:true` を見て最新の状態と履歴を取得し、必要なら操作を組み立て直す。
準備待ちの期限切れは `{ok:false, preparing:true, conflict:false, error}` で返り、準備が進むのを待って再試行できる。
画面は競合時に今の操作と順番待ちの編集・古いプレビューを破棄して状態と履歴を読み直し、
「別のエンジン（Claude Code など）がこのトラックを書き換えたので、読み直した。今の操作は当てていない」と表示する。
画面の保存と競合の読み直しは編集と同じ順番待ちの中で行う。1 つの画面操作を何回かのツールで当てるもの（複数ノートの
`shift_pitch`・`merge_notes`）は原子的ではないので、途中のツールが競合したら前の分は当たっている。画面は
「…今の操作は一部だけ当たった可能性がある。表示を確かめて」と表示する。Claude Code も、続けて呼んだツールの途中で
`conflict:true` が返ったら、前のツールの分は当たっている前提で読み直す。
準備待ちの期限切れは「準備中。少し待ってからもう一度」と表示する。
画面の `analyze_take`（`background=true` のジョブ）の合流には期限を付けない（長い曲の初めての準備は 15 秒を超える）。
代わりに画面が、取り消しを出さない待ちでも 15 秒を超えたら「待つのをやめる」を出し、やめたら合流したジョブを
`cancel_job` して（準備は続く）、準備が終わったところで描き直す。
解析キャッシュ（F0・ノート・DTW・音素）も、描画前にファイルの版が変われば読み直す。
画面側は `project.json` を `fs.watch` していて、変わると `export_view_data` を呼び直して描き直す。

保存はプロセスをまたいで排他する（issue #63）: `project.json` を書く間は `<プロジェクト>/project.lock` の
OS のロックを握り（10 秒取れなければ失敗）、書きかけのファイルはプロセス・スレッドごとに別の名前にする。
読み直してから保存するまでの間に別のエンジンが書いていたら、編集（`apply_plan`・各編集ツール・
`undo`・`redo`・歌詞の変更など）は自動ではやり直さず競合を返す。`analyze_take` の要約は、
最新の編集・歌詞・履歴を保ったまま、その解析結果だけを足して保存する。間の書き込みが解析要約だけなら、
編集中の変更は再計算せず、その要約を取り込んで保存する。

ファイルの版は更新時刻・サイズ・ファイル ID で確認する。音声の SHA-256 の使い回しにも同じ版を使う。
同じ場所へ直接上書きし、サイズと更新時刻を元に戻した場合はファイル ID も変わらず、変更を検知できない。
準備済みの印の内容ハッシュも同じ版の間は使い回すので、解析キャッシュをそう上書きして壊すと印は準備済みのまま見える。
その代わり、準備済みのキャッシュを実際に読んで失敗したら（JSON として読めない・中身が足りない）、エラーにせず
そのファイルを外し、それを含む準備済みの印とそのトラックの内容ハッシュの使い回しを取り消して準備し直す。
`analyze_take` は合流して待ってから開き（`background=true` ならジョブ）、`export_view_data` など解析の要るツールも
合流して作り直してから返す（同期の待ちは 15 秒まで。超えたら `preparing:true`）。

## 3. 確かめる

| ツール | 返すもの |
|---|---|
| `render_preview(start_sec?, end_sec?, backend?, name?)` | WAV のパス。**編集していない区間は原音のサンプルそのまま**、編集区間だけ 20 ms クロスフェードでつなぐ |
| **`export_wav(path?, start_sec?, end_sec?, backend?, full_source?)`** | **DAW に戻す WAV。元と同じ sr / ビット深度 / ch / 長さ / 開始位置** |
| `render_region(start_sec?, end_sec?, backend?, channels?, path?)` | **区間 → PCM**（32 bit float の WAV のパス。`channels` の既定は `"all"`＝素材のチャンネルそのまま。Python の `render/region.py` の既定は `"mono"`）。長さが変わらず、中身は `export_wav` が同じ範囲に書くものと同じ。`source_start_sec`・`rendered_windows_sec`・`timing_sec` を返す（DAW 連携 段階 0） |
| `render_audition(note_id, cents?, start_sec?, end_sec?, backend?)` | **画面向け**: つかんだノートのプレビュー音（issue #27）。ノートを `cents` 動かした**つもり**で範囲（既定はノートの範囲）を再合成したモノラルの WAV（`renders/audition.wav`。毎回上書き）のパス。**プロジェクトは書き換えない**。中身は `shift_pitch` を当ててから `render_region` したものと同じ |
| `render_view(start_sec?, end_sec?, show_guide?, title?)` | ピアノロール PNG のパス（波形・テイク F0・ガイド F0・ノートの帯・編集区間） |
| `export_view_data(start_sec?, end_sec?, peak_ms?, path?)` | **画面（UI）向け**の描画データ JSON のパス。**LLM 向けではない** |
| `remeasure(start_sec?, end_sec?, backend?, keep_wav?)` | 編集後の音を測り直した結果。ガイドがあれば `deviation_summary.before/after` |
| `list_changes(include_undone?)` | changeset と編集リストの一覧 |
| `get_job(job_id)` | 長い処理の結果。`status`・`progress`・`cancellable`。裏の準備に合流した `analyze_take` は `joined`・`stage`・`stage_label` も（§1 `analyze_take`） |
| `prep_status()` | 裏の準備の状態（§3-2 の「裏の準備」）。エンジンのロックを取らない |
| `pause_prep(paused?)` | 裏の準備を一時停止／再開（走っている段は最後まで進み、次の段の前で止まる。合流して待っているトラックは止めない） |
| `engine_info()` | バージョン・バックエンド・重みの有無・ログの場所。ピッチ検出の方式（`f0_estimator`＝選んでいる方式、`f0_estimator_effective`＝実際に使う方式、`f0_estimators`、`gliss_f0_model_found`） |
| `set_f0_estimator(estimator?)` | ピッチ検出の方式を選ぶ（`rmvpe`（既定）/ `gliss` / `praat`。そのエンジンの既定で、曲は変えない）。この後の `analyze_take`・裏の準備がその方式で解析する。返り値の `effective` が実際に使う方式（`rmvpe` を選んでいても重みが無ければ `gliss`）。画面が起動したエンジンと AI のエンジンは別のプロセスなので、AI 側で呼んでも画面の方式は変わらない |

`backend` は `praat`（既定。Praat（praat-parselmouth）の TD-PSOLA）、`psola`（自前の TD-PSOLA）、`world` のどれか。
praat-parselmouth が import できない環境では `praat` を頼んでも `psola` で再合成し、engine.log に警告を書く
（返り値の `backend` が実際に使ったもの）。既定にしたのは 2026-09-23（聴き比べで良かった方式。GPL の parselmouth を使えるよう本体を GPL にした）。

### `export_wav(path?, start_sec?, end_sec?, backend?, background?, full_source?)` — DAW に戻すファイル

`render_preview` は「聞いて確かめる」ための切り出しなので、範囲も長さも自由でよい。
**`export_wav` は約束が固い**:

- **サンプルレート・ビット深度・チャンネル数・総サンプル数・開始位置が元と同じ。**
  曲頭 0:00 起点のまま出るので、DAW のトラックに置き直せば位置がそのまま合う。
- **編集していないところは元のファイルとビット単位で同じ。**
  PCM_16 / 24 / 32 は `int32` のまま読み書きし、非編集区間は配列をコピーするだけ
  （float を経由すると量子化のずれが乗るため）。
- 編集のかたまりごとに窓を作り、その中だけ再合成して差し替える。窓の端は
  **前後 1 秒の中でいちばん静かなところ**に取る。伸縮で長さが変わったぶんは
  **窓の末尾の無音が吸収する**ので、総サンプル数は変わらない。
- `start_sec` / `end_sec` は「どの編集を反映するか」の指定。**出力は常に素材まるごと**。
- テイクがファイルの一部（`open_project` の `offset_sec` / `length_sec`）なら、既定は**クリップの長さ**の WAV
  （返り値の `start_sec` = ソース上の開始秒）。`full_source=true` で**ソースと同じ長さ**
  （クリップの範囲だけ差し替え、他はソースのサンプルそのまま）。
- 素材が 60 秒を超えると自動でジョブになる（`get_job` で取りに行く）。

- **BWF（`bext` / `iXML`）を元から書き写す**（DAW 連携 段階 1。`vocal_engine/bwf.py`）。DAW は
  `bext` の TimeReference（録音・バウンスした位置。サンプル数）で「元の位置」を知るので、書き出した
  ファイルを DAW に戻したとき元の位置に揃う。クリップをクリップの長さで書き出すときは TimeReference を
  クリップの開始ぶん進める（iXML の `BWF_TIME_REFERENCE_*`・`TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_*` も同じだけ）。
  元に `bext` が無ければ書き足さない（クリップのときだけ TimeReference = 開始位置の `bext` を足す）。
  `acid`・`cue `・`LIST` などほかのチャンクは書き写さない。

既定の書き先は**元ファイルの隣**の `<take名>_ve.wav`（拡張子は元に合わせる。FLAC なら `_ve.flac`。bext / iXML を書き写すのは WAV のときだけ）。**既存のファイルは上書きしない**（あれば
`<take名>_ve(2).wav`、`(3)` …。元が `…_ve.wav` なら `_ve` を重ねない）。元がサンプル列のとき・元の場所に
書けないとき（PermissionError）は `<project>/export/` に書いて warnings に書く。`path` に元の音声そのものは指定できない（再合成の前にエラー）。元の `bext` が壊れていれば写さずに warnings に書く。

```json
{"ok": true,
 "path": "D:/rec/vo_ve.wav",
 "sr": 48000, "channels": 1, "subtype": "PCM_24",
 "frames": 7584339, "duration_sec": 158.007062, "start_sec": 0.0,
 "same_as_source": {"sr": true, "channels": true, "subtype": true, "frames": true},
 "replaced_spans_sec": [[148.9, 150.96], [152.8, 154.77]], "replaced_sec": 4.03,
 "edits": 2, "backend": "praat", "elapsed_sec": 1.09, "warnings": [],
 "bwf": {"bext": true, "ixml": false, "time_reference": 4771683, "time_reference_sec": 99.41,
         "source_time_reference": 4771683}}
```

（クリップのときは返り値に `source_id`・`source_offset_sec`・`full_source` も入る。`replaced_spans_sec` は**クリップ内の秒**なので、`full_source=true` のファイルでは `source_offset_sec` を足した位置になる）

**`replaced_spans_sec` の外は元のファイルと完全に一致する**
（`engine/tests/test_export.py` と `test_production_song.py` が整数で突き合わせている）。

### `export_view_data` — 画面専用（エージェントは呼ばなくてよい）

Electron の画面がピアノロールを描くための配列を **JSON ファイルに書いてパスだけ返す**。
「生の数値列を MCP の結果に載せない」決まりはこれで守っている。
**LLM がこの JSON を読む意味は無い**（ずれや音程を知りたいなら `list_notes` /
`get_pitch` / `list_deviations` の要約を使うこと）。

| キー | 中身 |
|---|---|
| `waveform` | 波形のピーク列（`peak_ms` ごと、既定 2 ms の min / max）。画面幅に足りる粒度（エディターの画面では描かない） |
| `f0.take_env` / `f0.guide_env` | F0 のフレームごとの音量の包絡（RMS、0〜1。鳴っているフレームの 98 百分位 = 1）。画面のノートごとの小さな波形の太さ |
| `f0.take_midi` / `take_edited_midi` | テイク F0（10 ms、半音単位）。**編集前と編集後の両方**（編集後はなだらかさ・鉛筆込み。ノートの中心の音程は基本の段だけで測る） |
| `f0.take_edited_sec` | 各フレームを編集後の時間軸に写した秒 |
| `f0.guide_midi` / `guide_sec` | ガイド F0。タイムライン上のガイドの位置でテイク時間に置いたもの（置き場所の違う素材は + 全体のずれ）（発音の頭の組が 3 つ未満の素材は DTW の写像。`guide_basis`） |
| `notes` | id、開始/終了（編集前・編集後）、音名、編集前後の中心ピッチ、`pitch_editable`（息・囁きは false）、`text`（読み）、`phonemes`（重なる音素の id）、`muted`、`fade_in_sec` / `fade_out_sec`（編集後の秒。ノートに収めたもの） |
| `boundaries` | 境界一覧（`sec` = 編集前、`edited_sec` = 編集後、`kind`、`id`）。**歌詞があれば音素境界、無ければ音符境界** |
| `phonemes` | 音素レーン。`phonemes` / `syllables` / `boundaries`（`deviation_ms` つき）/ `warnings`。編集前後の秒を両方持つ |
| `lyrics` | テイクの歌詞をつないだ 1 本の文字列（画面の入力欄の初期値） |
| `lyrics_entries` | **区間ごとの歌詞**（`[{start_sec, end_sec, text}]`） |
| `export_default_path` | 「書き出し先を選んで書き出し…」（Ctrl+Shift+E）のダイアログの初期値＝`export_wav` の既定の書き先（元ファイルの隣の `_ve`。命名の規則はエンジン側に 1 つだけ置く） |
| `time_map` | 編集前の秒 → 編集後の秒 の区分線形写像（波形を引き伸ばして描くため） |
| `transitions` | 接続された境目ごとのなだらかさ（`ta`・`tb`・`value`・`auto_sec`・`hl`・`hr`・`max_hl`・`max_hr`・`delta`）。画面がドラッグ中・スライダー中に同じ式で曲線を描く |
| `f0.draw_keep` | 鉛筆があるときだけ。フレームごとの Π(1 − 鉛筆の重み) |
| `guide_notes` | ガイドのノート（タイムライン上のガイドの位置でテイク時間に置いたもの。置き場所の違う素材は + 全体のずれ（発音の頭の組が 3 つ未満の素材は DTW の写像。`guide_basis`）。薄い帯に使う） |
| `guide_basis` | ガイドの置き方 `{basis: "guide_time" / "dtw", offset_ms（描く・合わせる基準のずれ。ふだん 0）, measured_offset_ms（測った全体のずれ）, reference: "timeline" / "offset", same_timeline}` |
| `edits` / `history` | 編集リストと changeset の一覧、`can_undo` / `can_redo`（ツールの結果の `history` は曲の 1 本の履歴の要約。§2-9） |

`peak_ms` を小さくすると JSON が大きくなる（3.84 秒 / 2 ms で約 52 KB）。
書き先は既定で `<project>/cache/view/<鍵>.json`。**入力が同じなら前に書いたものをそのまま返す**（結果の `cached: true`。
issue #63 の 3）。鍵は素材・歌詞・編集の履歴・読み込んだ解析のファイル（更新時刻・サイズ・ファイル ID）・
発音の頭・引数・描画形式とエンジンのコード／ビルドの版から作る。最近の 8 件を残す（ガイドの付け外し・トラックの切り替えで戻る分）。裏の準備も、済んだトラックの
分を作っておく。`path` を渡すとそこに毎回作り直して書く。
`export_default_path` は書き出し先のファイルの有無で変わるので、結果（ツールの返り値）にも毎回入れる（画面はこちらを使う）。

```json
// remeasure
{"ok": true, "range_sec": [0.0, 3.84],
 "before": {"median_hz": 349.2, "median_note": "F4", "n_voiced": 241},
 "after":  {"median_hz": 440.1, "median_note": "A4", "n_voiced": 238},
 "deviation_summary": {"before": {"abs_pitch_cents_median": 489.8, "over_50_cents": 14},
                       "after":  {"abs_pitch_cents_median": 148.7, "over_50_cents": 11}}}
```

## 3-2. トラック（複数トラックのセッション。issue #7・`docs/track-view.md`）

1 曲ぶんの**複数トラック**（ボーカルのテイク複数・ガイド 1 本・伴奏）。`open_project(take_path)` で
テイクのプロジェクトのディレクトリに `session.json` ができる（issue #7 より前のプロジェクト（1 テイク＋ガイド）は
テイク＋ガイドの 2 トラックになる。中身は変えない）。2 本目以降のボーカルのトラックの編集はセッションの下の `tracks/` の
プロジェクトに入るので、Claude Code から触るときは `open_project(最初のテイク)` → `select_track`（そのファイルを単独で
`open_project` すると別のプロジェクトになる）。

- **既存のツールは編集対象のトラックに効く**（`select_track` で切り替え。切り替えたら `analyze_take`）。
- **ガイドは 1 本だけ指定**（`set_guide_track`）。編集対象のテイクには、ガイドのトラックを**タイムライン上の
  位置を合わせて**重ねる（テイクの頭の位置から切り出す。前が足りなければ無音で詰める）。
- **トラックの位置 `offset_sec`**（音源全体を非破壊でずらす）。編集の秒はトラックの頭が 0 のまま
  （タイムラインの秒 = `offset_sec` + 編集の秒）。書き出しは中身・長さそのままで BWF の TimeReference だけ動く。
- 伴奏（`kind: "inst"`）は聴くだけ（編集・ガイドにはできない）。

| ツール | 何をするか |
|---|---|
| `list_tracks()` | トラックの一覧 `{dir, path（session.json）, guide, current, timeline_sec, tempo（下の set_tempo）, tracks:[{id, name, kind, path, offset_sec, duration_sec, sr, channels, clip, mute, solo, guide, current, audible, project_dir, edits（編集対象だけ）}], guide_stale, guide_note}`。`guide_stale` = 外部でガイド・位置が変わって編集対象のガイドが古い（`select_track` で開き直す）。`guide_note` = 編集対象にガイドが重ならない理由（「ガイドが指定されていない」「編集対象がガイドのトラック自身」「ガイドがこのトラックの範囲に重ならない…」など。重なるなら null。issue #32）。ボーカルのトラックの `prep` = 裏の準備の状態（下の「裏の準備」。伴奏は null）。トラックのツールの返り値の `session` にも同じものが入る |
| `select_track(track_id)` | 編集対象を切り替える（返り値は `open_project` と同じ形）。伴奏は選べない |
| `add_track(path, kind?, name?, offset_sec?, guide?, select?, author?)` | ファイルをトラックとして足す。`kind` 省略時はファイル名から推す（inst・karaoke・オケ・伴奏 など → `inst`）。`guide=true` でガイドに指定（もうあるファイルなら指定だけ）、`select=true` で編集対象に |
| `remove_track(track_id, author?)` | セッションから外す（ファイルと、そのトラックの編集＝プロジェクトは消さない）。編集対象を外したら残りの最初のボーカルへ。ボーカルが 0 本になる外し方はできない。**同じファイルを `add_track` で足し直すと、前の id・前の編集のまま戻る**（最初のテイクも。issue #32） |
| `set_track(track_id, name?, kind?, mute?, solo?, offset_sec?, index?, author?)` | 渡したものだけ変える。`offset_sec` で位置をずらす（負も可）。編集対象かガイドの位置が変わると開き直す（`reopened: true` → `analyze_take`）。`index` でトラックの並びの何番目に置くか（0 が一番上。範囲の外は端に丸める。issue #38。並びは見た目だけで、音・編集・ガイドとの対応は変わらない。取り消しの名前は「トラックの順番」） |
| `set_guide_track(track_id?, author?)` | ガイドを指定（null で外す）。変わったら `analyze_take` |

| `set_tempo(bpm?, numerator?, denominator?, start_sec?, clear?, group?, author?)` | 曲のテンポと拍子（画面の時間グリッド・スナップ・ルーラーの小節と拍。issue #18）。渡したものだけ変える。音は変わらない。下の「テンポ」 |

トラックの追加・外す・位置・名前・種類・並び順・ガイドの指定・テンポは**取り消せる**（§2-9。ミュート／ソロは取り消しの対象外）。

**裏の準備**（issue #63。`engine/vocal_engine/prep.py`）: エンジンは、ボーカルのトラックの解析（テイクの F0 と音符・
歌詞の推定・音素・セッションのガイドとの対応付け）を**選ばれる前に裏で済ませておく**。選んだときの `analyze_take` は
キャッシュを読むだけになる。

- 始めるとき: 開いた（`open_project` / `load_project` / `new_project` の後のトラック）・`add_track`・`remove_track`・
  `set_track`（位置）・`set_guide_track`・取り消し／やり直しでトラックやガイドが変わった・`session.json` が外部で
  変わっていたのを読み直した。画面ではなくエンジンが決めるので、Claude Code から操作しても同じ。
- 1 本ずつ。順番は 今のトラック → ガイドのトラック → 最近選んだ順 → 並び順。今のトラックが待っていれば、ほかの
  トラックの準備は段の境目で譲る（済んだ段は鍵付きで保存してあるので、戻ったら続きから）。
- ガイドの指定・位置が変わったら、古い組み合わせの準備はやめて（走っていれば次の段の境目で止まる）新しい組み合わせで
  入れ直す。
- **エンジンのロックを握らない**。準備の間も編集・再生・保存のツールはそのまま返る。書き込むのは対象のトラックの
  キャッシュと、最後に 1 回だけ project.json（読み直した最新の内容に、テイク側の解析の要約と歌詞の推定を足す。
  その間に入った編集・歌詞は消さない。推定は、その間に入った歌詞と重なる区間を捨てる）。project.json のガイドの写しは
  書き換えない（選んだときに合わせる）。
- 再生前の音作り（`render_tracks`）・試聴（`render_audition`）・`render_region`・`render_preview`・`export_wav` の間は
  次の段へ進まない（走っている段は続く）。
- 済んだら `cache/prepared.json` に組み合わせの署名を残す（エンジンを開き直しても、済んだトラックはやり直さない）。
  組み合わせ（ガイドの指定・位置）ごとに最近の 8 件を残すので、ガイドを外して戻す・A→B→A でも準備をやり直さない。
  署名には、選んだときにキャッシュを読むだけで済むのに要るファイル（テイクの解析・ガイドの解析・対応付け・発音の頭・
  音素）とその大きさ・内容ハッシュ、テイクの歌詞の鍵を残す。1 つでも欠けていれば印を書かない。後で消えた・変わった、歌詞が
  変わった（別のエンジンが変えたのも）なら準備済みではなくなり、裏で準備し直す（歌詞だけなら音素だけ計算する）。
  内容ハッシュは同じ版の間は使い回す。読んで壊れていたキャッシュは外して印を取り消し、準備し直す（§2 の「ファイルの版」）。
- ガイドとの対応付けに失敗して位置のままの対応になった（#32）組み合わせは `failed`（理由が `error`）。同じ組み合わせ
  では裏でやり直さない（選んで `analyze_take` すると表で取り直す）。
- 表で解析が要るとき（`analyze_take` を呼ばずに `list_notes` などを呼んだ・歌詞を入れて音素を取る）は、そのトラックの
  準備が順番待ち・準備中なら最優先にして待ち、計算は裏の 1 回で済ませる。表が自分で計算する間は、裏は同じトラックを
  始めない。
- 別のプロセスのエンジン（画面と Claude Code）とは、準備の間 `<プロジェクト>/prep.lock` の OS のロックを握って
  同じトラックを同時に準備しない（握られていれば後回し。落ちたプロセスのロックは OS が外す）。`cache/preparing.json`
  （pid と時刻）は状態を見る人向けの印で、準備の間 5 秒ごとに時刻を更新する。
- 作業場所を消す・空にするとき（無題を保存しないで閉じた・名前を付けて保存で移った）は、準備をやめさせて離れるのを
  最長 2 秒待つ。段の途中でまだ中にいれば、消すのは離れた後に回す（空にするときは 10 秒待って、離れなければ失敗）。
  後に回した削除の予定（作業場所の置き場の `.pending-delete/<鍵>.json`）には予定の世代を残し、同じ世代を作業場所の中の
  `.pending-delete` にも書く。その作業場所を使い始めたら（`load_project`・`.gliss` を開く・保存で移る）予定を取り消す。
  片付けは、予定と中の世代が同じで、中のロック（`project.lock`・`prep.lock`）がすべて取れたときだけ消す。
  準備は project.json・済みの印を書く直前にも取り消しを確かめ、消されたディレクトリを作り直さない。
- 最後に、選んだときの描画データ（`export_view_data`）も作っておく（段 `view`「描画データ」。`VOCAL_ENGINE_PREP_VIEW=0` で作らない）。
- `VOCAL_ENGINE_PREP=0` で止める。

`prep_status()` と `list_tracks().tracks[].prep`:

```json
{"ok": true, "enabled": true, "session_dir": "...", "current": "t1", "paused": false, "running": "t4",
 "tracks": [{"id": "t1", "state": "ready", "stage": null, "progress": 1.0, "error": null, "paused": false},
            {"id": "t4", "state": "preparing", "stage": "alignment", "stage_label": "ガイドとの対応付け",
             "progress": 0.65, "error": null, "paused": false},
            {"id": "t5", "state": "queued", "stage": null, "progress": null, "error": null, "paused": false}]}
```

`state`: `ready`（準備済み）/ `preparing`（準備中。`stage` が今の段）/ `queued`（待ち）/ `failed`（`error` が理由。
選んで `analyze_take` するともう一度やる。対応付けの失敗は裏ではやり直さず、表で取り直す）。`paused` = 表の重い処理・一時停止で次の段へ進むのを待っている。
`select_track` などの返り値の `analyzed` は「`analyze_take` がキャッシュを読むだけで済むか」（今のガイドとの対応付けまで）。

**テンポ**（`session.json` の `tempo` = `{bpm, num, den, start_sec, source, …}`。無ければ `null` = 画面は秒のグリッド）:

- `bpm` は 4 分音符の数／分（20〜400。Studio One と同じ）、`num` / `den` は拍子（1〜16 / 2・4・8・16）、
  `start_sec` は 1 小節目の頭（タイムラインの秒。負も可）。
- **iXML から自動で読む**: まだテンポが無いとき、足した・開いたトラックの WAV に PreSonus のテンポマップ
  （Fender Studio Pro のミックスダウン・バウンスの iXML の `<PRESONUS><TEMPO_MAP>`）があれば読む（伴奏を先に見る）。
  `source: "ixml"`、`file`（読んだファイル名）、`varies`・`bpm_range`（途中でテンポが変わる曲。グリッドはファイルの頭の
  テンポ 1 つで引く）。1 小節目はソングの 0:00 とみなし、bext の TimeReference（ファイルの頭のソング上の位置）から求める。
  トラックの追加で読んだテンポは、その追加を取り消すと一緒に消える。
- iXML の読み方（手元の Studio One のソング 14 本で確認）: `TEMPO_SEGMENT_OFFSET` = ファイルの頭からのサンプル数、
  `TEMPO_SEGMENT_VALUE` = 1 拍の秒数（60 / 値 = BPM）。拍子は入っていない（4/4 とみなす。画面・`set_tempo` で変える）。
- `set_tempo` で手で変えると `source: "manual"`。`group` に同じ値を渡した続けての変更（画面のドラッグ・ホイール）は
  取り消しの履歴で 1 回にまとまる（まとめた結果が元と同じなら項目は消える）。`clear=true` で消す。
  取り消しの名前は「テンポ」「拍子」「テンポと拍子」「1 小節目の位置」「テンポを消す」。
| `track_overview(track_ids?)` | 画面向け: チャンネル別の符号付き min/max 波形（32～8192 サンプル刻み、Int8 バイナリ）の JSON メタのパス |
| `render_tracks(track_ids?, backend?, background?)` | 画面の再生向け: トラックごとの音のファイル。編集のあるボーカルは編集を当てた音（`render_region` と同じ中身・長さ不変・チャンネルそのまま。編集が変わるまで使い回す）、他は元のファイルのパス（ファイルの一部のトラックと、AIFF など画面が読めない形式は、セッションの `mix/` に書いた WAV）。`tracks[].start_sec` がタイムライン上の位置。音声が無いトラックは `error` だけ（他は鳴らせる） |

`export_wav` はトラックの位置をずらしていれば TimeReference をその量だけ動かす（返り値の `timeline_offset_sec`。
元に bext が無くても足す。前へずらして 0 より前になるときは 0 にして警告）。

## 3-3. プロジェクトのファイル（新規・開く・保存。issue #33・`engine/vocal_engine/project/document.py`）

画面のファイル > 新規プロジェクト（Ctrl+N）・開く（Ctrl+O）・保存（Ctrl+S）・名前を付けて保存（Ctrl+Shift+S）と同じ。

| もの | 置き場 | 中身 |
|---|---|---|
| **プロジェクトのファイル** `<名前>.gliss` | ユーザーが選んだ場所（画面の既定は最初のトラックの音声の隣） | 1 つの JSON（`format: "gliss-project"`, `version: 1`）。セッション（トラック・ガイド・テンポ・**取り消しの履歴**）と、各トラックの編集（`project.json` から解析の要約・置き場・更新日を除いたもの）。**音声は参照**（`path` = 絶対パス、`rel` = ファイルからの相対パス） |
| **作業場所** | `%LOCALAPPDATA%\Gliss\work\<ファイルのパスのハッシュ>-<名前>\`（環境変数 `VOCAL_ENGINE_WORK_DIR` で変えられる） | 今までのプロジェクトのディレクトリと同じ（`session.json`・`project.json`・`tracks/`・**解析のキャッシュ**・再生用の音）。編集のツールは今までどおりここに書く |

- **保存** = 作業場所の中身を `.gliss` に書く。**未保存**（`dirty`）= 作業場所の中身が最後に保存した中身と違う
  （編集対象の切り替え・解析の結果・ガイドの写し・編集の無いトラックのプロジェクトは比べない。ミュート／ソロは比べる）。
- **保存しないまま落ちても消えない**: 作業場所は編集のたびに書いているので、同じファイルを開けば続きから開く（`recovered: true`）。
  「保存しない」は `close_project(discard=true)`（作業場所を最後に保存した中身に戻す）。
- 同じファイルを開けば、画面と Claude Code は同じ作業場所を使う。トラックの保存競合は `conflict:true` で通知する。
- 開くときの音声の探し方: 絶対パス → `.gliss` からの相対パス → `.gliss` と同じフォルダ（と `Media/`）の同じ名前。見つからないものは
  `missing`（トラックはそのまま）。中身のハッシュは比べない。
- ファイルが外で書き換わっていて（別の PC で保存して同期された、など）作業場所にも保存していない変更があったら、作業場所の JSON を
  `backup/<時刻>/` に写してからファイルの中身で開く（`backup`）。
- **旧形式**（issue #33 より前の `projects/<テイク名>-<sha8>/`）はそのまま開ける（`kind: "legacy"`。今までどおり編集のたびに自動で
  保存され、`dirty` にはならない）。`open_project(take_path)` も今までどおり使える。名前を付けて保存すると `.gliss` になり、旧形式の
  ディレクトリに `moved_to.json` を置く（同じテイクを `open_project` / `new_project(take_path)` で開くと、その `.gliss` を開く）。

| ツール | 何をするか |
|---|---|
| `new_project(name?, take_path?, guide_path?, author?)` | 新しいプロジェクト（`kind: "untitled"`）。`take_path` を渡すと最初のトラック（ボーカル・編集対象）にする。**そのテイクの旧形式のプロジェクトがあれば、新しく作らずにそちらを開く**（`opened: "legacy"` か、保存済みなら `"gliss"`）。無題は保存するまで作業場所にだけある |
| `load_project(path?, author?)` | 開く。**`path` を省くと Gliss の画面で今開いている曲**（と画面で編集中のトラック。§4-1 の bridge.json）を開き、返り値に `from_app: true`（画面で何も開いていなければ ok=false）。`path` は `.gliss`・旧形式のディレクトリかその `session.json` / `project.json`・無題の作業場所・音声ファイル（= `new_project(take_path)`）。返り値は `open_project` と同じ形に `document`・`opened`・`missing`・`recovered`・`backup` |
| `save_project(path?)` | 保存。`path` を渡すと名前を付けて保存（`.gliss` を付ける）。無題・旧形式は `path` が要る。作業場所が変わったら（`moved: true`）編集対象を開き直すので `analyze_take` |
| `project_status()` | `document = {kind: "gliss" / "untitled" / "legacy" / "ara"（§3-4）, path, name, dirty, work_dir, tracks, saved_at}`（開いていなければ null）。編集・トラックのツールの返り値にも `document` が付く |
| `close_project(discard?)` | 閉じる。`discard=true` で保存していない変更を捨てる（`.gliss` は最後に保存した中身に戻し、無題は作業場所ごと消す）。無題の削除結果は `removal: "removed" / "deferred"`。延期分は次回起動時にも片付ける。延期の間に同じ作業場所を `load_project` で開き直したら、削除の予定は取り消す |

Claude Code から: `load_project("D:/…/曲.gliss")` → `list_tracks` → `select_track` → 編集 → `save_project()`。
**画面で開いている曲を触るときは `load_project()` を引数なしで呼ぶ**（画面と同じ作業場所・同じトラック。編集は画面に即反映される）。
同じ `.gliss` を `load_project(path)` しても同じ作業場所を使う。保存は画面の「AI に許可: 保存・書き出し」がオンのときだけ AI からもできる（§4-1）。

## 3-4. DAW（VST3 + ARA 2 のプラグイン。`engine/vocal_engine/mcp_ara.py`）

DAW のプラグイン（C++）がエンジンを子プロセスで 1 本起動し（`GLISS_CLIENT=ara`。§4-1 の許可に従わない）、次の対応で使う。
**AI のプロセスからは呼べない**（許可に関係なく `permission: "ara"` で断る）。

| DAW（ARA） | エンジン |
|---|---|
| Document | セッション 1 つ（作業場所 `%LOCALAPPDATA%\Gliss\work\ara\<work_key>\`。文書の種類 `ara`） |
| AudioSource | プラグインが書いたソースの WAV（ソースの周波数・チャンネルのまま） |
| AudioModification | **ボーカルのトラック 1 本**（`ara_id`。範囲はソース全体、編集の秒はソースの秒）。プロジェクトは `tracks/ara-<ara_id のハッシュ 12 桁>/` |
| PlaybackRegion | エンジンは知らない（トラックの `offset_sec` = 代表のリージョンでソースの 0 秒が置かれるソングの秒） |
| MusicalContext | `session.tempo`（`source: "daw"`。`ara_sync` の `tempo`。プラグインはまだ渡していない） |

- `ara_*` は **`author` を取らず、取り消しの履歴に入れない**（DAW が決めたことを Gliss の Ctrl+Z で戻させない）。
  画面の操作（ガイドの指定・テンポなど）の `session` の項目を Ctrl+Z しても、ARA のトラック（有無・位置・名前・素材）と
  DAW のテンポは今のまま（戻すのはガイドの指定・画面で変えたテンポ）。DAW が外したトラックの編集の項目は履歴から外す。
- 同じソースの 2 つの修飾は、同じ WAV を指す**別のトラック・別の編集**（同じファイルの重複の検査をしない）。
- 編集・取り消し・トラックの選択・解析は今までのツール（`select_track`・`analyze_take`・`shift_pitch`・`undo` …）。
  `list_tracks` などの `session.tracks[]` には、ARA のトラックだけ `ara_id`・`group` が付く。
- `new_project`・`load_project`・`open_project`・`save_project`・`close_project`・`add_track`・`remove_track`・`export_wav`・
  `render_tracks` はプラグインからは呼ばない（プラグインが断る）。`ara` の文書の `save_project` はエンジンも断る。
- `project_status().document.kind = "ara"`、`dirty` は常に false。`close_project` は作業場所を消さない（開き直したとき解析の
  キャッシュを使う）。`bridge.json` には載らない（AI の `load_project()` はプラグインの曲を開かない）。
- プラグインがソースの WAV を書き直す（同じ音でもファイルのバイトは変わりうる）ので、編集を捨てないように、
  `ara_set_modification`・`ara_open` がテイクの参照を直してからプロジェクトを開く。その前に開こうとすると
  （WAV が書き換わった後、`ara_set_modification` の前）「DAW の音を読み込み中」で断る。

| ツール | 引数 | 返り値・中身 |
|---|---|---|
| `ara_open` | `work_key`（英数字・`-`・`_` の 1〜64 字。DAW のドキュメントごと）, `name?` | `{dir, opened, created, document, session, project_dir, analyzed, guide_note}`。作業場所を開く／作る。同じ鍵で開いていれば何もしない（`opened: false`）。前に選んでいたトラックがあれば編集対象にする |
| `ara_set_modification` | `ara_id`, `source_path`, `source_id?`, `name?`, `offset_sec?`（新しいトラックの既定 0。既存で省けば今のまま）, `group?`（DAW のトラック名）, `clone_of?`（複製元の `ara_id`） | `{track: {id, ara_id, name, group, offset_sec, duration_sec, sr, channels, source_frames, source_id, path, project_dir, current, guide}, created, source_changed, cloned, selected, analyzed, session}`。トラックを作る／直す。編集対象が無ければ編集対象にする（`selected`）。素材のファイルが変わっても音が同じなら編集はそのまま、音が変わったら `source_changed: true`（編集は残し、解析は捨てる）。外した `ara_id` を足し直すと前の id・前の編集。`clone_of` は新しく作るときだけ、素材の中身が同じならその編集を写す |
| `ara_remove_modification` | `ara_id` | `{removed, track, switched_to, reopened, session}`。外す（ボーカルが 0 本でもよい。プロジェクトのディレクトリは残す）。無ければ `removed: false` |
| `ara_sync` | `tracks: [{ara_id, offset_sec?, name?, group?}]`, `tempo?: {bpm, numerator, denominator, start_sec}`, `guide?`（ガイドにする修飾の `ara_id`。`""` で外す。アーカイブのガイドを戻すとき用） | `{changed: [track_id], unknown: [ara_id], tempo, tempo_changed, guide, guide_changed, reopened, session}`。位置は 1 サンプル未満の差なら変えない。ガイドの指定も履歴に入れない（画面の `set_guide_track` は入る）。編集対象のガイドの重ね方が変われば開き直す（`reopened: true` → 画面は `analyze_take` から描き直す） |
| `ara_render_dirty` | `ara_id`, `since?`（前に受け取った `rev`）, `backend?`（`praat`）, `channels?`（`all` / `mono`）, `max_sec?`（1 回で再合成する窓の長さの上限。既定 10） | `{track, rev, reset, more, analysis_pending, sr, channels, source_frames, restore: [[start_frame, frames]], windows: [{start_frame, frames, byte_offset}], path, rendered_sec, backend, timing_sec}`。下の「差分の再合成」 |
| `ara_revs` | なし | `{revs: {ara_id: rev}, track_ids: {ara_id: track_id}, errors}`。**ロックを取らない**（ディスクの project.json から）。プロジェクトがまだ無い修飾は `"empty"` |
| `ara_archive` | `ara_ids?`（省けば全部） | `{archives: {ara_id: {name, track, archive}}, guide: <ガイドの ara_id> \| null, tempo, errors, missing}`。`archive` は `Project.to_archive()` の形（素材の参照・歌詞・changeset の列。ガイドは入れない）。**ロックを取らない**（解析のジョブの最中も保存を止めない）。素材の照合のハッシュはトラックを作ったときに覚えた値を使う。プロジェクトがまだ無い修飾は `archive: null` |
| `ara_restore` | `ara_id`, `archive` | `{track, mismatch, reason?, edits, changesets, reopened, session}`。編集を戻す（履歴に入れず、戻した changeset も Ctrl+Z の列に入れない）。素材の長さ・音の中身が違えば**戻さずに** `mismatch: true`（`ok` は true。プラグインはアーカイブを持ち続ける）。編集対象なら開き直す |

**差分の再合成**（`ara_render_dirty`。`render/region.py` の `EditCache` をファイルで渡すもの）:

- エンジンは修飾ごとに「最後に渡した `rev` と、その時の Segment 列・窓」を覚える（このプロセスの中だけ）。
  `since` がそれと同じなら、変わった範囲（`dirty_windows`）だけ。違う・初回・エンジンを起動し直した・解析か素材が変わった・
  `backend` / `channels` が違うなら `reset: true` で全部の窓。
- 当て方（プラグイン）: `reset` なら手元の窓を全部捨てて原音から → `restore` の範囲を原音に戻す → `windows` を置く
  （`path` の `.f32` = float32 リトルエンディアンのインターリーブを窓の順に並べたもの。`byte_offset` が各窓の頭、
  長さは `frames × channels × 4` バイト）。結果は `render_region` の全体（＝書き出し）と**サンプル単位で同じ**。
  `.f32` は作業場所の `ara-out/` に修飾ごとに最近の 4 つだけ残る（読んだら手元に写す）。
- `max_sec` を超える分は次に回す（`more: true`。`rev` は途中の印 `<版>~<n>`。`since=rev` で続けて呼ぶ。少なくとも窓 1 つは返す）。
  途中で編集が入っても、続きの呼び出しで正しく当たる。
- `rev` = `<解析の署名>:<編集の署名>`（素材・テイクの解析・編集リスト・歌詞から決まる）。`ara_revs` と同じ値なので、
  プラグインは手元の `rev` と違う修飾だけを呼べばよい（取り消しで別のトラックが変わったときも拾える）。
- 編集はあるがテイクの解析がまだ（別の PC でアーカイブから戻した直後など）なら、解析を待たずに窓を空で返す
  （`analysis_pending: true`。原音のまま）。裏の準備・`analyze_take` で解析が済むと `rev` が変わる。
- 編集対象でないトラックも、編集対象を変えずにディスクのプロジェクトから作る（下ごしらえは修飾ごとに 4 本まで持つ）。
  再合成の間は裏の準備は次の段へ進まない。

呼ぶ順（プラグイン）: 起動 → `engine_info` →（あれば）`set_f0_estimator` → `ara_open(work_key)` →
ソースの WAV を書いたら各修飾に `ara_set_modification`（アーカイブから戻すなら続けて `ara_restore`）→ `ara_render_dirty(since=null)`。
DAW の操作は `ara_sync` / `ara_set_modification` / `ara_remove_modification`。画面の編集の後は `ara_revs` → 違う修飾に
`ara_render_dirty(since=<手元の rev>)`（`more` の間は続ける）→ `ara_archive(変わった ara_id)` で保存用の写しを取り直す。

## 4. Claude Code から使う

**画面から登録するのがいちばん簡単**: Gliss の **ヘルプ > AI とつなぐ…** で

- **Claude Code [追加]**: `claude` コマンドが PATH にあれば、Gliss が `claude mcp add-json gliss '<json>' --scope user` を実行する
  （ユーザー全体。どのフォルダーで Claude Code を起動しても `gliss` が使える）。登録済みなら「追加済み」。
- **Claude Desktop [追加]**: `%APPDATA%\Claude\claude_desktop_config.json` の `mcpServers` に `gliss` を 1 件だけ足す
  （ほかのキーはそのまま。書く前に `claude_desktop_config.json.gliss-backup-<時刻>` に写す）。足したら Claude Desktop を再起動する。
- **その他 [設定をコピー]**: 下の形の `mcpServers` の JSON をクリップボードへ（ほかの MCP クライアントに貼る）。

登録する設定は `cwd` に頼らない（Claude Code の設定は `cwd` を効かせないため）。開発版は画面がエンジンの起動に使っている
`.mcp.json` の `gliss` の `command` / `args` に、エンジンの場所を `PYTHONPATH` で足したもの（`app/ai-connect.mjs` の `serverConfig`。
配布版は同梱の実行ファイルを指す予定で、分岐の置き場だけある）:

```json
{
  "mcpServers": {
    "gliss": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "vocal_engine.mcp"],
      "env": {
        "PYTHONPATH": "<repo>\\engine",
        "PYTHONIOENCODING": "utf-8",
        "GLISS_BRIDGE": "C:\\Users\\<名前>\\AppData\\Roaming\\Gliss\\bridge.json"
      }
    }
  }
}
```

手で書くときは、リポジトリ直下の `.mcp.json`（このリポジトリで Claude Code を起動したときだけ効く）:

```json
{
  "mcpServers": {
    "gliss": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "vocal_engine.mcp"],
      "cwd": "<repo>\\engine",
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

- `command` は **venv の python の絶対パス**。`python` ではダメ（依存が入っていない）。
- サーバー ID に **アンダースコアを入れない**（ツール名を `mcp__<id>__<tool>` で分解するクライアントがあるため）。
  `gliss` のように短く。
- 重み（`rmvpe.onnx` と HubertFA）は `%LOCALAPPDATA%\Gliss\models` を見る（画面の初回のダウンロードと同じ場所）。別の場所に置くなら
  `env` に `VOCAL_ENGINE_MODELS_DIR` を足す（F0 も音素も同じ変数。旧名 `VOCAL_ENGINE_MODELS` も読むが、
  新しい名前が優先）。単体 exe（`engine/packaging`。配布版）の既定も同じ。
- プロジェクトの置き場を変えるなら `VOCAL_ENGINE_PROJECTS`。

### 4-1. AI に許可と、画面で開いている曲（bridge.json）

画面が起動するエンジンと、AI（Claude Code など）が起動するエンジンは**別のプロセス**。画面は自分のエンジンに
`GLISS_CLIENT=app` を渡し（DAW の ARA プラグインは `GLISS_CLIENT=ara`。§3-4）、**それ以外のプロセス（= AI）だけ**が画面の設定に従う（`vocal_engine/bridge.py`）。
画面は userData（`%APPDATA%\Gliss`）の `bridge.json` に書き、AI 側のエンジンは**ツールを呼ぶたびに**読む。置き場は
環境変数 `GLISS_BRIDGE` で差し替える（画面から登録した設定には入っている）。

```json
{
  "version": 1,
  "allow": { "edit": true, "save": false },
  "project": { "path": "C:\\曲\\うた.gliss", "kind": "gliss", "name": "うた",
               "track": "t2", "track_name": "take1" },
  "updated_at": "2026-09-27T12:00:00.000Z"
}
```

- `allow`: ヘルプ > AI とつなぐ の「AI に許可」。**編集**（既定オン）・**保存・書き出し**（既定オフ）。`bridge.json` が無い・読めない
  ときも既定のとおり。許していないツールは `{"ok": false, "permission": "edit" | "save", "error": "AI の編集は許可されていない。
  Gliss の ヘルプ > AI とつなぐ の「AI に許可: 編集」で許可できる"}` を返す。
- `project`: 画面で今開いている曲（`path` は `.gliss`、無題・旧形式は作業場所のディレクトリ）と編集中のトラック（`track` = id）。
  画面がツールの返り値の `document` / `session` を見て書き換え、画面を閉じると `null` にする。`load_project()`（引数なし）がこれを開く。
- AI 側のプロセスでは、編集の `author` は必ず `"ai"` になる（`apply_plan` の既定 `human` や、`author="human"` を渡しても）。
  画面は取り消しの履歴の表示（ツールチップ・編集メニューの「元に戻す: AI · ピッチ」・ステータス）と、最後に当たっている編集が AI の
  ノートの縁の色で見分けられる。

どのツールがどちらに入るか（`bridge.py` の `EDIT_TOOLS` / `SAVE_TOOLS` / `category()`。`tests/test_bridge.py` が全ツールの入れ忘れを見る）:

| 許可 | ツール |
|---|---|
| **編集** | `set_lyrics`・`import_lyrics`・`set_note_syllable`・`shift_pitch`・`set_pitch_curve`・`move_note`・`stretch`・`move_boundary`・`correct_to_guide`・`set_transition`・`split_note`・`merge_notes`・`apply_plan`・`set_connection`・`mute_notes`・`set_fade`・`reset_to_original`・`undo`・`redo`・`add_track`・`remove_track`・`set_track`・`set_guide_track`・`set_tempo`、`close_project(discard=true)`（保存していない変更を捨てる） |
| **保存・書き出し** | `save_project`・`export_wav`・`prepare_asr_model`（聞き取り用の数 GB のモデルをダウンロードして書く）、`render_region(path=…)`・`export_view_data(path=…)`・`render_preview(name=<フォルダーを含むパス>)`（ユーザーが指定した場所に書くとき） |
| AI からは呼べない | DAW のプラグイン専用の `ara_*`（§3-4。`bridge.py` の `ARA_TOOLS`。`permission: "ara"` で断る） |
| 許可なしで呼べる | 開く・作る・閉じる（`open_project`・`new_project`・`load_project`・`project_status`・`close_project()`）、読む・測る（`analyze_take`・`list_notes`・`get_pitch`・`list_deviations`・`get_phonemes`・`get_lyrics`・`list_utterances`・`inspect_lyrics_score`・`list_connections`・`list_changes`・`list_tracks`・`select_track`・`plan_edit`）、聞き取り（`transcribe`＝候補を返すだけ・`asr_status`）、プロジェクトの中の一時ファイル（`render_preview`・`render_region`・`render_audition`・`render_view`・`remeasure`・`export_view_data`・`track_overview`・`render_tracks`）、ジョブ（`get_job`・`cancel_job`）・裏の準備（`prep_status`・`pause_prep`）・`engine_info` |

`select_track` は編集対象を切り替えるだけ（履歴に入らない）なので許可なし。`plan_edit` は計画を作るだけで、当てるのは `apply_plan`（編集）。
`transcribe` は候補を返すだけで確定の歌詞を変えない（取り込む `set_lyrics` / `set_note_syllable` が編集）。`prepare_asr_model` は大きなダウンロードを AI が勝手に始めないよう「保存・書き出し」に入れている。

## 5. 会話の例

```
> C:\曲\take.wav を開いて、
  guide.wav をガイドにして、どこがどれだけずれているか教えて

  open_project → analyze_take → list_deviations
  「14 個の音符すべてがしきい値超えです。ずれの中央値は約 490 セント（≒5 半音）で、
   テイクの方が一貫して低い。タイミングのずれは中央値 5 ms でほぼ揃っています」

> 7 割ガイドに寄せて、図を見せて

  correct_to_guide(pitch_strength=0.7) → remeasure → render_view
  「ずれの中央値は 490 → 149 セントになりました。図はこちら（PNG のパス）」

> やっぱり戻して

  undo()

> 149.19〜150.56 秒は「さくらさくら」、153.22〜153.97 秒は「やよいのそらは」。
  そこだけ歌詞を付けて

  set_lyrics(entries=[{"start_sec": 149.19, "end_sec": 150.56, "text": "さくらさくら"},
                      {"start_sec": 153.22, "end_sec": 153.97, "text": "やよいのそらは"}])
  「2 区間・20 音素にアラインしました（確信度 0.70）。区間の外には音素を付けていません」

> 書き出して

  export_wav()
  「…\Media\vo_ve.wav に書きました。48 kHz / PCM_24 / 158.007 秒で
    元と同じです。差し替えたのは 148.90〜150.96 秒と 152.80〜154.77 秒だけで、
    そのほかのサンプルは元のファイルと完全に同じです」

> 歌詞は「さくらさくら やよいのそらは」。
  子音は伸ばさずにタイミングだけガイドに 70% 寄せて

  set_lyrics("さくらさくら やよいのそらは")
  「26 音素・14 音節にアラインしました（確信度 0.75）。
    母音が 1 つ 198 ms あるので制御点を足せます」
  correct_to_guide(pitch_strength=0, timing_strength=0.7)
  「母音・息の 20 区間だけ伸縮しました。子音 14 個は長さを変えていません。
    境界のずれは中央値 10.9 ms → 3.3 ms です」

> 「ら」の頭を 7 ms 遅らせて

  get_phonemes(start_sec=0.5, end_sec=0.9)   ← 境界の id を調べる
  move_boundary(boundary_id="b012", ms=7)
  「r の長さが 10.9 → 17.9 ms、a が 197.8 → 190.8 ms になりました」
```

## 6. 段階1で入っていないもの

| 項目 | 理由 |
|---|---|
| ~~音素（`get_phonemes`）~~ | **段階2 で実装済み**（HubertFA v0.0.7 ONNX + pyopenjtalk-plus） |
| openvpi/GAME による音符分割 | **重みが CC BY-NC-SA 4.0（非商用）** でライセンス方針に反する（コードは MIT だが重みが使えない） |
| ニューラルの高品質モード | PC-NSF-HiFiGAN の重みも非商用。商用可の経路（SingingVocoders で自前学習など）が要る |
| ~~画面の歌詞・音素レーン~~ | **段階2 で実装済み**（かな 1 段＋音素 1 段、境界のドラッグで `move_boundary`） |
| ~~WAV の書き出し~~ | **実装済み**（`export_wav`） |
| ~~区間ごとの歌詞~~ | **実装済み**（`set_lyrics(entries=[...])`。同上） |
