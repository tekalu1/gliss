# DAW 連携 段階 0: エンジン入力の一般化と再合成時間の実測

issue #3。背景は VST3 / ARA 2 の調査（§5.2 段階 0・§5.3 ARA に着手する条件の (iv)「1 回の編集の再合成が 1 秒以内」。調査の文書は公開リポジトリに含めていない）。

> この文書の計測は手元の曲（158 秒）とテスト素材（`docs/testing.md`）で行った。計測スクリプトは素材に結び付くので公開リポジトリには含めていない。

計測環境: AMD Ryzen 9 7950X（16 コア）・64 GB・Windows 11・Python 3.13.11（`.venv`）・praat-parselmouth 0.4.7（Praat 6.1.38）。
素材は段階 0 のクリップ C（3.84 秒、48 kHz モノラル PCM_24）と、曲全体 158.007 秒（`engine/tests/test_production_song.py` の SONG。48 kHz モノラル PCM_24。歌はアウトロの約 10 秒だけ、ノート 41 個）。

## 0. まとめ

| 項目 | 結果 |
|---|---|
| 1. 入力の一般化 | ソース（WAV ファイル／サンプル列）＋ソース内オフセット＋ソース ID（`media.py`）。プロジェクトの秒はクリップの頭が 0。WAV を開くのはその特別な場合で、既存の project.json（schema 1）もそのまま開ける。ファイル全体の書き出しは（伸縮を含まない編集なら）main とビット単位で同じ |
| 2. 区間 → PCM と再合成時間 | `render_region`（長さ不変・中身は書き出しと同じ）と `EditCache`（変わった窓だけ再合成）。**Praat で 1 ノートの編集 9〜15 ms、範囲 30 秒でも 0.10 s、MCP 越しの往復 12 ms**（158 秒の曲）。§5.3 の条件 (iv)「1 秒以内」を満たす。途中で見つけた Praat の伸縮の非決定性を直した |
| 3. 編集リストのアーカイブ | `to_archive` / `from_archive`。UI・マシンの状態を含まない JSON（曲全体・有効な編集 63 件＝ changeset 194 個で 89 KB、zlib で 5.3 KB）。別の場所・別の渡し方（サンプル列）に戻しても編集と PCM がサンプル単位で同じ |
| 4. 単体 exe | torch 無しで PyInstaller。**フォルダ形式 376 MB（zip 152 MB）・起動 0.69 s**（Python と同じ）、1 ファイル 152 MB・起動 3.25 s。torch は読み込まず同梱もしない |

テスト: engine の pytest 176 件が通る（ほか 2 件は worktree に `stage0/out/psola` が無くて skip。本体では走る）、app の Playwright 48 件が通る（main を取り込んだ後。worktree のエンジンで。`VOCAL_ENGINE_CWD`）。

## 1. 入力の一般化（ソース＋ソース内オフセット＋ソース ID）

### 何をしたか

エンジンの入力を「曲頭 0:00 の WAV ファイル」から、ARA のモデル（オーディオソース ⊃ リージョン）に写せる形にした（`engine/vocal_engine/media.py`）。

| エンジン | ARA | 意味 |
|---|---|---|
| ソース（`source_id`・`path`・`source_frames`） | `ARAAudioSource`（persistentID・サンプル数） | 元の音声 1 本。**WAV ファイル**か**サンプル列**（`media.Samples`） |
| クリップ（`offset_frames`・`frames`） | audio modification / playback region のソース側の範囲 | ソースのうち編集する範囲（ソースの頭からのサンプル数と長さ） |

