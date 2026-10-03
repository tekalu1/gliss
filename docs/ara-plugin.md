# VST3 + ARA 2 プラグイン

Gliss を DAW の中で使うためのプラグイン（`plugin/`）。DAW のオーディオイベントに ARA の拡張として載り、ホストの音を読み、編集を当てた音を返す。
issue は [tekalu1/gliss#1](https://github.com/tekalu1/gliss/issues/1)。作業の手順は [AGENTS.md](../AGENTS.md)。

状態（2026-10-03）: **段階 1（最小の ARA プラグイン）まで**。ホストの音を素通しで返し、アーカイブ（版つきの空の JSON）を保存・復元し、エディタに WebView2 で静的な HTML を出す。
エンジン（Python）との接続・編集・再生への反映は段階 2 以降（下の「段階」）。実物の DAW（Fender Studio Pro 8 など）での確認はまだ。

## 決まった形

| 項目 | 内容 |
|---|---|
| 手段 | JUCE **9.0.3**（AGPLv3 の側で使う）＋ ARA SDK **2.3.0**（Apache-2.0）＋ WebView2 SDK（NuGet の `Microsoft.Web.WebView2` **1.0.4258.31**、静的リンク）。退路は素の VST3 SDK ＋ ARA_Library |
| 形式 | VST3 のみ（`Gliss.vst3`）。ARA 2（`IS_ARA_EFFECT`）。出力は `plugin/build/GlissARA_artefacts/<構成>/VST3/Gliss.vst3` |
| 製品名・会社名 | `Gliss`・`Gliss`（ホストに見える名前） |
| `BUNDLE_ID` | `io.github.tekalu1.gliss`（アプリの appId と同じ。変えない） |
| `ARA_FACTORY_ID` | `io.github.tekalu1.gliss.arafactory.1`（能力＝解析の種類・再生の変形が変わったら末尾の版を上げる） |
| `ARA_DOCUMENT_ARCHIVE_ID` | `io.github.tekalu1.gliss.aradocumentarchive.1`（保存の形が下位互換でなくなったら上げ、古い ID は `ARA_COMPATIBLE_ARCHIVE_IDS` に残す） |
| ARA の API の世代 | JUCE の既定（`kARAAPIGeneration_2_0_Final`）。部分的な保存（2.3）は使わない |
| 解析・変形の能力 | まだ何も名乗らない（`ARA_ANALYSIS_TYPES`・`ARA_TRANSFORMATION_FLAGS` は既定。ノートを DAW へ返すのは段階 4） |
| 実行時ライブラリ | 静的（`/MT`）。利用者の PC に VC++ 再頒布可能パッケージを要求しない |
| WebView2 | ローダは静的リンク。ランタイムは Evergreen（Windows 11 に入っている）。無いときはエディタに文言を出す作り（ランタイムの無い環境では未確認） |
| 依存の取得 | CMake の `FetchContent` が `plugin/build/_deps` にタグ固定で取る（リポジトリには入れない。`plugin/.gitignore`） |

### 編集の単位・保存

- 編集の単位は ARA の `AudioModification`（`GlissAudioModification`。ソースの時間で持つ）。同じ素材を複製・分割した `PlaybackRegion` は同じ `AudioModification` を共有する（編集を共有する）。クローン（TestHost の `ModificationCloning`）では編集の一覧を写す。
- アーカイブは **版つきの JSON 1 つ**（`doStoreObjectsToStream` / `doRestoreObjectsFromStream`）。解析のキャッシュは入れない。今の形:

```json
{ "format": "gliss-ara-document", "version": 1,
  "audioModifications": [ { "id": "<AudioModification の persistentID>", "edits": [] } ] }
```

  `version` が 1 でない・`format` が違うものは復元に失敗として返す（ホストにエラーが伝わる）。`edits` は段階 3 でエンジンの編集リストにする。

### 再生（`GlissPlaybackRenderer`）

- ホストの音（`ARAAudioSourceReader`）は**裏のスレッドが先読み**（JUCE の `BufferingAudioReader`。全インスタンスで 1 本の `TimeSliceThread`。先読みは 4 秒）。オーディオスレッドはそのバッファを写すだけで、ホストの音の読み出しをしない（先読みスレッドがバッファを入れ替える瞬間の短いロックだけは取る）。
- 先読みが間に合っていない範囲は、**読めたところまでを返し、残りだけを無音にする**（ブロック全体を無音にしない）。ホストが「リアルタイムでない」描画（VST3 の `kOffline`＝バウンス）のときだけ、先読みの完了を最大 500 ms 待つ。常にリアルタイムでないインスタンス（`alwaysNonRealtime`）は先読みせず直に読む。
- ドキュメントの編集中（`willBeginEditing`〜`didEndEditing`）は、オーディオスレッドが待たずに（`ScopedTryReadLock`）そのブロックを無音にする。
- ソース（ホストの音）とホストの描画のサンプリング周波数が違うリージョンは**まだ鳴らさない**（ログに残す）。段階 2 でエンジンの soxr で合わせる。チャンネルはソースのまま写す（モノラルのソースはステレオの両方に入れる）。
- ARA に結び付かない（普通の VST3 として読み込まれた）ときは、入力をそのまま通す。

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
| `GLISS_PLUGIN_WEB_DIR` | 既にあるフォルダ（`<repo>\app\renderer`）を指すと、エディタは画面の資源（`index.html`・`*.js`・`ara-bridge.js`）を埋め込みでなくそのフォルダから要求のたびに読む（ビルドし直さずに画面を直せる）。`ara-bridge.js` は user script なのでエディタを開き直したときに読み直す。このときは F5・Ctrl+R をブラウザの再読み込みに残す（DAW へ渡さない） |

## ビルド

必要なもの: Visual Studio 2022（C++ のデスクトップ開発。Community でよい）、CMake 3.22 以上、git。管理者権限は要らない（プラグインはどこにもインストールしない）。

```powershell
cmake -S plugin -B plugin/build -G "Visual Studio 17 2022" -A x64
cmake --build plugin/build --config Release --target GlissARA_VST3
# 出来るもの: plugin\build\GlissARA_artefacts\Release\VST3\Gliss.vst3\Contents\x86_64-win\Gliss.vst3
```

最初の構成で JUCE（約 130 MB）・ARA SDK（サブモジュール込み）・WebView2 の NuGet パッケージ（URL とハッシュを固定）を取る。2 回目以降はネットワークが要らない。`COPY_PLUGIN_AFTER_BUILD` は切ってある（`C:\Program Files\Common Files\VST3` に書かない）。
同じ構成が `GlissHostCheck`（検証用のホスト）も作る（`-DGLISS_BUILD_HOSTCHECK=OFF` で外せる）。

DAW に載せて試すときは、`Gliss.vst3` を DAW が探す場所に置く（Fender Studio Pro 8 の VST3 の追加の場所に `plugin\build\GlissARA_artefacts\Release\VST3` を足すか、`%CommonProgramFiles%\VST3` へ管理者権限でコピーする。どちらも未確認で、段階 1 の完了後に実機で確かめる）。
Cubase は ARA のプラグインを `%CommonProgramFiles%\ARA` に置く必要があるという報告がある（Steinberg のフォーラムの報告。現行の条件は未確認）。

## 検証

```powershell
powershell -NoProfile -File plugin\scripts\test-plugin.ps1              # ビルドして、下の検証を流す（初回は依存の取得と JUCE・ARA_Examples のビルドで数分から 10 分ほど）
powershell -NoProfile -File plugin\scripts\test-plugin.ps1 -SkipBuild   # 流すだけ（約 1 分）
```

| 検証 | 見ること |
|---|---|
| ARA SDK の **TestHost**（`-vst3 Gliss.vst3`、全 12 項目） | プロパティ更新・コンテンツ更新・読み出し・クローン・アーカイブ・分割アーカイブ・ドラッグ＆ドロップ・再生・EditorView・処理アルゴリズム・音声ファイルのチャンク。終了コード 0 |
| TestHost の `PlaybackRendering` ＋ `verify_render_trace.py` | プラグインが返した音を、SDK の試験信号（5 秒・44.1 kHz のパルス状の正弦波）と、ブロックごとの総和・二乗和で突き合わせる（`tests/verify_render_trace.py`）。全ブロックが一致すること |
| **GlissHostCheck**（`plugin/tests/hostcheck`、JUCE のホスト） | VST3 として見つかる・`hasARAExtension`・ARA ファクトリの ID が決めたとおり・ARA に結び付かない `processBlock` が入力を変えない・ARA に結び付かないエディタは画面を出さずに案内を出す・エディタとインスタンスを閉じて落ちない |
| **GlissPluginTests**（`plugin/tests/unit`） | 単位ごとの単体テスト（カテゴリ `Gliss`）。エディタは `WebResources`（`/fs/` の decode と拒否・資源の振り分け・開発時のフォルダ） |
| **GlissHostCheck `--editor`**（`EditorCheck.h`・`FakeDocumentBridge.h`） | Gliss.vst3 を読まず、エディタの画面の部品（`plugin/src/editor`）を**偽の DocumentBridge** につないでこのプロセスの中で画面の外に開く: `app/renderer` が読み込まれ `ui-ready` が来る（その前の知らせは捨てる）・`window.api` と `data-mode=ara`・user script から `/juce/index.js` の動的 import・`engineCall` の往復（`{ok:false}` も値で・別スレッドの completion も）・ほかのネイティブ関数・`/fs/`（空白・`%`・`+`・日本語の名前を読める／外・`..`・無い・フォルダ・知らない資源は拒否）・知らせ 6 種が画面の受け手に届く・F8 は窓へ渡り Space は渡らない・応答の前に閉じても落ちない・2 つ同時に 20 回開閉。`--expect-web-dir` で `GLISS_PLUGIN_WEB_DIR` から読むこと |

どの検証もタイムアウトを持ち、終わりに起動したプロセスを木ごと止めて、残りが 0 であることを確かめる。GlissHostCheck の窓は画面の外に置き、`SW_SHOWNA`（前面にも入力の対象にもならない）で出す。

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
- TestHost は VST3 の `processMode` を `kRealtime` にして CPU の速さで描画する。先読みが間に合わないブロックが多く出る（普通の再生ではない）。このため、突き合わせの検証では `GLISS_ARA_READ_TIMEOUT_MS` で待たせる。待たない場合の挙動は、ブロックが欠ける（無音になる）だけで、読めた部分は正しい。
- JUCE のホスト側で、`AudioPluginFormatManager::createARAFactoryAsync(説明, ...)` で ARA ファクトリを取ると、DLL のハンドルを持たずに取り、ファクトリを手放すときに外れた DLL の中を呼んで落ちる（JUCE の ARAPluginDemo でも同じ）。**インスタンスを先に作り、`juce::createARAFactoryAsync (*instance, ...)` で取る**（AudioPluginHost と同じ）。

## 構成

| 場所 | 内容 |
|---|---|
| `plugin/CMakeLists.txt` | 依存の取得・`juce_add_plugin`・オプション |
| `plugin/src/GlissProcessor.*` | `AudioProcessor`（`createPluginFilter`・`createARAFactory` もここ） |
| `plugin/src/GlissDocumentController.*` | ARA の `DocumentController`（`AudioModification` の差し替え・アーカイブ・編集中のロック）。段階 2 でエンジンの接続をここに 1 つだけ持たせる |
| `plugin/src/GlissPlaybackRenderer.*` | 素通しの再生（先読み・部分的に返す・オフラインの待ち・検証の記録） |
| `plugin/src/GlissEditor.*` | プラグインのエディタ（`DocumentBridge` を得て画面を出す・選択を渡す・ARA でないときの案内） |
| `plugin/src/editor/` | 画面の橋: `EditorWebView`（WebView2・ネイティブ関数・知らせ・ユーザーデータのフォルダ）・`WebResources`（resource provider）・`EmbeddedAssets`・`KeyForwarding`・`key-forward.js` |
| `plugin/src/ara/DocumentBridge.h` | エディタがドキュメントに頼む口（`GlissDocumentController` が実装する） |
| `plugin/src/Diagnostics.*`・`ProcessUtils.*` | 検証用のログ・環境変数、プロセスの ID と生死 |
| `plugin/tests/hostcheck/` | GlissHostCheck（`--editor` の偽の DocumentBridge も） |
| `plugin/tests/verify_render_trace.py` | 再生の記録を試験信号と突き合わせる（numpy が要る） |
| `plugin/scripts/test-plugin.ps1` | ビルドと検証の一式 |

## 段階と残り

実装の計画（2026-10-03 承認）は次の 5 段階。

0. 土台の確認（済）。
1. 最小の ARA プラグイン（このリポジトリの状態）。残り: **Fender Studio Pro 8 で ARA の拡張として開き、鳴るか・保存して開き直せるか・エディタが出るか**（人の許可を取って、親が行う）。
2. エンジンとの接続: DocumentController がエンジンを子プロセスで起動し、`AudioSource` ごとに裏のスレッドでホストの音を読み、`open_project`（offset・length・source_id）→ 解析。編集を当てた音をキャッシュに持ち、PlaybackRenderer はそれを読む。サンプリング周波数の変換。
3. 編集と再生: WebView の `window.gliss` を JUCE のネイティブ関数・イベントで作り直し、画面からの編集 → エンジン → `render_region` → キャッシュの差し替え。アーカイブに編集リスト（`to_archive` / `from_archive`）。リージョンの移動・トリムへの追従。
4. DAW に返すもの・配布: content reader（ノート）、インストーラ（`Common Files\VST3`・Cubase 用の ARA フォルダ）、エンジンの exe とモデルの場所、THIRD_PARTY_NOTICES（JUCE・ARA SDK・VST3 SDK・WebView2）。

### 既知の制約・未解決（段階 1）

- サンプリング周波数がホストと違うソースは鳴らさない（段階 2）。ステレオのソースと、モノラルのソースをステレオのバスで鳴らす経路は、TestHost の `-file` が使えないため自動の検証が無い（コードを読んで確かめただけ）。
- ホストが音声ソースへのアクセスを外して戻したとき（`enableAudioSourceSamplesAccess`）、先読みのリーダーは作り直さない（リージョンの追加・削除と同じく、ARA の規則ではレンダラーが準備されている間は変わらない前提）。段階 2 で `AudioSource` ごとのキャッシュを DocumentController が持つ形にして直す。
- 再生の開始直後（途中から再生を始めたとき）は、その位置の先読みができるまで、最初の 1 ブロック分（32768 サンプル以内）が無音になりうる。
- WebView2 の 2 つ同時・開閉の 20 回の繰り返しは GlissHostCheck `--editor`（JUCE のホストの中・偽の DocumentBridge）で通る（2026-10-03）。保存と再読み込み・2 つの DAW の同時起動・実物の DAW の中での開閉は、確かめていない（JUCE のフォーラムなどに、複数の DAW や複数のインスタンスで固まる報告がある）。危ないと分かったら、エディタをプラグインの窓に埋めず、別ウィンドウの Electron で出す形にする。
- JUCE 9 は AGPLv3、Gliss は GPL-3.0-or-later（GPLv3 §13 と AGPLv3 §13 が結合を認める）。配布物は AGPLv3 の条件になる。THIRD_PARTY_NOTICES への記載は段階 4。
