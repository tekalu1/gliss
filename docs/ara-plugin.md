# VST3 + ARA 2 プラグイン

Gliss を DAW の中で使うためのプラグイン（`plugin/`）。DAW のオーディオイベントに ARA の拡張として載り、ホストの音を読み、編集を当てた音を返す。
issue は [tekalu1/gliss#1](https://github.com/tekalu1/gliss/issues/1)。作業の手順は [AGENTS.md](../AGENTS.md)。

状態（2026-10-03）: **段階 2（エンジンとの接続）と段階 3 の画面の橋まで**。DocumentController がエンジン（Python）を子プロセスで 1 本持ち、
DAW のイベントの音をエンジンに渡し、編集を当てた音を再生に乗せ、編集リストを DAW のソングに保存する（下の「エンジンとの同期」）。
エディタは `app/renderer` の画面を WebView2 で出し、`DocumentBridge` を通してエンジンを呼ぶ（下の「エディタ」）。
配布（インストーラがユーザーごとの VST3 の置き場に入れる・配布版のエンジンの見つけ方・ライセンスの表示）は段階 4 の一部として済み（下の「配布」）。
DAW にノートを返す（ARA の content reader、`kARAContentTypeNotes`）も段階 4 で足した（下の「DAW に返すもの」）。
実物の DAW（Fender Studio Pro 8 など）での確認はまだ。

## 決まった形

| 項目 | 内容 |
|---|---|
| 手段 | JUCE **9.0.3**（AGPLv3 の側で使う）＋ ARA SDK **2.3.0**（Apache-2.0）＋ WebView2 SDK（NuGet の `Microsoft.Web.WebView2` **1.0.4258.31**、静的リンク）＋ Signalsmith Stretch **1.4.0**・Signalsmith Linear **0.6.4**（ともに MIT）。退路は素の VST3 SDK ＋ ARA_Library |
| 形式 | VST3 のみ（`Gliss.vst3`）。ARA 2（`IS_ARA_EFFECT`）。出力は `plugin/build/GlissARA_artefacts/<構成>/VST3/Gliss.vst3` |
| 製品名・会社名 | `Gliss`・`Gliss`（ホストに見える名前） |
| `BUNDLE_ID` | `io.github.tekalu1.gliss`（アプリの appId と同じ。変えない） |
| `ARA_FACTORY_ID` | `io.github.tekalu1.gliss.arafactory.3`（線形の時間伸縮を名乗ったので `.2` から上げた） |
| `ARA_DOCUMENT_ARCHIVE_ID` | `io.github.tekalu1.gliss.aradocumentarchive.1`（保存の形が下位互換でなくなったら上げ、古い ID は `ARA_COMPATIBLE_ARCHIVE_IDS` に残す） |
| ARA の API の世代 | JUCE の既定（`kARAAPIGeneration_2_0_Final`）。部分的な保存（2.3）は使わない |
| 解析・変形の能力 | 解析はノートだけ（`ARA_ANALYSIS_TYPES kARAContentTypeNotes`）。再生は線形の時間伸縮（`kARAPlaybackTransformationTimestretch`）に対応。`TimestretchReflectingTempo` と内容に合わせたフェードは未対応 |
| 実行時ライブラリ | 静的（`/MT`）。利用者の PC に VC++ 再頒布可能パッケージを要求しない |
| WebView2 | ローダは静的リンク。ランタイムは Evergreen（Windows 11 に入っている）。無いときはエディタに文言を出す作り（ランタイムの無い環境では未確認） |
| 依存の取得 | CMake の `FetchContent` が `plugin/build/_deps` にタグ固定で取る（リポジトリには入れない。`plugin/.gitignore`） |

### 編集の単位・保存

- 編集の単位は ARA の `AudioModification`（`GlissAudioModification`）。エンジンのボーカルのトラック 1 本（`ara_id` = persistentID、範囲はソース全体、編集の秒はソースの秒）。
  同じ素材を複製・分割した `PlaybackRegion` は同じ `AudioModification` を共有する（編集と再生のキャッシュを共有する）。
  複製された修飾（DAW の「固有にする」・TestHost の `ModificationCloning`）は、エンジンに `clone_of` で複製元の編集を写す。
- アーカイブは **版つきの JSON 1 つ**（`doStoreObjectsToStream` / `doRestoreObjectsFromStream`、`plugin/src/ara/ArchiveIO.*`）。**編集リストだけ**（エンジンの `ara_archive` = `Project.to_archive()`）を書き、解析のキャッシュは入れない（作業場所に持つ）:

```json
{ "format": "gliss-ara", "version": 1,
  "document": { "work_key": "<作業場所の鍵>", "guide": "<ガイドの修飾の persistentID>" | null },
  "modifications": { "<persistentID>": { "name": "…", "archive": { …Project.to_archive()… } | null } } }
```

  - `work_key` は作業場所 `%LOCALAPPDATA%\Gliss\work\ara\<work_key>` の名前。新しいドキュメントで UUID を作り、アーカイブにあれば（まだエンジンで開いていなければ）それを使う（同じ PC なら前の解析のキャッシュが使える）。
  - `archive` の `f0_estimator` は、補正を作った F0 の方式（`rmvpe`・`gliss`・`praat`。未解析で明示も無ければ `null`）。
    再合成は F0 を使うので、方式が違うと音が変わる（実測: gliss で補正した編集を rmvpe で解析し直すと約 19 秒分が変わる）。
    `ara_restore` はこの方式をその修飾の方式にして、別の PC・別の作業場所で開き直しても同じ方式で解析する。
    プラグインが起動のたびに呼ぶ `set_f0_estimator`（設定 `plugin-state.json` の `f0Estimator`）は、方式の決まっていない新しい修飾の既定にだけ効く
    （エンジンは `GLISS_CLIENT=ara` のとき `scope=default` として扱い、修飾ごとの方式・前に解析した方式を外さない）。これを外すと、rmvpe で作った補正が
    開き直したときに gliss で解析し直されて、ノートの ID が合わず補正が当たらなくなる（描画が毎秒 `ノートが無い` で失敗し続ける）。
    方式の記録が無い・合っていないアーカイブは、`ara_render_dirty` がノートの ID に頼る編集の対象が全部ある方式を探して、その修飾の方式にする（`engine/docs/MCP.md`）。
    `ArchiveIO` は `archive` を不透明な JSON として持つだけなので、このキーは C++ を変えずに保たれる（`AraTests.cpp` が確かめる）。
  - 保存のときは、エンジンが動いていれば `ara_archive` を取り直してから書く（エンジンのロックを取らないので、解析の最中も待たない）。部分的な保存（`ARAStoreObjectsFilter`）では渡された修飾だけを書く。
  - 戻すと、修飾ごとの編集を**保留**し、ソースの音を読んで `ara_set_modification` した後に `ara_restore` で当てる。保留の間・素材が違って当てられなかった（`mismatch`）間に保存されると、保留のものをそのまま書く（読み込み直後に保存しても編集が消えない。利用者がその修飾を編集したら捨てる）。
  - `format` が違う・`version` が新しすぎるものは復元に失敗として返す。段階 1 の形（`gliss-ara-document`）は読まない（配布していない）。

### 再生（`GlissPlaybackRenderer`）

- オーディオスレッドは**キャッシュを読むだけ**: 修飾ごとの `EditedPcm`（編集した窓だけの PCM のスナップショット）を `tryLock` で取り、窓の中は窓の PCM、外は原音を返す。IPC・再合成・ホストの音の読み出し・確保・ロック待ちをしない。取れない・無い区間は原音。
- リージョンごとに `RegionReader`（`plugin/src/cache`）を `prepareToPlay` で用意し、窓＋原音＋周波数の変換＋チャンネル数の変換を 1 ブロックずつ読む。**ソースとホストの周波数が違っても鳴らす**（原音と窓を合わせたソースの周波数の列を流しで変換する。継ぎ目が出ない）。
- 伸縮フラグのあるリージョンは修飾の長さとソングの長さの比を一定の倍率として `StretchReader`（Signalsmith Stretch）で鳴らす。`RegionReader` が作った修飾の時間・ホストの周波数の音を伸縮し、シーク時は `outputSeek` で揃える。伸縮しないリージョンは従来の整数の切り出しと再生のまま。リージョンの途中でテンポが変わる場合の追従は未対応（`TimestretchReflectingTempo` は名乗らない）。
- 伸縮用の作業領域は `prepareToPlay` で最大ブロック長と 8 倍までの入力を基準に確保する。8 倍を超える比は出力を小分けにして処理し、シークの先読みは確保した長さで切る。入力が 1 出力サンプル当たりの確保量を超える極端な比では無音になる。
- 原音は**裏のスレッドが先読み**したもの（JUCE の `BufferingAudioReader`。全インスタンスで 1 本の `TimeSliceThread`。先読みは 4 秒）。先読みが間に合っていない範囲だけ無音にする。常にリアルタイムでないインスタンス（`alwaysNonRealtime`）は先読みせず直に読む。
- ホストが「リアルタイムでない」描画（VST3 の `kOffline`＝バウンス）のときだけ、原音の先読みを最大 500 ms、**同期（ソースの読み込み・エンジン・差分の再合成）の完了を最大 10 秒**待つ（`prepareToPlay` ごとの持ち時間。待っても済まなければ原音のまま描き、ログに書く）。
- 原音と比べる（`DocumentBridge::setCompare`）間は窓を当てない（全部の修飾の音が変わったとホストに知らせる）。
- ドキュメントの編集中（`willBeginEditing`〜`didEndEditing`）は、オーディオスレッドが待たずに（`ScopedTryReadLock`）そのブロックを無音にする。
- DAW の再生位置は `GlissProcessor::processBlock` が `PlayheadState` に書く。再生中は一つの processor を位置の正本に選び、停止中に別の再生中 processor が現れた場合や更新が途切れた場合だけ切り替える。複数の値は連番の前後一致で同じブロックから読み、エディタへ `song_sec`・`stamp_ms`・`sequence` として 30 Hz で送る（ループの PPQ はその位置の BPM で秒に直す）。画面は通知時刻との差から配送の揺れを補正し、補間を 55 ms に制限する。ソングの秒は上段・時計の正本、リージョンに写したソースの秒は下段の表示だけに使う。
- `EditorRenderer` はエンジンの `render_audition` が作った補正後の WAV を作業スレッドで読み、DAW の音声出力へ足す。音声コールバックは準備済み PCM を読むだけでロック・IO・確保をしない。ループ端、開始、ピッチ差し替え、離したときは 6 ms でフェードし、DAW の通常再生中とオフライン描画には足さない。離す・フォーカス喪失・ホストの再生開始・文書破棄・native editor の破棄では準備中のリクエストも世代番号で取り消す。短いホスト再生が画面への通知の間に終わっても、音声コールバックの取消印で古い試聴を再開させない。同一ドキュメントの複数 EditorRenderer は最初に出力処理した 1 つだけが試聴を加算し、所有者が消えるか 250 ms 更新しなければ別の renderer に渡す。`bootstrap.preview` は EditorRenderer がある場合に保存した設定（未設定ならオン）を返す。`preview('start')` は PCM を公開してから `{ok:true}`、利用できないときは `{ok:false,reason}` を返す。
- ARA に結び付かない（普通の VST3 として読み込まれた）ときは、入力をそのまま通す。

### エンジンとの同期（`plugin/src/ara/DocumentSync.*`）

DocumentController がエンジン（`McpClient`。`plugin/src/engine`）を **1 本**持つ。同期のスレッドも 1 本で、メッセージスレッドは ARA の編集サイクルの後（`didEndEditing`・読み出しの許可の変化・音の変化）に「あるべき形」（ソース・修飾・リージョンの時間）を渡すだけで、エンジンを待たない。同期のスレッドは次を繰り返す（engine/docs/MCP.md §3-4 の順）:

1. エンジンの遅延起動（最初のソースの読み出しが許されたとき、または画面が開いたとき）→ `engine_info` → `ara_open(work_key)`。
2. ソースの音をホストから読み（`ARAAudioSourceReader`。リーダーはメッセージスレッドで作り・壊す）、`<作業場所>\ara-src\<persistentID の FNV-1a 64 の 16 桁>.wav`（float32、ソースの周波数・チャンネルのまま）に書いて、**すぐ** `ara_set_modification`（保留のアーカイブがあれば続けて `ara_restore`）。音が変わったら読み直す。
3. 外した修飾は `ara_remove_modification`、位置（代表のリージョン＝ソングで最初のものでソースの 0 秒が置かれる秒）・名前・DAW のトラック名の変化とアーカイブのガイドは `ara_sync`。
4. `ara_revs` で版の変わった修飾に `ara_render_dirty`（`max_sec` 10、`more` の間は続ける）→ `EditedPcm::applyDirty` → ホストに音が変わったと知らせ（メッセージスレッドで `notifyContentChanged`）→ ノートを取った版と違う修飾に `ara_notes`（下の「DAW に返すもの」）→ `ara_archive` で保存用の写し。編集はあるが解析がまだのものは `waiting`（1 秒ごとの `ara_revs` で解析の終わりを拾う）。
5. 画面の `engineCall` が成功したら（読むだけのもの以外）同期を予約する。途中で次の予約が来たら、終わってからもう 1 回だけ回す。

エンジンが落ちたら `engine` の知らせ（`failed`）を出し、最後のキャッシュのまま鳴らす。画面の［つなぎ直す］（`restartEngine`）で起動し直し、`ara_open` → 全修飾の `ara_set_modification` → 全部の窓を取り直す。ドキュメントを閉じるとエンジンの stdin を閉じ、2 秒待って Job Object を閉じる（作業場所は消さない）。

### DAW に返すもの（ノート。`kARAContentTypeNotes`）

DAW がイベントの上に音符を描く・MIDI に書き出す・ほかのプラグインに渡すための、ARA の content reader。中身はエンジンの今のノート
（編集を当てた後の位置と音程）で、**同期のスレッドがエンジンから取ってメモリに写しを持ち、content reader はその写しを読むだけ**（エンジンを待たない。
オーディオスレッドは関係しない）。写しは `plugin/src/ara/NoteContent.*`（ARA の型を使わない。単体テストにも入る）、ARA の口は `GlissDocumentController`。

| ARA のオブジェクト | 返すノート | 時間 | 品質のラベル（content grade） |
|---|---|---|---|
| `AudioSource` | 解析だけ（エンジンの `ara_notes` の `source_notes`。同じソースの修飾のうち解析の済んだ最初のもの） | ソースの秒 | `detected`（ARA の名前。DAW の画面では analyzed と出ることが多い） |
| `AudioModification` | 編集を当てた後（`notes`） | ソースの秒 | 解析だけなら `detected`、編集リストが空でなければ `adjusted` |
| `PlaybackRegion` | 修飾のノートをリージョンの修飾の範囲で切り、位置と長さを `ソングの秒 = ソングの頭 + (修飾の秒 − 修飾の頭) / 伸縮比` で写す | ソングの秒 | 修飾と同じ |

- 1 つのノート（`ARAContentNote`）: `frequency` = 中心の音程の Hz（画面の帯の中心 `edited_pitch_midi` と同じ定義）、`pitchNumber` = それを丸めた MIDI 番号、
  `volume` = ノートの音量の山（-60 dB → 0、0 dB → 1）、`startPosition`・`noteDuration` = 編集後の頭と長さ（画面の `edited_start_sec`・`edited_end_sec`）、
  `signalDuration` = `noteDuration`、`attackDuration` = 0。音程のあるノートだけで、無音にしたノート（`mute_notes`）は返さない。頭の順。
- 解析がまだの間は「無い」（`isContentAvailable` が false、品質は `initial`）。ホストが渡す時間の範囲（`range`）は「少しでも重なるもの」で絞る。
- 解析（`ARA_ANALYSIS_TYPES`）: ホストの `requestAudioSourceContentAnalysis` はエンジンの起動を促すだけ（解析はエンジンの裏の準備が全部の修飾に行う）。
  `isAudioSourceContentAnalysisIncomplete` は「解析の済んだ修飾が無く、解析が進みうる」間だけ true。エンジンが無効・失敗、ホストが読ませない、
  修飾が無い・失敗したときは false を返す（ホストが終わりを待ち続けないように）。
- 取り直し: `ara_revs` の版（`<解析の署名>:<編集の署名>`）が、ノートを取った版と違う修飾だけ `ara_notes(ara_ids)` を呼ぶ（解析が済む・編集・取り消し・アーカイブから戻す、で版が変わる）。
  中身が変わったら、メッセージスレッドで修飾と各リージョンに `notifyContentChanged(notesAreAffected)`、解析だけのノートが変わったらソースにも（ホストへは次の `notifyModelUpdates` で出る）。
  TestHost ではメッセージが回らないのでこの知らせは届かないが、TestHost は `isAudioSourceContentAnalysisIncomplete` を回して待つので、読み出しは確かめられる。
- アーカイブから戻した直後も、解析が済んで中身が変わったところで知らせる（ARA は「戻した状態と違うときだけ知らせる」としているが、解析はアーカイブに入れていないので、戻した時点ではノートが無い）。

#### DAW ごとに確かめること（実物の DAW。人の許可を取って）

| DAW | 確かめること |
|---|---|
| Fender Studio Pro 8 | **確認済み（8.1.2.113407、2026-10-04。このブランチの 0239d26 のビルド）**: 挿す・開く・解析・再生・Export Selection（オフラインの描画）・ソングの保存と開き直し（アーカイブの復元・ガイドの指定も戻る）・外部の AI からの編集（下の「外部の AI からの操作を DAW で確かめる」）。書き出しは Gliss の再合成と ±0.3 セント、開き直し前後の書き出しはサンプル単位でほぼ同じ（差 4e-6）。リアルタイムの再生は、開始位置の約 0.9 秒手前（Studio Pro の先行描画）の頭の約 80 ms だけ先読みが間に合わず無音のブロック（`incompleteReads` 19〜35）。ループの折り返しでは出ない。**未確認**: イベントの上・ピアノロールに Gliss のノートが出るか（この日の操作の間、Studio Pro は内容の読み出しを頼まなかった＝`notes: the host requested the analysis` の行が無い）（JUCE フォーラム 2023-10 に、Reaper では出るが Studio One では出ない・Melodyne を載せた後に出たという報告があり、条件が分かっていない）。「音声を MIDI に」のような操作で `adjusted` のノートが使われるか。編集の後に描き直されるか（`notifyContentChanged(notesAreAffected)` を受けるか）。プラグインのログ（`GLISS_ARA_TRACE_DIR`）の `notes: the host requested the analysis` で、DAW が解析を頼むかも分かる |
| Cubase / Nuendo | 未確認。ARA の拡張の「音声を MIDI に」などでノートが取れるか、`detected` と `adjusted` で扱いが変わるか |
| Reaper | 未確認（この PC に無い）。報告では ARA のノートを MIDI のアイテムに書き出せる |

### 外部の AI から操作する（中継。`engine/vocal_engine/ara_relay.py`）

Claude Code などの外部の AI（自分でエンジン `python -m vocal_engine.mcp` を起動する MCP クライアント）が、DAW の中で開いている Gliss の文書を
普段のツールで編集できる。DAW の文書を持つのはプラグインのエンジン 1 本だけなので、プラグインのエンジンが `127.0.0.1` の口をトークン付きで開き、
AI のエンジンが呼び出しを転送する。ツールと仕組みの詳細は [engine/docs/MCP.md](../engine/docs/MCP.md) §3-5。

AI からの手順:

1. `ara_documents()` で開いている文書と修飾（DAW のトラック名・修飾の名前・persistentID・ソースの長さ・解析の状態・プラグインの画面で開いているか）を見る。
2. `ara_attach(ara_id)` で修飾を選ぶ（修飾が 1 つ・画面で開いているものなら省ける）。
3. `analyze_take` → `list_notes` / `list_deviations` → `shift_pitch`・`correct_to_guide`・`move_note`・`undo` など、単体のときと同じ名前・引数で呼ぶ。
4. 終わったら `ara_detach()`（単体の Gliss の曲に戻る）。

- 編集はプラグインのエンジンの中で、選んだ修飾のトラックに一時的に切り替えて実行し、画面の編集対象に戻す（エンジンのロックの中。画面の呼び出しと
  外部の呼び出しが同時に来ても順に当たる）。外部の編集は曲の取り消しの履歴に `author: "ai"` で入る。
- 同期のスレッドは今までどおり 1 秒ごとに `ara_revs` を見るので、外部の編集は約 1 秒で再生のキャッシュ・DAW に返すノート・保存用の写し
  （`ara_archive`。DAW のソングに保存される）に入る。`ara_revs` の `external.seq` が変われば画面に `project-changed`（セッションを変えうるもの
  なら `session-changed` も）を送り、画面は「外部の変更を読み込んだ」で描き直す。プラグインのログには `sync: external edit` の行が出る。
- 許可はプラグインのエンジンの環境変数 `GLISS_ARA_AI`（DAW を起動するときの環境から引き継ぐ）: `off`（口を開かない）・`read`（読むだけ）・
  `edit`（既定。編集まで）・`save`（編集と保存・書き出し）。DAW の文書の作り・トラックの増減・保存（`load_project`・`add_track`・`save_project` など。
  画面が断るものと同じ）は許可に関係なく断る。プラグインの画面にはまだ切り替えが無い。
- 記録は `%APPDATA%\Gliss\ara-sessions\<エンジンの pid>.json`（接続先・トークン・DAW の pid（プラグインが `GLISS_ARA_HOST_PID` で渡す）・文書の鍵）。
  エンジンが終わる（DAW が文書を閉じる）と消え、落ちて残ったものは AI 側が pid の生存で無視して消す。

#### 単体の `.gliss` の補正を DAW の文書へ移す（`export_edits` → `import_edits`）

単体の Gliss で作った補正（`.gliss` のトラックの編集）を、DAW の中の Gliss（ARA）の**同じ素材**の修飾へ移せる。DAW には補正済みの音を
書き出して載せるのではなく、元の音の上に Gliss の編集が乗るので、DAW の中でも手で直し続けられる。編集の秒は素材の秒でトラックの位置に
依らないので、`.gliss` のトラックの `take`・`lyrics`・`changesets`（取り消しの履歴・author）をそのまま当てる。

1. DAW に**補正前の元の音**のイベントを置き、Gliss（ARA）を挿してエディタを 1 回開く（`ara_documents` に出る）。同じ音のイベントを複数の
   トラックに置くと修飾が共有されるかは DAW 次第なので、取り込み用の専用のトラックを作る。
2. `export_edits(gliss_path, track?)`（読むだけ）で `estimator`（補正を作った F0 の方式）・`stats`（編集の数・author・ノート ID に頼る編集の数）・
   `not_transferred`・`warnings` を見る。`estimator` が `null`（方式を記録する前の `.gliss`。`.gliss` に残るのはトラックに明示した方式だけ）なら、
   `import_edits` に `estimator` を指定する（ノート ID に頼る編集があれば、`missing_note_targets` が 0 になる方式を試す）。
3. `ara_attach(ara_id)` → `import_edits(gliss_path=…, track=…)`（または `archive=<export_edits の archive>`）。素材（長さ・音の中身のハッシュ）が違えば
   `mismatch: true` と `reason` を返して**何も変えない**。修飾に別の編集が既にあるときは `replace: true` が要る。F0 の方式は
   その修飾の方式になり（裏の準備も追従する）、解析がその方式でなければここで解析する（数秒〜数十秒、エンジンを占有する）。
4. 確かめる: `list_changes`（件数・author）・`list_notes`、DAW の再生・書き出し。外部の編集と同じ道なので、約 1 秒で再合成・画面の描き直し・
   ソングの保存に乗る。書き出しは、同じ方式で解析したとき、単体の `render_region` とサンプル単位で同じ。終わったら `ara_detach()`。

- 方式は音を決める: 補正を作ったのと違う方式で解析すると再合成の音が変わる（実測: gliss で補正した編集を rmvpe で解析し直すと約 19 秒分）。
  取り込んだ後は `archive.f0_estimator` として DAW のソングに保存され、開き直す（`ara_restore`）と同じ方式で解析する。この PC で使えない方式
  （`rmvpe` の重みが無い）は当てずに `warnings` / `estimator_note` に出る。
- 移らないもの: `.gliss` のセッションの項目（`mutes`・`cuts`・ゲイン・パン・ミュート・ソロ・ガイドの指定・テンポ）とクリップ（素材の一部）のトラック
  （ARA の修飾は素材の全体）。`export_edits` / `import_edits` の `not_transferred` に一覧が出る。区間のミュートは DAW のリージョンのミュートで作り直す。
- 取り込んだ編集は取り消しの履歴（Ctrl+Z）に入らない（`ara_restore` と同じ。DAW の読み込みを Ctrl+Z で戻させない）。1 つの undo にまとめる仕組みは
  無い。戻すときは `undo(changeset_id)`・`reset_to_original(whole_track=true)`、または元の編集を `replace: true` で入れ直す。
- `import_edits` は外部の AI から中継で呼べる**編集**の許可（`GLISS_ARA_AI=edit` 以上）。`export_edits` は AI のエンジンの中で `.gliss` を読む
  だけ（許可なし。DAW の文書を選んでいる間も転送しない）。画面で開いていて未保存の編集は入らない（保存した `.gliss` の中身）。

#### 外部の AI からの操作を DAW で確かめる（Fender Studio Pro 8。人の許可を取って）

開発版のプラグインを Studio Pro に載せる手順（ビルドの出力を使う。インストール済みの `%LOCALAPPDATA%\Programs\Common\VST3\Gliss.vst3` は上書きしない）:

1. Studio Pro を閉じる（読み込み中の DLL は上書きできない。プラグインの一覧を取り直させるためにも閉じた状態で置く）。
2. `<wt>\plugin\build\GlissARA_artefacts\Release\VST3\Gliss.vst3`（フォルダごと）を、Studio Pro の VST3 の探す場所に足すか写す。
   インストール済みの `Gliss.vst3` と同じ `ARA_FACTORY_ID`・`BUNDLE_ID` なので、両方が探す場所にあると 2 つ並ぶか片方だけが読まれる。
   確かめる間はインストール済みの置き場を Studio Pro の探す場所から外す（ファイルは消さない）か、Studio Pro がどちらを読んだかをプラグインのログで見る。
3. Studio Pro を、次の環境変数を付けて起動する（PowerShell から `$env:…='…'` を設定して `& "<Studio Pro の exe>"`）:
   `GLISS_ENGINE_PYTHON=<main>\.venv\Scripts\python.exe`・`GLISS_ENGINE_CWD=<wt>\engine`（worktree のエンジンを使う）・
   必要なら `GLISS_ARA_TRACE_DIR=<ログの置き場>`・`GLISS_ARA_AI=edit`（既定と同じ）。
4. ボーカルのイベントに Gliss を ARA の拡張として挿し、エディタを開く（エンジンが起動し、`%APPDATA%\Gliss\ara-sessions\` に記録ができる）。
5. Claude Code（`~/.claude.json` の `gliss`。外部のエンジンは main の `engine` でなく worktree の `engine` を読むよう、`PYTHONPATH` か `cwd` を `<wt>\engine` にする）から
   `ara_documents` → `ara_attach` → `analyze_take` → `shift_pitch` を呼び、再生の音・イベントの上のノート・プラグインの画面（描き直し）が変わること、
   ソングを保存して開き直しても編集が残ることを確かめる。

実機の結果（Fender Studio Pro 8.1.2.113407、2026-10-04。このブランチの 0239d26 のビルド。Studio Pro の操作は fender-studio-pro-mcp の MCP、
記録はそのリポジトリの `evidence/ara-relay-2026-10-04.json`）:

- 2 の置き場は、インストール済みのフォルダを VST3 の探す場所の外（`%LOCALAPPDATA%\Gliss\backup\…`）へ移し、同じ場所にビルドを写した。
  Studio Pro は起動時に Gliss だけを読み直した（`VSTPlugInScanner.log`）。worktree のエンジンが動いたことは、`ara-sessions` に記録ができた
  （`ara_relay.py` は worktree のエンジンにしか無い）ことで分かる。
- 1 音: `shift_pitch(+100)` は約 1 秒で `ara_render_dirty`・`sync: external edit`・`ara_notes … adjusted` がログに出て、画面は「外部の変更を読み込んだ」で
  描き直した。Export Selection で測ると、そのノートだけ +99.9 セント（前後のノート 0.0）。`undo` で書き出しは元とサンプル単位でほぼ同じ（差 3e-6）。
- ガイドに合わせる: 主旋律のガイドと録り直しを別のトラックの同じ位置に置き、どちらにも Gliss を挿す。外部から `set_guide_track`（中継でそのまま使える。
  ARA 用の別のツールは要らない）→ 範囲 40–55 秒に `correct_to_guide`（タイミング 1.0 → 音程 1.0・`pitch_mode="contour"`）。`measure_against_guide` で
  区間の音程の残差 45.3 → 4.5 セント（±10 以内 12.5% → 83.3%）、発音の頭 19.9 → 0.2 ms（再合成して測り直した値）。Studio Pro の書き出しが変わったのは
  40–54 秒だけ（範囲外は −121 dB）で、Gliss の再合成した窓（`ara-out` の `.f32`）と ±0.3 セントで同じ。
- 保存すると、Gliss のアーカイブ（`ARA/ModelData` の `gliss-ara`）にガイドの persistentID と changeset が入る。開き直すと `ara_restore … ok (60 edit(s))`、
  ガイドの指定も戻り、`measure_against_guide` と書き出しは保存前と同じ。ただし開き直した後は `ara_revs` の編集の署名が変わる（音は同じ。開いたときに 1 回再合成する）。
- 比べるときは DAW の書き出しどうしで比べる（作業場所の 48 kHz のソースを手元で 44.1 kHz に直すと、変換の誤差を「範囲外が変わった」と取り違える）。

### エディタ（`GlissEditor`・`plugin/src/editor`）

- `GlissEditor` は ARA の `EditorView` のドキュメントから `DocumentBridge`（`plugin/src/ara/DocumentBridge.h`。`GlissDocumentController` が実装する）を `dynamic_cast` で得る。
  得られれば画面（`editor::EditorWebView`）を出し、`EditorView` の選択の変化（`onNewSelection`。開いた時点の選択も）を `DocumentBridge::editorSelectionChanged` へ渡す。
  得られない（ARA に対応しないホスト・普通の VST3 として挿された）ときは「ARA に対応した DAW で、オーディオのイベントに挿してください」の案内だけを出す。
- 画面は単体アプリと同じ `app/renderer`（`index.html` と `*.js`）。`WebBrowserComponent`（WebView2、ネイティブ連携あり）の resource provider（`editor/WebResources`）で配る:

  | パス | 中身 |
  |---|---|
  | `/`・`/<名前>.js` | `app/renderer` の `index.html`・`*.js`（`juce_add_binary_data` で DLL に埋め込み。名前空間 `GlissPageData`） |
  | `/juce/index.js` | JUCE 9.0.3 の `modules/juce_gui_extra/native/typescript/webview-interop/dist/index.js`（`getNativeFunction`。名前空間 `GlissEditorData`） |
  | `/fs/<encodeURIComponent(絶対パス)>` | ディスクのファイル。1 回だけ percent-decode し、`DocumentBridge::isReadableByEditor` が許した通常のファイルだけ（`..`・`.` の段・装置のパス・代替データストリーム・リンク・フォルダは断る） |

  JUCE 9.0.3 の resource provider は状態コードを選べない（いつも 200。資源を返さないと WebView2 がネットワークへ取りに行く）。見つからない・読ませないものは **MIME `application/x-gliss-not-found`** の本文で返し、`ara-bridge.js` の `fetchFs` がそれを「読み取りを許していない場所」として扱う。
- `app/renderer/ara-bridge.js`（`window.api` を作る）と `plugin/src/editor/key-forward.js` を `withUserScript` で差し込む。ネイティブ関数（`engineCall`・`bootstrap`・`saveState`・`transport`・`preview`・`setCompare`・`hostState`・`restartEngine`）は `DocumentBridge` へ渡し、`pickFile`・`confirm`・`copyText`・`reveal`（作業場所の下だけ）はエディタが答える。どれも中で待たない。
  `DocumentBridge` の completion はエディタが閉じた後に来ても捨てる（JUCE の completion は `WebBrowserComponent` が壊れた後に呼ぶと解放済みのものを触る）。
- 画面のイベント `ui-ready` を受けてから、`DocumentBridge::Listener` の知らせ（`playhead`・`selection`・`session-changed`・`project-changed`・`cache`・`engine`）を `emitEventIfBrowserIsVisible` で送る。それより前の知らせは捨てる（画面は起動のときに `hostState()` で引き直す）。DAW がエディタを隠しても画面は読み直さない（`withKeepPageLoadedWhenBrowserIsHidden`）。
- キー: WebView2 にフォーカスがあるとキーは DAW の窓に届かない。`key-forward.js` が、画面のどの受け手も `preventDefault` しなかったキー（文字の入力欄・IME の変換中・修飾キーだけ・Tab・Esc・F10 は除く）を C++ へ送り、C++ はプラグインの窓（JUCE のピア）に `WM_KEYDOWN`/`WM_KEYUP` として置く。JUCE のピアは使わなかったキーを親の窓（DAW）へ渡す（JUCE の普通のプラグインと同じ道）。画面が使う Space（再生／停止＝`transport('toggle')`）は渡らない。渡したキーはブラウザ自身の動き（印刷・検索・拡大・再読み込み）をさせない。
- 画面の外へのリンク（http(s)）は WebView では開かず、既定のブラウザで開く。
- WebView2 の `userDataFolder` は**プロセスごとの一時フォルダ**（`%TEMP%\GlissARA-<プロセス ID>`）。最後のエディタが閉じたときに消す（WebView2 が掴んでいて消せなければ残る。次に起動したプロセスが、動いていないプロセスの分を消す）。
- ARA ではエディタをリサイズできることが求められるので `setResizable (true, false)`。

### 検証用の環境変数（開発用）

| 名前 | 意味 |
|---|---|
| `GLISS_ARA_TRACE_DIR` | 指すフォルダの `gliss-ara-<プロセス ID>.log` に、プラグインの出来事（アーカイブの保存・復元、レンダラーの準備・解放と集計、エディタの `ui-ready`・案内を出したこと）と、再生の記録（`trace` の行。ブロックごとの総和・二乗和）を書く。オーディオスレッドからは書かず、レンダラーの解放のときにまとめて書く |
| `GLISS_ARA_READ_TIMEOUT_MS` | リアルタイムの描画でも先読みの完了をこの ms だけ待つ。検証ホスト（TestHost は CPU の速さで取りに来る）で欠けなく比べるため。普段は使わない |
| `GLISS_ARA_SYNC_WAIT_MS` | リアルタイムの描画でも、同期（エンジン・差分の再合成）の完了をこの ms だけ待つ（prepareToPlay ごとの持ち時間。既定はバウンスのときだけ 10 秒）。検証ホストで編集の当たった音を描かせるため |
| `GLISS_TEST_EDIT` | 試験用の編集。エンジンにつないで最初の修飾を解析した後に 1 回だけ当てる。`{"tool": "shift_pitch", "args": {...}}`・`shift_pitch` の引数そのもの（`{"cents": 100, "start_sec": 0, "end_sec": 5}`）・`shift_pitch:<note_id>:<cents>` |
| `GLISS_ENGINE_DISABLED` | `1` でエンジンを起動しない（原音のまま。エンジンの要らない検証を速く・利用者の環境のエンジンを起動しないため） |
| `GLISS_PLUGIN_STATE_FILE` | 画面の設定 `plugin-state.json`（既定 `%APPDATA%\Gliss\plugin-state.json`）の置き場を差し替える（試験で利用者の設定を書かない） |
| `GLISS_ARA_AI` | 外部の AI に許すこと（`off`・`read`・`edit`（既定）・`save`。上の「外部の AI から操作する」）。エンジンが読む |
| `GLISS_ARA_SESSIONS_DIR` | 外部の AI の中継の記録の置き場（既定 `%APPDATA%\Gliss\ara-sessions`）。試験で利用者の置き場に書かない。プラグインのエンジンと AI のエンジンで同じ値にする |
| `GLISS_PLUGIN_WEB_DIR` | 既にあるフォルダ（`<repo>\app\renderer`）を指すと、エディタは画面の資源（`index.html`・`*.js`・`ara-bridge.js`）を埋め込みでなくそのフォルダから要求のたびに読む（ビルドし直さずに画面を直せる）。`ara-bridge.js` は user script なのでエディタを開き直したときに読み直す。このときは F5・Ctrl+R をブラウザの再読み込みに残す（DAW へ渡さない） |

エンジンの起動の設定（`GLISS_ENGINE_PYTHON`・`GLISS_ENGINE_CWD`）は下の「配布版のプラグインがエンジンを見つける順」、エンジン側の環境変数（`VOCAL_ENGINE_WORK_DIR`・`GLISS_F0_ESTIMATOR` など）は AGENTS.md。試験では `VOCAL_ENGINE_WORK_DIR`・`VOCAL_ENGINE_LOG_DIR` を一時フォルダに向ける。

## ビルド

必要なもの: Visual Studio 2022（C++ のデスクトップ開発。Community でよい）、CMake 3.22 以上、git。管理者権限は要らない（ここの手順はプラグインをどこにもインストールしない。配布用のビルドは `pnpm build:plugin`。下の「配布」）。

```powershell
cmake -S plugin -B plugin/build -G "Visual Studio 17 2022" -A x64
cmake --build plugin/build --config Release --target GlissARA_VST3
# 出来るもの: plugin\build\GlissARA_artefacts\Release\VST3\Gliss.vst3\Contents\x86_64-win\Gliss.vst3
```

最初の構成で JUCE（約 130 MB）・ARA SDK（サブモジュール込み）・WebView2 の NuGet パッケージ（URL とハッシュを固定）・Signalsmith Stretch と Linear を取る。2 回目以降はネットワークが要らない。

新しい worktree で取り直さずに、別の worktree の `plugin/build/_deps` を使うとき（2026-10-04 に通した形。`<他>` = その worktree の `plugin/build/_deps`）:
JUCE・ARA SDK・Signalsmith は `FETCHCONTENT_SOURCE_DIR_*` で指せるが、WebView2 は `JUCE_WEBVIEW2_PACKAGE_LOCATION` を自分の `_deps/nuget` に固定しているので、
`<他>\nuget` を自分の `plugin/build/_deps/nuget` に写してから指す。`test-plugin.ps1` は VST3 SDK を自分の `_deps\vst3sdk-3.7.11` に探す（無いと取りに行く）ので、それも写す。

```powershell
cmake -S plugin -B plugin/build -G "Visual Studio 17 2022" -A x64 "-DFETCHCONTENT_SOURCE_DIR_JUCE=<他>/juce-src" "-DFETCHCONTENT_SOURCE_DIR_ARA_SDK=<他>/ara_sdk-src" `
  "-DFETCHCONTENT_SOURCE_DIR_SIGNALSMITH_STRETCH=<他>/signalsmith_stretch-src" "-DFETCHCONTENT_SOURCE_DIR_SIGNALSMITH-LINEAR=<他>/signalsmith-linear-src" `
  "-DFETCHCONTENT_SOURCE_DIR_WEBVIEW2=plugin/build/_deps/nuget/Microsoft.Web.WebView2.1.0.4258.31"
````COPY_PLUGIN_AFTER_BUILD` は切ってある（システムの VST3 フォルダに書かない）。
同じ構成が `GlissHostCheck`（検証用のホスト）も作る（`-DGLISS_BUILD_HOSTCHECK=OFF` で外せる）。

DAW に載せて試すときは、`Gliss.vst3` を DAW が探す場所に置く（Fender Studio Pro 8 の VST3 の追加の場所に `plugin\build\GlissARA_artefacts\Release\VST3` を足すか、`%CommonProgramFiles%\VST3` へ管理者権限でコピーする。どちらも未確認。実物の DAW での確認は人の許可を取って行う）。配布版はインストーラがユーザーごとの置き場に入れる（下の「配布」）。
Cubase は ARA のプラグインを `%CommonProgramFiles%\ARA` に置く必要があるという報告がある（Steinberg のフォーラムの報告。現行の条件は未確認）。

## 配布

決めた日: 2026-10-03。Gliss のインストーラ（NSIS・ユーザーごと・管理者権限なし。[release-plan.md](release-plan.md) §3-1）がプラグインも入れる。

### 作り方

```powershell
cd app
pnpm build:plugin     # = node ../scripts/build-plugin.mjs
pnpm dist             # build:engine → build:plugin → prepack:files → electron-builder（プラグインのビルドに失敗すれば止まる）
```

`scripts/build-plugin.mjs` は `plugin/build` を構成し（まだなら、vswhere で見つけた Visual Studio の generator と `-A x64`。構成済みならその generator のまま）、`GlissARA_VST3` を Release でビルドして、`plugin/build/dist/Gliss.vst3` に写す。
写した `Contents/Resources` に、ライセンスの文書 `NOTICE.txt`・`LICENSE-AGPL-3.0.txt`・`LICENSE-GPL-3.0.txt`・`THIRD_PARTY_NOTICES.txt` を足す（`Gliss.vst3` だけを別の場所へ写しても文書が付いていく）。
`plugin/CMakeLists.txt` の末尾が `plugin/build/gliss-plugin-<構成>.json`（DLL の場所・依存の置き場と版）を書き、`build-plugin.mjs` がそれを `plugin/build/dist/gliss-plugin.json` に写す。`scripts/third-party-notices.mjs` はそれを見て依存のライセンス文を集める。
`node scripts/build-plugin.mjs -- <cmake の引数>` で構成に引数を足せる（手元で取得済みの依存を使う `-DFETCHCONTENT_SOURCE_DIR_JUCE=<置き場>` など）。`--check` は `plugin/build/dist` にあるかだけを見る（`pnpm prepack:files` の最初の一手。無ければ `dist:dir` も止まる）。
CI（`release.yml`）はランナーの Visual Studio と CMake で同じスクリプトを呼ぶ。手元では依存の取得を含めて約 5 分。

### 置き場

| 場所 | 中身 |
|---|---|
| `<インストール先>\resources\plugin\Gliss.vst3` | electron-builder が同梱する写し（`app/electron-builder.yml` の `extraResources`）。インストール先の既定は `%LOCALAPPDATA%\Programs\Gliss` |
| `%LOCALAPPDATA%\Programs\Common\VST3\Gliss.vst3` | インストーラ（`app/build/installer.nsh` の `customInstall`）が写す。VST3 の仕様の**ユーザーごとの置き場**（`FOLDERID_UserProgramFilesCommon`。仕様では優先 1、管理者権限が要らない） |
| 両方の `Contents\Resources\gliss-install.json` | インストーラが書く `{"format":"gliss-install","version":1,"installDir":"<インストール先>","appVersion":"<版>"}`。NSIS は UTF-8 で書けないので **UTF-16LE（BOM 付き）** |

管理者の `C:\Program Files\Common Files\VST3` には入れない。

**ユーザーごとの置き場を探さない DAW がある**（VST3 の仕様は「主に開発の用途」と書いている。Renoise のフォーラムに `Common Files\VST3` の外を認識しない報告がある）。Studio Pro・Cubase・Reaper が既定で探すかは**未確認**。探さない DAW では、DAW の設定で `%LOCALAPPDATA%\Programs\Common\VST3` を探す場所に足すか、下の手順で管理者の置き場に写す。

### 配布版のプラグインがエンジンを見つける順（`plugin/src/engine/EngineConfig.cpp`）

1. 環境変数 `GLISS_ENGINE_PYTHON`（と `GLISS_ENGINE_CWD`）: 開発版。`python -m vocal_engine.mcp`。
2. `Gliss.vst3\Contents\Resources\gliss-install.json` の `installDir` の `resources\engine\vocal-engine\vocal-engine.exe`（インストール先を変えても見つかる）。指す先にエンジンが無ければ次へ。
3. プラグインの祖先のフォルダ: `<祖先>\engine\vocal-engine\vocal-engine.exe`（インストール先の中の写しと、展開版 `dist\win-unpacked\resources`）、`<祖先>\engine\vocal-engine.exe`、開発の `.mcp.json`。
4. レジストリ `HKCU\Software\a4620d0b-b9f5-551f-81ff-214a8d76afd2` の `InstallLocation`（electron-builder がインストール先を書くキー。appId の UUID v5 で、appId を変えない限り変わらない。`app/tests/unit/release.spec.js` の R11 が照らす）。
5. 現在の作業ディレクトリの祖先の `.mcp.json`。

作業ディレクトリはエンジン exe のフォルダ。重みはエンジンの既定（`%LOCALAPPDATA%\Gliss\models`）のまま。見つけ方は `EngineConfig::source`（`env`・`install-file`・`bundled`・`mcp.json`・`registry`・`cwd-mcp.json`）に残る。

### 更新とアンインストール

- **更新**（アプリの自動更新・手動の上書き）: `customInstall` が VST3 の置き場の `Gliss.vst3` を消してから写し直す（古い版の余計なファイルを残さない）。
  **DAW がプラグインを読み込んでいる**と DLL は消せず上書きもできないが、改名はできる。そこで DLL を `Gliss.vst3.<数>.old` に改名して新しい版を隣に置く。DAW は起動し直すまで古い版のまま動き、`.old` は次のインストールで消える。
  画面のあるインストールでは「DAW を起動し直すと新しい版になる」と出す（静かなモード＝アプリからの更新では出さない）。
- 更新の前に `customCheckAppRunning` が `<インストール先>\resources\engine\` の下のエンジンを止める。**DAW のプラグインが起動したエンジンも止まる**（プラグインは次の呼び出しで起動し直す）。
- **アンインストール**（`--updated` が付かないとき）: `gliss-install.json` のある `Gliss.vst3` だけを消す（手で置いたものは残す）。DAW が掴んでいれば「DAW を閉じて再試行」の確認を出す（キャンセルと静かなモードでは残す）。
- `Gliss.vst3` がリンク（ジャンクション）なら、リンクだけを外し、指す先は消さない（開発のビルドを指していても消えない）。

### 管理者の置き場に入れたいとき

DAW がユーザーごとの置き場を探さないときの手順（管理者の PowerShell で）。

```powershell
Copy-Item -Recurse "$env:LOCALAPPDATA\Programs\Gliss\resources\plugin\Gliss.vst3" "$env:CommonProgramFiles\VST3\"
```

インストール先の写しには `gliss-install.json` が入っているので、写した先でもエンジンが見つかる。Gliss の更新・アンインストールは管理者の置き場の写しを**更新も削除もしない**（更新のたびに写し直す。要らなくなったら手で消す）。
ユーザーごとの置き場にも同じプラグインがあると、DAW によっては 2 つ並ぶ。管理者の置き場に写したら、`%LOCALAPPDATA%\Programs\Common\VST3\Gliss.vst3` は消してよい（Gliss の次の更新でまた入る）。

### DAW ごとの置き場の癖

| DAW | 癖 |
|---|---|
| Fender Studio Pro 8 | ユーザーごとの置き場を既定で探すかは未確認。探さなければ、設定の VST3 の探す場所に足す |
| Cubase 15 / Nuendo | ARA のプラグインは `C:\Program Files\Common Files\ARA` に置く必要があった（Steinberg の担当者の 2022-01 の書き込み。そこへのシンボリックリンクでよい）。VST3 フォルダの直下でないと認識しない（ベンダーのサブフォルダ不可）という 2021 年の報告もある。**現行の条件は未確認**。必要なら管理者の PowerShell で `New-Item -ItemType SymbolicLink -Path "$env:CommonProgramFiles\ARA\Gliss.vst3" -Target <Gliss.vst3 の場所>`。ARA の拡張は「オーディオ > 拡張」から開く |
| Reaper 7 | VST の探す場所は設定で足せる。ARA を認識せず普通の VST3 として載ったら、ARA の設定を確かめる（`ARA_DOCUMENT_ARCHIVE_ID` は設定済み） |

### ライセンス

- JUCE 9（AGPLv3 か商用。Gliss は AGPLv3 の側で使う）と結合した `Gliss.vst3` は **AGPLv3 の条件で配る**。Gliss 自身のソースは GPL-3.0-or-later のまま（GPLv3 §13 と AGPLv3 §13 が結合を認める）。
- 対応するソースは、このリポジトリの同じ版のタグの `plugin/` と、`plugin/CMakeLists.txt` および Signalsmith Stretch の CMakeLists が版を固定して取る依存（JUCE・ARA SDK・`Microsoft.Web.WebView2` の NuGet・Signalsmith Stretch・Signalsmith Linear）。`Gliss.vst3` の `NOTICE.txt` に場所を書いている。
- 依存のライセンス文: JUCE（`LICENSE.md` と AGPLv3 の全文）・ARA SDK（Apache-2.0。`NOTICE.txt` も）・WebView2 のローダ（BSD-3-Clause）・Signalsmith Stretch と Signalsmith Linear（MIT）・JUCE が同梱してリンクされる第三者のコード（zlib・HarfBuzz・SheenBidi・LunaSVG・PlutoVG・libpng・jpeglib・libwebp・FLAC・Ogg Vorbis・Opus・VST3 SDK（MIT）・PreSonus の拡張ヘッダ）。
  `scripts/third-party-notices.mjs` が JUCE の SBOM（`JUCE.spdx.json`）を、`GlissARA` にリンクするモジュールから辿って集める。リンクしないもの（ASIO SDK・Oboe・AudioUnitSDK・AAX SDK・LV2 一式）は理由つきで外し、SBOM に知らない同梱物が増えたら載せる（JUCE を上げたら `JUCE_NOT_LINKED` を見直す）。
- インストール先の `resources\THIRD_PARTY_NOTICES.txt` の「DAW のプラグイン」の節と、`Gliss.vst3\Contents\Resources\THIRD_PARTY_NOTICES.txt` は同じ中身。

### 配布の確かめ方

| 確かめること | 手順 |
|---|---|
| 同梱・置き場・レジストリのキー・ライセンス文の集め方 | `pnpm test:unit`（`release.spec.js` の R6b・R11・R11b） |
| 展開版にプラグインとライセンスの文書がある | `pnpm dist:dir` → `node scripts/smoke-packaged.mjs` |
| 見つけ方（UTF-16 の `gliss-install.json`・日本語と空白を含むパス・インストール先の中の写し・消えたインストール先） | `GlissPluginTests.exe` の「Engine discovery (distribution)」 |
| 展開版のエンジンを見つけて起動できる（重みは要らない） | `GLISS_TEST_PACKAGED_RESOURCES=<dist\win-unpacked\resources>` を付けて `GlissPluginTests.exe` |
| インストーラが VST3 の置き場に写したプラグインからエンジンを起動できる | `GLISS_TEST_INSTALLED_PLUGIN=<VST3 の置き場の Gliss.vst3\Contents\x86_64-win\Gliss.vst3>` を付けて `GlissPluginTests.exe` |

インストーラの `customInstall` / `customUnInstall` の中身（`glissInstallPlugin`・`glissRemovePlugin`）は、本物のインストーラを走らせずに確かめられる: 2 つのマクロだけを呼ぶ小さな NSIS の試験用の exe を、electron-builder の makensis で作り、一時フォルダに向けて流す（`!include` で `app/build/installer.nsh` を読み、`/VST3=<一時フォルダ>`・`/D=<一時のインストール先>`）。
本物のインストーラは利用者の `%LOCALAPPDATA%`・レジストリ・スタートメニューに書き、入っている Gliss を上書きするので、開発機では走らせない。

## 検証

```powershell
powershell -NoProfile -File plugin\scripts\test-plugin.ps1              # ビルドして、下の検証を流す（初回は依存の取得と JUCE・ARA_Examples のビルドで数分から 10 分ほど）
powershell -NoProfile -File plugin\scripts\test-plugin.ps1 -SkipBuild   # 流すだけ（約 1〜2 分）
```

エンジンにつなぐ検証（下の表の「エンジン」）は、main の作業ディレクトリの `.venv` の python（worktree には無い）で、worktree の `engine` を cwd にして起動する。
python は `-EnginePython <パス>`・環境変数 `GLISS_ENGINE_PYTHON`・リポジトリ（と main の worktree）の `.venv` の順に探し、無ければエンジンの検証を飛ばして（SKIP と書いて）残りを流す。
ピッチ検出は Praat（重み不要）、エンジンの作業場所・ログ・`plugin-state.json` は一時フォルダに向ける。それ以外の検証は `GLISS_ENGINE_DISABLED=1` で流す。
ARA SDK のホスト（`ARATestHost` と `GlissARATest`）は `plugin/tests/aratest` を根にした別のツリー（`plugin/build/ara-hosts`）で作る。

| 検証 | 見ること |
|---|---|
| ARA SDK の **TestHost**（`-vst3 Gliss.vst3`、全 12 項目） | プロパティ更新・コンテンツ更新・読み出し・クローン・アーカイブ・分割アーカイブ・ドラッグ＆ドロップ・再生・EditorView・処理アルゴリズム・音声ファイルのチャンク。終了コード 0 |
| TestHost の `PlaybackRendering` ＋ `verify_render_trace.py` | プラグインが返した音を、SDK の試験信号（5 秒・44.1 kHz のパルス状の正弦波）と、ブロックごとの総和・二乗和で突き合わせる（`tests/verify_render_trace.py`）。全ブロックが一致すること |
| **GlissHostCheck**（`plugin/tests/hostcheck`、JUCE のホスト） | VST3 として見つかる・`hasARAExtension`・ARA ファクトリの ID が決めたとおり・解析の種類がノートだけ・ARA に結び付かない `processBlock` が入力を変えない・ARA に結び付かないエディタは画面を出さずに案内を出す・エディタとインスタンスを閉じて落ちない |
| **GlissPluginTests**（`plugin/tests/unit`） | 単位ごとの単体テスト（カテゴリ `Gliss`）。エディタは `WebResources`（`/fs/` の decode と拒否・資源の振り分け・開発時のフォルダ）。ドキュメント（`AraTests.cpp`）はアーカイブの形と往復・作業場所の鍵・リージョンの時間の写し（周波数が同じときは段階 1 の計算と同じ・違うときは続きのブロックが途切れない）・再生位置・禁止のツールと同期を予約するツール・`GLISS_TEST_EDIT`・`ara_render_dirty` の読み方・float の WAV がホストの値をそのまま書く・`plugin-state.json`・エンジンの無いときの同期の待ち。DAW に返すノート（`NoteTests.cpp`）は `ara_notes` の写し・品質のラベル・時間の範囲・ソングの秒への写し・リージョンでの切り取り・知らせるかどうかの比べ方。JUCE の UnitTestRunner はこの console のアプリでは失敗の文を出さない（結果の数だけ）。失敗の中身を見たいテストは、`AraTests.cpp` の `ScopedStdoutLogger` のように間だけ stdout へ出すロガーを入れる |
| エンジン: TestHost（全 12 項目） | エンジンにつないだまま全項目が終了コード 0（ドキュメントを作ってすぐ壊す試験でエンジンが残らない） |
| エンジン: **GlissARATest** ＋ `verify_ara_engine.py`（`plugin/tests/aratest`） | ARA SDK の TestHost の部品で、合成の歌声もどき（44.1 kHz・6.2 秒）のドキュメントを作り、`GLISS_TEST_EDIT`（+100 セント）を当てて描画 → 保存 → 閉じる → 別の作業場所で同じ永続 ID のドキュメントにアーカイブを戻して描画 → 48 kHz でも描画。描画が**エンジンの `render_region`（同じ範囲）とサンプル単位で同じ**（float32 で差 0）・原音と違う・アーカイブから戻した音が同じ・48 kHz の描画が鳴る。ノート: 先に編集なしのドキュメント（リージョン 2 つ。2 つ目はソースの 1.5〜4.2 秒をソングの 10 秒）でホストとして解析を頼んで待ち、ソース・修飾・リージョンのノートがエンジンの `ara_notes` の解析だけのノートと一致して `detected`、2 つ目のリージョンは切って写したもの。編集の後は `adjusted` で、エンジンの編集後のノートと一致し、全部 1 半音上がる。アーカイブから戻した後も同じ |
| エンジン: **GlissHostCheck `--ara-editor`**（`AraEditorCheck.h`） | JUCE の ARA ホスト（`juce_ARAHosting`）で Gliss.vst3 に本物のドキュメントを作り、インスタンスを全部の役で結び付けてエディタを開く。プラグインのログで、エディタがドキュメントの `DocumentBridge` を得る・画面の `ui-ready`・`bootstrap`・エンジンの起動と修飾の登録・画面の `engineCall`（`list_tracks`・`select_track`・`export_view_data`）の成功・`/fs/` を断っていないことを確かめる |
| エンジン: **GlissARATest `-relay`** ＋ `verify_ara_relay.py` | 外部の AI の代わり（`plugin/tests/relay_client.py`。別のプロセスの `vocal_engine.mcp` を stdio で起動）が、開いているドキュメントを `ara_documents` で見つけ、`ara_attach` → `analyze_take` → `shift_pitch`（+100 セント）を中継で呼ぶ。編集の前の描画が原音・後の描画がエンジンの `render_region` とサンプル単位で同じ・保存（アーカイブ）に作者 `ai` の changeset・DAW に返すノートが adjusted で 1 半音上・プラグインのログに `sync: external edit`・ドキュメントを閉じたら中継の記録が消える |
| **GlissHostCheck `--editor`**（`EditorCheck.h`・`FakeDocumentBridge.h`） | Gliss.vst3 を読まず、エディタの画面の部品（`plugin/src/editor`）を**偽の DocumentBridge** につないでこのプロセスの中で画面の外に開く: `app/renderer` が読み込まれ `ui-ready` が来る（その前の知らせは捨てる）・`window.api` と `data-mode=ara`・user script から `/juce/index.js` の動的 import・`engineCall` の往復（`{ok:false}` も値で・別スレッドの completion も）・ほかのネイティブ関数・`/fs/`（空白・`%`・`+`・日本語の名前を読める／外・`..`・無い・フォルダ・知らない資源は拒否）・知らせ 6 種が画面の受け手に届く・F8 は窓へ渡り Space は渡らない・応答の前に閉じても落ちない・2 つ同時に 20 回開閉。`--expect-web-dir` で `GLISS_PLUGIN_WEB_DIR` から読むこと |

どの検証もタイムアウトを持ち、終わりに起動したプロセス（ホスト・WebView2・エンジン）を木ごと止めて、残りが 0 であることを確かめる。
数えるのはその回が起動したホストと、その子のエンジン・その WebView2 だけ（別の worktree で同時に流している検証のプロセスは数えず、止めもしない）。GlissHostCheck の窓は画面の外に置き、`SW_SHOWNA`（前面にも入力の対象にもならない）で出す。

AGENTS.md の「実装と検証」の表では、`plugin/` を変えたら `test-plugin.ps1` を回す。CI（`test.yml`）にはまだ載せていない（VS・WebView2 のランタイムのある Windows ランナーで足せる）。

### 段階 0 の確認（土台。2026-10-03）

| 確認 | 手順 | 結果 |
|---|---|---|
| ARA_Examples の TestPlugIn・TestHost のビルド | `cmake -S <ARA SDK>\ARA_Examples -B <出力> -G "Visual Studio 17 2022" -A x64 -DARA_VST3_SDK_DIR=<VST3 SDK 3.7.11> -DARA_SETUP_DEBUGGING=OFF`（`ARA_SETUP_DEBUGGING=ON` の既定だと `Common Files\VST3` にシンボリックリンクを作ろうとして管理者権限が要る）。VST3 SDK は `cmake -DVST3_SDK_DIR=<置き場> -P <ARA SDK>\install_vst3sdk.cmake` | ビルド成功。TestHost の内蔵の 43 の試験は 0 失敗 |
| TestHost で TestPlugIn | `ARATestHost.exe -vst3 ARATestPlugIn.vst3` | 全 12 項目が終了コード 0 |
| JUCE の ARAPluginDemo | JUCE の `examples` を `-DJUCE_BUILD_EXAMPLES=ON -DJUCE_GLOBAL_ARA_SDK_PATH=<ARA SDK>` で構成し、`--target ARAPluginDemo_VST3` | ビルド成功（ホストの `ARATestHost -vst3 ...\ARAPluginDemo.vst3\Contents\x86_64-win\ARAPluginDemo.vst3` で全 12 項目が終了コード 0。TestHost には `.vst3` の**中のバイナリ**を渡す） |
| JUCE の AudioPluginHost を ARA 付きで | `extras/AudioPluginHost/CMakeLists.txt` を写し、`JUCE_PLUGINHOST_ARA=0` を `1` にして `juce_set_ara_sdk_path` を呼ぶ | ビルド成功（GUI のホストなので、読み込みの確認は代わりに GlissHostCheck で行う） |

### TestHost と JUCE のホストについての注意

- TestHost の `-file <wav>`（音声ファイルを渡す）は、**SDK 自身の TestPlugIn でも**ときどき終わらない（試験ごとに起きたり起きなかったりする）。Gliss の検証では使わず、内蔵の試験信号を使う。
- TestHost は VST3 の `processMode` を `kRealtime` にして CPU の速さで描画する。先読みが間に合わないブロックが多く出る（普通の再生ではない）。このため、突き合わせの検証では `GLISS_ARA_READ_TIMEOUT_MS` で待たせる。待たない場合の挙動は、ブロックが欠ける（無音になる）だけで、読めた部分は正しい。同じ理由で、編集の当たった音を描かせるには `GLISS_ARA_SYNC_WAIT_MS` で同期を待たせる。
- TestHost のメインスレッドは描画の間メッセージを回さない（眠るだけ）。プラグインの `MessageManager::callAsync`（エディタへの知らせ・ホストへの `notifyContentChanged`）はそこでは届かない。同期はメッセージスレッドに頼らずに進む作りなので、描画とアーカイブは確かめられる。
- TestHost のホストは、ホストの音のリーダーの作成と破棄を**ドキュメントを作ったスレッドでだけ**許す（`ARA_VALIDATE_API_THREAD`）。読むのは描画のスレッド以外ならどこでもよい。プラグインは `ARAAudioSourceReader` をメッセージスレッドで作り、同期のスレッドで読む。
- ARA のストリームに `juce::OutputStream::writeString` で書くと、文字列の後ろに NUL が 1 バイト付く（`readString` が読む印）。アーカイブを外から読むときは落とす。
- ARA_Examples を `add_subdirectory` して自前のホストを足すとき（`plugin/tests/aratest`）、`configure_ARA_Examples_target` の `source_group(TREE …)` がターゲットのフォルダの外のソースで落ちる（関数 `ara_group_target_files` を空で定義し直す）。`ExamplesCommon\Windows\ARAExamples.rc` は版の定義が要るので外す。ARA の例は cp932 で読むので、日本語のコメントのあるファイルには `/utf-8` を付ける。
- JUCE のホスト側で、`AudioPluginFormatManager::createARAFactoryAsync(説明, ...)` で ARA ファクトリを取ると、DLL のハンドルを持たずに取り、ファクトリを手放すときに外れた DLL の中を呼んで落ちる（JUCE の ARAPluginDemo でも同じ）。**インスタンスを先に作り、`juce::createARAFactoryAsync (*instance, ...)` で取る**（AudioPluginHost と同じ）。
- JUCE 9.0.3 の `WebBrowserComponent`（Windows）で確かめたこと（2026-10-03）: resource provider には `https://juce.backend` の後ろが**解かれないまま**（`?` 以降も）渡る。`nullopt` を返すと WebView2 がネットワークへ取りに行くので、見つからないものも何か返す（エディタは印の MIME。上の「エディタ」）。ネイティブ関数の completion は `WebBrowserComponent` を壊した後に呼ぶと解放済みのものを触る（`EditorWebView::guarded` で捨てる）。
- 検証で `evaluateJavascript` を使うとき: 結果は JSON の文字列で来るので `JSON::fromString` で読む（`JSON::parse` は最上位の `true`・文字列を読まない）。`evaluateJavascript` の中から直に `import()` すると解決しない（user script やページからの `import()` は通る）。
- GlissHostCheck（ARA のホスト側だけ）では `juce::ARAViewSelection` が宣言されない（`JucePlugin_Enable_ARA` のときだけ）。`DocumentBridge.h` を読むファイルには `tests/hostcheck/AraSelectionShim.h` を前置きする。

## 構成

| 場所 | 内容 |
|---|---|
| `plugin/CMakeLists.txt` | 依存の取得・`juce_add_plugin`・オプション |
| `plugin/src/GlissProcessor.*` | `AudioProcessor`（`createPluginFilter`・`createARAFactory` もここ） |
| `plugin/src/GlissDocumentController.*` | ARA の `DocumentController`。ARA の出来事 → `DocumentSync`、アーカイブ、`DocumentBridge` の実装（`engineCall`・`bootstrap`・`saveState`・`transport`・`hostState`・`isReadableByEditor`・選択と再生位置の知らせ）、ホストの音のリーダー |
| `plugin/src/GlissPlaybackRenderer.*` | 再生（リージョンごとの `RegionReader`・原音の先読み・バウンスでの同期の待ち・比べる・検証の記録） |
| `plugin/src/GlissEditorRenderer.*` | 試聴の役（まだ何も足さない） |
| `plugin/src/ara/` | ドキュメントの部品（ARA の型を使わず、単体テストにも入る）: `DocumentSync`（エンジンとの同期のスレッド）・`NoteContent`（DAW に返すノートの写し・リージョンでの切り取り・品質のラベル）・`ArchiveIO`（アーカイブの形・作業場所の鍵）・`RegionMapping`（リージョンの時間）・`PlayheadState`・`EngineCalls`（禁止のツール・同期の予約・`GLISS_TEST_EDIT`・`ara_render_dirty` の読み方）・`FloatWavWriter`・`PluginState`。`sources.cmake` が ARA を使うファイル（`GlissEditorRenderer`）をプラグインだけに足す |
| `plugin/src/engine/`・`plugin/src/cache/` | エンジンの子プロセスと MCP クライアント・編集した窓のキャッシュと再生の読み出し |
| `plugin/src/GlissEditor.*` | プラグインのエディタ（`DocumentBridge` を得て画面を出す・選択を渡す・ARA でないときの案内） |
| `plugin/src/editor/` | 画面の橋: `EditorWebView`（WebView2・ネイティブ関数・知らせ・ユーザーデータのフォルダ）・`WebResources`（resource provider）・`EmbeddedAssets`・`KeyForwarding`・`key-forward.js` |
| `plugin/src/ara/DocumentBridge.h` | エディタがドキュメントに頼む口（`GlissDocumentController` が実装する） |
| `plugin/src/Diagnostics.*`・`ProcessUtils.*` | 検証用のログ・環境変数、プロセスの ID と生死 |
| `plugin/tests/hostcheck/` | GlissHostCheck（`--editor` の偽の DocumentBridge、`--ara-editor` の本物の ARA ドキュメントも） |
| `plugin/tests/aratest/` | ARA SDK のホスト（`ARATestHost`）と、その部品で作った通し試験 `GlissARATest` を 1 つのツリーで作る CMake |
| `plugin/tests/verify_render_trace.py` | 再生の記録を試験信号と突き合わせる（numpy が要る） |
| `plugin/tests/verify_ara_engine.py` | `GlissARATest` の出力をエンジンの `render_region`・`ara_notes` と突き合わせる（エンジンの python・cwd は engine） |
| `plugin/tests/relay_client.py`・`verify_ara_relay.py` | `GlissARATest -relay` が起動する外部の AI の代わりと、その出力の確かめ |
| `plugin/scripts/test-plugin.ps1` | ビルドと検証の一式 |

## 段階と残り

実装の計画（2026-10-03 承認）は次の 5 段階。

0. 土台の確認（済）。
1. 最小の ARA プラグイン（済）。
2. エンジンとの接続（済。上の「エンジンとの同期」）: エンジンを子プロセスで 1 本、ホストの音を一時 WAV にして `ara_set_modification`、差分の再合成をキャッシュへ、再生はキャッシュを読む、周波数の変換、アーカイブに編集リスト。
3. 編集と再生: 画面の橋（`window.api` を JUCE のネイティブ関数・知らせで。済）。画面からの編集 → エンジン → `ara_render_dirty` → キャッシュの差し替えは通る（GlissHostCheck `--ara-editor` で画面の `engineCall` まで）。残り: DAW のテンポ（`ara_sync` の `tempo`）・つかんだノートの試聴（EditorRenderer）・画面からの編集を自動で確かめる検証。
   残り（全段階で）: **Fender Studio Pro 8 で ARA の拡張として開き、鳴るか・編集が鳴るか・保存して開き直せるか・エディタが出るか**（人の許可を取って、親が行う）。
4. DAW に返すもの・配布: content reader（ノート。済、上の「DAW に返すもの」。実物の DAW で出るかは未確認）。配布（インストーラ・エンジンの exe とモデルの場所・THIRD_PARTY_NOTICES）は済み（上の「配布」。Cubase 用の ARA フォルダは文書の手順だけ）。
   残り: DAW のテンポ・拍子・調をホストから読む（ホストの content reader）、ノート以外（テンポなど）を返すこと。

### 既知の制約・未解決

- ステレオのソースと、モノラルのソースをステレオのバスで鳴らす経路は、ARA の通しの検証が無い（`RegionReader` の単体テストだけ。TestHost の `-file` が使えないため、試験の音はモノラル）。
- 再生の先読みのリーダーは `prepareToPlay` で作る。準備されている間にホストが音声ソースへのアクセスを外して戻したときは、JUCE の `ARAAudioSourceReader` が自分で作り直す（エンジンへ渡す読み出しは DocumentController が `AudioSource` ごとに持ち、音が変わったら読み直す）。
- ソースの読み込みは同期のスレッドで 1 つずつ（長いソースを読んでいる間は、ほかの修飾の編集の反映が待つ）。同じソースの 2 つの修飾は解析が 2 回走る（エンジンのトラックごとのキャッシュ）。
- DAW のテンポ・拍子（`MusicalContext`）はまだエンジンに渡していない（画面のグリッドは画面で直したテンポのまま）。
- ドキュメントを閉じる前に DAW がイベントを 1 つずつ消すと、その分だけ `ara_remove_modification` を呼ぶ（次に開いたとき同じ `ara_id` で足し直し、前の編集のまま戻る）。
- 再生の開始直後（途中から再生を始めたとき）は、その位置の先読みができるまで、最初の 1 ブロック分（32768 サンプル以内）が無音になりうる。
- WebView2 の 2 つ同時・開閉の 20 回の繰り返しは GlissHostCheck `--editor`（JUCE のホストの中・偽の DocumentBridge）で通る（2026-10-03）。保存と再読み込み・2 つの DAW の同時起動・実物の DAW の中での開閉は、確かめていない（JUCE のフォーラムなどに、複数の DAW や複数のインスタンスで固まる報告がある）。危ないと分かったら、エディタをプラグインの窓に埋めず、別ウィンドウの Electron で出す形にする。
- JUCE 9 は AGPLv3、Gliss は GPL-3.0-or-later（GPLv3 §13 と AGPLv3 §13 が結合を認める）。配布物は AGPLv3 の条件になる（上の「配布」の「ライセンス」）。