- `Project.open(take, guide)` の `take` / `guide` に、今までどおりの **WAV のパス**、`media.Samples(samples, sr, source_id)`、`media.Clip(source, offset_frames|offset_sec, length_frames|length_sec, source_id)` のどれでも渡せる。
- **プロジェクトの時間はクリップの頭が 0。** 解析（F0・ノート・音素）・編集リスト・画面の秒はすべてクリップ内の秒。ソース上の秒は `offset_sec` を足す（`Project.source_sec(t)`）。解析・画面のデータ・プレビューが音を読むのは `Project.audio()` にそろっていたので、そこでクリップの範囲だけを読むようにすれば下流は変えずに済んだ。書き出しは元のビットを保つため整数のまま別に読むので、同じくクリップの範囲に切るようにした（`render/export.py`）。
- **「WAV ファイルを開く」はその特別な場合**（オフセット 0・長さ＝ファイル全体・ID＝`sha256:<中身の SHA-256 の頭 16 桁>`）。プロジェクトのディレクトリ名もファイル全体なら段階 2 までと同じ（`<名前>-<sha8>`）。一部なら `-o<オフセット>-n<長さ>` が付く。
- **サンプル列**はプロジェクトの `sources/<ID を英数字化して 40 字まで>-<ID のハッシュ 8 桁>.wav` に型を保って書き出して（既定の ID は `samples:<中身のハッシュ 16 桁>`）（float32 → FLOAT、int16 → PCM_16 …）、以降はファイルと同じに扱う。ARA でもエンジンは別プロセスなので（`research-vst-ara.md` §3.1 案 (a)）、プラグインは一時 WAV か共有メモリで渡すことになる。今は一時 WAV を受け口にした。
- `project.json` は **schema 2**。`take` / `guide` に `source_id`・`source_kind`・`source_name`・`source_frames`・`source_duration_sec`・`offset_frames`・`offset_sec` を足しただけで、既存のキー（`path`・`sha256`・`frames`・`duration_sec` …）の意味は変えていない（`frames` / `duration_sec` はクリップの長さ）。**schema 1（段階 2 まで）の project.json はそのまま開ける**（足りないキーはオフセット 0・全体で補う。`media.normalize_media`）。
- 書き出し（`export_wav`）: クリップなら既定は**クリップの長さ**の WAV（返り値の `start_sec` がソース上の開始秒）。`full_source=True` で**ソースと同じ長さ**（クリップの範囲だけ差し替え、外はソースのサンプルそのまま＝DAW のトラックに 0:00 で置けば位置が合う）。ファイル全体を開いたときの書き出しは、main とビット単位で同じことを確かめた（モノラル・ステレオ、ピッチと鉛筆の編集）。
- MCP: `open_project` に `offset_sec` / `length_sec` / `source_id`（ガイドにも `guide_offset_sec` / `guide_length_sec`）、`export_wav` に `full_source`。既存の引数は名前も既定値も変えていない。画面（Electron）は今までどおりファイル全体を開く（画面の動作は変えていない）。
- ついでに直したもの:
  - 同じディレクトリで素材（中身か範囲）が変わって作り直すとき、**前の素材の解析キャッシュを消す**ようにした（今までは `project_dir` を指定して別の素材を開くと古い `cache/take-analysis.json` を読んでいた）。
  - ガイドの同一判定をパスの一致から中身（SHA-256）と範囲の一致にした（同じファイルが移動しただけなら編集リストを捨てず、パスだけ直す）。
  - **テイクが同じでガイドだけ違うとき（ガイドを後から開いた・差し替えた）に、プロジェクトを作り直して編集リストを捨てていた**のを直した（main でも再現。画面で編集してから「ファイル > ガイドを開く…」をすると編集が全部消えていた）。今は編集リストとテイクの歌詞を残し、ガイドとその解析（DTW・ガイドの音素）だけ入れ替える。
  - `project.json` が壊れていて読めないときは、`project.json.broken-<時刻>` に退避してから作り直す（今までは例外で止まっていた。黙って上書きはしない）。
  - 重みの置き場の環境変数を **`VOCAL_ENGINE_MODELS_DIR` に統一**。F0 は `VOCAL_ENGINE_MODELS_DIR`、音素（HubertFA）は `VOCAL_ENGINE_MODELS`、stage0 の RMVPE（回帰テストが使う）は環境変数を見ていなかった。エンジンは `vocal_engine/config.py` の `models_dir()` に寄せ、旧名 `VOCAL_ENGINE_MODELS` も読む（新しい名前が優先）。単体 exe では exe の隣の `models/` を既定にする。stage0 の RMVPE は `VOCAL_ENGINE_MODELS_DIR` だけを見る（PR #9 と同じ変更にそろえた）。

### 確かめたこと（`engine/tests/test_daw_stage0.py`）

- ファイルを開く＝オフセット 0・全体・ID は SHA-256 から。ディレクトリ名は段階 2 までと同じ。
- クリップの音はソースの切り出しとサンプル単位で一致。クリップ（0.5〜3.0 秒）の F0 は、ファイル全体の F0 を 0.5 秒ずらしたものと有声判定 95 % 以上一致・差の中央値 5 セント未満（RMVPE は 10 ms ホップで 0.5 秒がちょうど 50 フレーム。端だけ違う）。ノートの頭も 30 ms 以内でそろう。
- float32 のサンプル列で渡すと、同じ中身の PCM_24 ファイルと**同じ音・同じノート**。同じサンプル列で開き直すと同じプロジェクト（編集が残る）。
- クリップの書き出し: クリップの長さ／ソースと同じ長さの両方で、差し替えた区間の外は元のサンプルと整数で一致。ソースと同じ長さの書き出しのクリップ部分は、クリップの長さの書き出しと一致。
- schema 1 の project.json（ソースのキーを消したもの）を `load` でも `open` でも開けて、編集が残る。
- MCP の `open_project(offset_sec, length_sec, source_id)` → `analyze_take` → `render_region`。
- 範囲がソースの外ならエラー。環境変数の新旧の優先順。
- 編集してからガイドを開く・別のガイドに差し替える・ガイドを省いて開き直すで、編集リストと歌詞が残り、前のガイドの DTW は捨てて取り直す。
- float のサンプル列は、ソース ID を変えて渡し直しても、`sources/` の WAV が消えていても、中身が同じなら編集リストが残る（float の WAV は libsndfile が PEAK チャンクに時刻を書くので、書くたびにファイルの SHA-256 が変わる。サンプル列は中身のハッシュ `content_sha256` で同一か見る）。ID が加工後に同じになるテイクとガイドが別のファイルになる。対応していない型（int64 など）はエラー。
- 壊れた project.json は退避してから作り直す。新しい版（schema > 2）は読まない。

## 2. 区間 → PCM の再合成 API と、1 回の編集の再合成時間

### API（`engine/vocal_engine/render/region.py`）

| 名前 | 何をするか |
|---|---|
| `RegionRenderer(x, sr, f0r, backend)` / `.for_project(p, channels="mono"\|"all")` | チャンネルごとのレンダラ（バックエンドの下ごしらえ）を持ち回す。多チャンネルの praat はモノラル化した音で解析・ゲイン・クロスフェードの相関を共有（`export_wav` と同じ。書き出しもこれを使うようにした） |
| `render_region(p, start_sec, end_sec)` → `(y, info)` | 編集を当てた区間の PCM。**長さが変わらない**（[a, b) を頼めば b − a サンプル）。中身は **`export_wav` が同じ範囲に書くものと同じ**（掛かる窓を丸ごと再合成して切り出す）。`info` にソース上の開始サンプル・再合成した窓・掛かった秒 |
| `dirty_windows(p, segs_before, segs_after)` | 1 回の編集（か undo）で差し替え直す範囲。変わった Segment に掛かる窓から始めて、重なる窓を編集の前後両方から足していく |
| `EditCache(p)` / `.prepare()` / `.update()` | クリップ全体の「編集を当てた PCM」を持ち、編集のたびに**変わった窓だけ**再合成して差し替える（ARA の PlaybackRenderer がキャッシュを読むだけにする形の試作）。`update()` の返り値の `timing_sec` が「1 回の編集の再合成時間」 |
| MCP `render_region(start_sec, end_sec, backend, channels, path)` | 上をファイル（32 bit float の WAV）で返す。32 ツール目 |

`render_preview`（聞いて確かめる用）は範囲の端がノートの途中でもそこで切って再合成するので、伸縮があると長さが変わる。DAW に渡す PCM はキャッシュの差し替えに使うので「長さ不変・書き出しと同じ中身」が要る。そこで書き出しの窓（編集のかたまりごと、端は前後 1 秒の中でいちばん静かなところ）をそのまま単位にした。

テスト: 6 種類の編集（ピッチ・鉛筆・分割・伸縮・つなぎ・undo）を順に当てて、(a) `render_region` の全体と途中で切った 3 区間が書き出しと整数で一致、(b) `EditCache` を編集ごとに差分更新した結果が毎回、書き出しと整数で一致し、差分更新の範囲がクリップ全体より短い（分割は音が変わらないので 0）。

#### 見つけた不具合: Praat の伸縮が毎回違う音になる

(b) を作る途中で、**伸縮（stretch）を含むと、同じ編集リストの書き出しを 2 回すると違うファイルになる**ことが分かった（main でも同じ。差は最大でフルスケールの約 7 %）。Praat の `Get resynthesis (overlap-add)` は長さを変えるとき、無声の区間を乱数で選んで継ぐため。差分更新のキャッシュ（再合成し直した窓の隣が前の乱数の結果）・オフラインのバウンスと再生の一致・ステレオの左右（チャンネルごとに別の乱数）のどれにも困るので、**区間（サンプル位置）と伸縮比から決まる種で毎回 Praat の乱数を初期化**するようにした（`render/praat.py` の `_seed_praat`。`random_initializeWithSeedUnsafelyButPredictably`）。音の性質（無声区間をランダムに継ぐ）は変わらず、同じ区間は常に同じ音になる。ピッチだけの編集は元から決定的で、書き出しは main とビット単位で同じ。

### 実測（各 5 回の中央値）

「1 回の編集」＝ **ツールの呼び出し**（編集リストに足して project.json を書く）＋ **`EditCache.update()`**（Segment の組み直し → 変わった窓を探す → その窓だけ再合成）。下ごしらえ（素材を受け取った時点で 1 回）は別に書く。モノラル。

下ごしらえ（`RegionRenderer.prepare`。Praat なら素材全体のパルスと PitchTier、自前 psola なら素材全体の解析。素材ごとに 1 回）:

| 素材 | praat | psola |
|---|---|---|
| クリップ C（3.84 秒） | 0.04 s | 0.64 s（プロセスで最初の 1 回。import などが入る） |
| 曲全体（158 秒） | 0.26 s | 0.28〜0.29 s |

1 回の編集（秒。カッコ内は再合成し直した窓の長さ）:

| 素材 | 編集 | praat | psola |
|---|---|---|---|
| C（3.84 秒） | 1 ノートのピッチ | **0.013**（2.06 秒） | 0.023 |
| | 1 ノートの伸縮 1.2 倍 | **0.015**（2.06 秒） | 0.023 |
| | 1 ノートに鉛筆 | **0.014**（2.06 秒） | 0.024 |
| | 全体のピッチ | **0.032**（3.84 秒） | 0.101 |
| | 13 ノート編集済みでさらに 1 ノート | **0.036**（3.74 秒） | 0.082 |
| 曲全体（158 秒） | 1 ノートのピッチ | **0.009**（0.97 秒） | 0.008 |
| | 1 ノートの伸縮 1.2 倍 | **0.014**（0.97 秒） | 0.012 |
| | 1 ノートに鉛筆 | **0.009**（0.97 秒） | 0.008 |
| | 範囲 10 秒のピッチ | **0.069**（11.1 秒） | 0.272 |
| | 範囲 30 秒のピッチ | **0.102**（31.2 秒） | 0.666 |
| | 40 ノート編集済みでさらに 1 ノート | **0.087**（11.0 秒） | 0.216 |

- 内訳はほぼ全部が窓の再合成。ツールの呼び出し（project.json の書き込み込み）は 1〜7 ms、Segment の組み直しと窓探しは 1〜4 ms。undo でその範囲が原音に戻るだけなら（再合成が要らないので）1 ms 以下、編集が残る窓があればその再合成ぶん（40 ノート編集済みで 0.09 s）。
- **窓は「編集のかたまり」単位**（0.5 秒より近い編集はまとめ、端は静かなところ）なので、編集が密なフレーズでは 1 ノートの編集でもフレーズ全体（上の 40 ノートの例で 11 秒）を再合成し直す。それでも praat で 0.1 秒以下。
- 参考: 曲全体の書き出し（40 ノート編集済み、差し替え 11 秒）は praat 0.81 秒・psola 0.99 秒（ファイルの読み書き込み）。

**別プロセス（MCP stdio）越しの往復**（Python のエンジン、曲全体、1 ノートのピッチ）: `shift_pitch` 2 ms ＋ `render_region`（ノートの前後 1 秒・2.2 秒ぶんを WAV で受け取る）10 ms ＝ **12 ms**（5 回の中央値。最大 0.26 s）。最初の `render_region` は下ごしらえ込みで 0.45 s。エンジンの起動（initialize が返るまで）は 0.73 s（初回 1.37 s）。

→ **§5.3 の条件 (iv)「1 回の編集の再合成が 1 秒以内」は、Praat で 1 ノートなら 9〜15 ms、30 秒の範囲でも 0.1 秒で満たす。** 下ごしらえ（158 秒で 0.26 s）は素材を受け取ったときに 1 回。ボトルネックは再合成ではなく、初回の解析（RMVPE。158 秒で約 3.4 秒、キャッシュ後は 0.02 秒）と、エンジンの起動。

## 3. 編集リストの UI 非依存のシリアライズ・復元

### 何をしたか

もともと編集の状態は `project.json` に JSON で持ち、画面（Electron）の状態（表示範囲・最近使ったファイル・ウィンドウ位置）は画面側の `state.json`（userData）に別に持っていた。つまり**編集リストは元から UI と無関係**。ただ `project.json` にはマシンに依存するもの（プロジェクトのディレクトリ、解析キャッシュのパス、更新時刻）も入っているので、ARA のアーカイブ（ホストのドキュメントに埋め込む単位）に入れるものだけを取り出す API を足した（`project/store.py`）。

- `Project.to_archive()` → dict（そのまま JSON にできる）:
  - `format`（`"vocal-editor-archive"`）・`version`（1）・`engine_schema`（2）
  - `take` / `guide`: 素材の参照（`source_id`・`source_kind`・`source_name`・`sha256`・`clip_audio_sha256`・`sr`・`channels`・`subtype`・`source_frames`・`offset_frames`・`frames`・手がかりの `path`）
  - `lyrics`（区間ごと）・`align_method`・`seq`（編集と changeset の採番）・`changesets`（**取り消し履歴ごと**。有効な編集リストは changeset を順に当てて作り直す）
  - 入れないもの: ディレクトリ・解析（F0・ノート・音素は素材から作り直せる）・画面の状態・ログ・更新時刻
- `Project.from_archive(archive, take=…, guide=…, project_dir=…)`: 素材（パス・`Samples`・`Clip`。ARA ならホストのオーディオソース）を渡して戻す。範囲を渡さなければアーカイブの範囲を使う。**素材の中身がアーカイブと違えばエラー**（別の音に編集を当てない）。置き場に**別の編集履歴を持つ**プロジェクトがあってもエラー（`overwrite=True` で上書き。省略時の置き場は画面と同じ既定のディレクトリなので、画面で編集中のものを黙って消さない）。照合はディレクトリに触る前に行う。まずクリップの範囲（オフセット・長さ）が一致すること。次に、ファイルの SHA-256 が同じならそれで一致とする。違えば、クリップの音を float64 で読んだサンプル列のハッシュ（`clip_audio_sha256`）で比べる（同じ音を PCM_24 の WAV で渡しても float32 のサンプル列で渡しても同じ値になる）。

### 確かめたこと（`test_daw_stage0.py`）

- 6 種類の編集（ピッチ・鉛筆・分割・伸縮・つなぎ・undo）を当てたクリップ（ソース ID 付き）とガイドのプロジェクトを `to_archive` → JSON 文字列 → `from_archive`（**別のディレクトリ**）で戻すと、編集リスト・changeset・Segment・**再合成した PCM がサンプル単位で同じ**。戻したものをもう一度 `to_archive` すると同じ JSON（パス以外）。undo した changeset も戻り、redo できる。
- 素材を **float32 のサンプル列**（パス無し）で渡して戻しても同じ PCM。
- アーカイブに UI・マシンの状態のキー（`dir`・`analysis`・`updated_at`・表示範囲など）が入っていない。
- オフセット ≠ 0 のクリップでも往復できる。同じファイルでも**別の範囲**を渡すとエラー。
- 別の素材・別の形式のアーカイブはエラーで、`overwrite=True` でも既存のプロジェクトに触らない。別の編集履歴のあるディレクトリへは `overwrite=True` が無ければエラー（元の履歴は残る）。

### 実測（曲全体 158 秒、41 ノートのピッチ＋11 ノートの伸縮。伸縮は後ろをずらさない組み直しで複数の編集になるので、有効な編集は 63 件。計測の繰り返しで undo 済みのものも含めて changeset 194 個。一時スクリプトで手で測った）

| 項目 | 値 |
|---|---|
| JSON の大きさ | 89 KB（zlib で 5.3 KB） |
| `to_archive` | 0.54 s（ほぼ全部がクリップの音のハッシュ。158 秒をクラウドの仮想ドライブ上のファイルから読む） |
| `from_archive`（別のディレクトリ・解析のキャッシュ無し） | 0.06 s ＋ 解析（RMVPE）3.6 s |
| `from_archive`（解析のキャッシュあり） | 0.08 s |
| 戻したものの PCM（140〜158 秒） | 元とサンプル単位で同じ |

→ ARA のアーカイブには**編集リスト（数十 KB の JSON）だけを入れれば足りる**。解析は素材から作り直せる（158 秒で 3.6 秒。ドキュメントを開いた直後に裏で解析し、終わるまでは原音を返せばよい）。解析のキャッシュをアーカイブに入れる（数 MB）かどうかは、段階 3 でホストの読み込み時間を見て決める。

## 4. エンジンの単体 exe 化（サイズ・起動時間・torch 非依存）

### 何をしたか

- PyInstaller 6.22.3 で MCP サーバーを固めた（`engine/packaging/vocal-engine.spec`・入口 `vocal_engine_entry.py`）。本体の `.venv`（CUDA の torch 入り、4.2 GB のうち torch が 2.9 GB）は使わず、worktree に **torch を入れない別の venv**（`.venv-exe`、`pyproject.toml` の必須依存だけを本体と同じ版で。0.55 GB）を作って固めた。生成物（`build/`・`dist/`）と `.venv-exe/` は `.gitignore` に入れた。
- `vocal-engine.exe --check` は依存の版と、**torch を読み込んでいないか・import できるか**を JSON で出す（どちらも false）。`dist/` に torch のファイルは無い。
- 重みは同梱しない。exe の隣の `models/` か `VOCAL_ENGINE_MODELS_DIR`（§1 で統一した `config.models_dir()`）。
- 作り方:

```
uv venv .venv-exe --python 3.13
uv pip install --python .venv-exe\Scripts\python.exe numpy==2.4.6 scipy==1.18.1 soundfile==0.14.0 librosa==0.11.0 ^
    numba==0.65.1 llvmlite==0.47.0 soxr==1.1.0 matplotlib==3.11.2 onnxruntime==1.30.0 mcp==2.2.0 praat-parselmouth==0.4.7 pyinstaller
cd engine\packaging
..\..\.venv-exe\Scripts\pyinstaller.exe vocal-engine.spec --noconfirm              # フォルダ形式
set "VE_ONEFILE=1" && ..\..\.venv-exe\Scripts\pyinstaller.exe vocal-engine.spec --noconfirm   # 1 ファイル
```

- ハマったところ:
  - `collect_submodules("mcp")` が `mcp.cli`（typer が要る）を import して止まる → `mcp.cli` を除いた。
  - **exe が onnxruntime の import で落ちる（Segmentation fault）。** PyInstaller が `msvcp140.dll` を PATH 上の JDK 11 から拾っていて（14.16、古い）、onnxruntime 1.30 は 14.3x 以上が要る。spec で VC++ ランタイムを System32 のもの（14.50）に差し替えた。配布するなら VC++ 再頒布可能パッケージの版を決めておくこと。
  - librosa は lazy_loader で遅れて import するので `collect_submodules("librosa")` とデータ（`.pyi`）が要る。

### 確かめたこと

- torch の無い `.venv-exe` で engine の pytest を回すと、**任意依存（pyworld・pyopenjtalk）が要る 7 件以外はすべて通る**（MCP stdio のテストも `.venv-exe` の python で起動して通る）。失敗の 7 件は、回帰テストが段階 0 の測定（`stage0/measure.py`。WORLD を使う）を借りている 6 件と、漢字の読み（pyopenjtalk）1 件。
- exe（フォルダ形式）を MCP で起動して `open_project`（クリップ指定）→ `analyze_take`（RMVPE。解析し直し）→ `set_lyrics`（HubertFA で 31 音素）→ `shift_pitch` → `export_wav`（Praat）→ `render_view`（matplotlib の PNG）まで通る。

### 実測（起動は 5 回、編集は 5 回の中央値。曲全体 158 秒）

| | Python（`.venv`、`python -m vocal_engine.mcp`） | exe（フォルダ形式） | exe（1 ファイル） |
|---|---|---|---|
| 大きさ | —（venv 4.2 GB。torch 抜きなら 0.55 GB） | **376 MB**（1,000 ファイル。zip で 152 MB） | **152 MB** |
| 起動（initialize が返るまで） | 0.73 s（初回 1.37 s） | **0.69 s**（初回 1.19 s） | 3.25 s（毎回展開するので初回 3.66 s） |
| 依存をすべて import（`--check`） | — | 1.66 s | — |
| 最初の `render_region`（下ごしらえ込み） | 0.45 s | 0.44 s | 0.54 s |
| 1 回の編集の往復（`shift_pitch` ＋ `render_region` 2.2 秒ぶん） | 12 ms | 13 ms | 15 ms |

フォルダ形式の内訳（MB）: llvmlite 102・scipy 49（＋ scipy.libs 20）・onnxruntime 36・parselmouth 32・numpy 6（＋ numpy.libs 21）・matplotlib 15・scikit-learn 13・PIL 13・cryptography 10（＋ libcrypto 8）・python313.dll 6。

→ **torch に依存せずに固められる**（エンジンが torch を使うのは任意の FCPE だけで、関数の中で import している）。フォルダ形式なら起動は Python と同じ 0.7 秒、編集の往復も同じ。**1 ファイル形式は毎回 150 MB を展開するので起動が 3 秒を超え、プラグインの子プロセスには向かない**（使うならフォルダ形式）。大きさの 1/3 強は **librosa が引き込む numba / llvmlite（102 MB）と scikit-learn（13 MB）**で、librosa を使っているのは RMVPE の前処理（メルフィルタと STFT）と MFCC の DTW（ガイドとの対応付け）だけ。ここを numpy / scipy で書き直せば 250 MB 前後まで減らせる見込み（未検証）。


## 5. 判断と残課題

### 判断

- **プロジェクトの時間軸はクリップの頭を 0 にした**（ソースの頭ではなく）。エンジンの下流（解析・編集・画面のデータ・書き出しの窓）がすべて「素材の頭 = 0」で書かれていて、音を読む入口（`Project.audio()` と書き出しの読み込み）を変えるだけで済むため。ARA の playback region はさらにホストのタイムライン上の位置（と伸縮）を持つが、それはプラグイン側で「ソース上の秒 = offset + クリップ内の秒」から写せばよい。
- **サンプル列はプロジェクトの `sources/` に WAV で書いてから扱う**。エンジンは別プロセス（案 (a)）の予定なので、どのみちプラグインからはファイルか共有メモリで渡す。書き出し（整数のまま差し替える）・アーカイブの照合・解析キャッシュが今のファイル前提の仕組みのまま使える。共有メモリは段階 3 で必要になってから。
- **区間 → PCM の単位は書き出しの窓**（編集のかたまりごと、端は静かなところ）。任意の区間で切って再合成すると、切り口で Praat の周期の位相がずれ、書き出しと違う音になる。窓を単位にすれば「差分更新のキャッシュ＝書き出し＝再生」がサンプル単位で一致する（テストで確認）。
- **Praat の乱数は区間から決まる種で初期化**。同じ区間は常に同じ音にする方が、キャッシュ・バウンス・ステレオの左右の一貫性で得。
- **exe はフォルダ形式**。1 ファイル形式は毎回の展開で起動が 3 秒を超える。

### 残課題

1. **画面（Electron）でクリップ（ファイルの途中）を開く操作は無い**。エンジンと MCP（`open_project(offset_sec, length_sec)`）だけ。画面は issue #1・#2 の作業中なので触っていない。画面のデータ（`view/export_data.py`）の `take.name`、`render_view` の題（`view/piano_roll.py`）、`analyze_take` の要約はソースのファイル名のまま（サンプル列なら `sources/` の WAV）で、クリップの位置も出していない。
2. **1 プロジェクト＝1 クリップ**。ARA のように 1 つのソースに複数のリージョン（それぞれ別の編集）を持つ形や、同じソースの別の範囲で解析を共有する形は無い（範囲ごとに別のプロジェクト・別の解析になる）。
3. サンプル列の受け渡しは一時 WAV（158 秒のモノラル float32 で 30 MB を書く）。共有メモリは未実装。
4. `EditCache` は試作で、MCP にはまだ差分更新そのもののツールは無い（`render_region` を窓ごとに呼べば同じことはできる）。編集が密なフレーズでは 1 ノートの編集でも窓がフレーズ全体（実測で 11 秒）になる（それでも Praat で 0.09 s）。
5. いちばん重いのは**初回の解析**（RMVPE。158 秒で 3.4〜3.6 s）とエンジンの起動（0.7 s）。ARA では解析キャッシュをどこに置くか（アーカイブに入れるか、ソース ID ごとにディスクに持つか）を段階 3 で決める。
6. exe の大きさの 1/3 強は librosa 経由の numba / llvmlite。使っているのは RMVPE の前処理と MFCC の DTW だけなので、numpy / scipy に置き換えれば減らせる（未検証）。VC++ ランタイムの同梱の仕方・コード署名・ウイルス対策ソフトの誤検知は未確認。
7. **Praat の乱数の種を固定したので、伸縮を含む書き出しは main とは（無声区間の乱数のぶん）違う音になる。聴き比べが必要**（手法は同じで、main でも書き出すたびに同じ程度に変わっていたので差は無いはずだが、未確認）。ピッチだけの編集は main とビット単位で同じ。
8. **クリップの範囲がプロジェクトの同一性に入っている**（既定の置き場の名前 `-o…-n…` と、開き直すときの照合）。DAW でリージョンの頭を少しトリムしただけで別のプロジェクトになる。編集の秒がクリップ内の秒なので、範囲が変わると当て直せない。ARA では編集をソース全体に置いてリージョンを見方として扱う（編集をソースの秒で持つ）方が安全で、段階 3 の前に決める。
9. `project.json` の版: 新しい版（schema > 2）は読まずにエラーにした。逆に、main（schema 1 の）エンジンが schema 2 の**クリップの**プロジェクトを開くと、オフセットを無視してファイル全体として読み、編集の位置がずれる（既定の置き場はディレクトリ名が違うので、同じディレクトリを指定したときだけ）。
10. `align_method` は `project.json` に保存されていない（アーカイブから戻した値はそのプロセスの中だけ）。外部のプロセスが `project.json` のテイクを差し替えたとき、MCP サーバーのレンダラ（`render_preview` / `render_region`）は古い音のまま（main からある弱点）。
11. 計測はモノラルだけ。ステレオでは Praat がチャンネル数＋モノラルの参照ぶん再合成するので、2 チャンネルで 3 倍程度の見込み（未計測）。「main とビット単位で同じ」「main でも伸縮の差が約 7 %」はその場のスクリプトで確かめたもので、リポジトリにテストとしては入れていない。
12. `app` の `.mcp.json` はエンジンの `cwd` に本体の checkout を書いているので、worktree で画面のテストを回すと本体のエンジンが動く。`app/main.mjs` に `VOCAL_ENGINE_CWD` の上書きを足した（画面の見た目には関係しない）。**PR #9（issue #1・#2。main にマージ済み）と同じ変更**なので、`app/main.mjs`・`stage0/f0/rmvpe_onnx.py`・`engine/tests/conftest.py` の該当部分は PR #9 と同じ内容にそろえ、main を取り込んだ（衝突は README のテスト件数だけ）。
