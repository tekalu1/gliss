# VST3 + ARA 2 プラグイン

Gliss を DAW の中で使うためのプラグイン（`plugin/`）。DAW のオーディオイベントに ARA の拡張として載り、ホストの音を読み、編集を当てた音を返す。
issue は [tekalu1/gliss#1](https://github.com/tekalu1/gliss/issues/1)。作業の手順は [AGENTS.md](../AGENTS.md)。

状態（2026-10-03）: **段階 1（最小の ARA プラグイン）まで**。ホストの音を素通しで返し、アーカイブ（版つきの空の JSON）を保存・復元し、エディタに WebView2 で静的な HTML を出す。
配布（インストーラがユーザーごとの VST3 の置き場に入れる・配布版のエンジンの見つけ方・ライセンスの表示）は段階 4 の一部として済み（下の「配布」）。
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

### エディタ（`GlissEditor`）

- `WebBrowserComponent`（WebView2、ネイティブ連携あり）に、`plugin/web/index.html`（`juce_add_binary_data` で DLL に埋め込み、resource provider で配る）を出す。
- ARA の `EditorView` で選ばれている `PlaybackRegion` の名前・開始・長さを、JS のイベント `selection` で画面へ送る。画面が読み込まれると `ready` を返す（ここで最新の選択を送る）。
- WebView2 の `userDataFolder` は**プロセスごとの一時フォルダ**（`%TEMP%\GlissARA-<プロセス ID>`）。最後のエディタが閉じたときに消す（WebView2 が掴んでいて消せなければ残る。次に起動したプロセスが、動いていないプロセスの分を消す）。
- ARA ではエディタをリサイズできることが求められるので `setResizable (true, false)`。

### 検証用の環境変数（開発用）

| 名前 | 意味 |
|---|---|
| `GLISS_ARA_TRACE_DIR` | 指すフォルダの `gliss-ara-<プロセス ID>.log` に、プラグインの出来事（アーカイブの保存・復元、レンダラーの準備・解放と集計、エディタの `page ready`）と、再生の記録（`trace` の行。ブロックごとの総和・二乗和）を書く。オーディオスレッドからは書かず、レンダラーの解放のときにまとめて書く |
| `GLISS_ARA_READ_TIMEOUT_MS` | リアルタイムの描画でも先読みの完了をこの ms だけ待つ。検証ホスト（TestHost は CPU の速さで取りに来る）で欠けなく比べるため。普段は使わない |

## ビルド

必要なもの: Visual Studio 2022（C++ のデスクトップ開発。Community でよい）、CMake 3.22 以上、git。管理者権限は要らない（ここの手順はプラグインをどこにもインストールしない。配布用のビルドは `pnpm build:plugin`。下の「配布」）。

```powershell
cmake -S plugin -B plugin/build -G "Visual Studio 17 2022" -A x64
cmake --build plugin/build --config Release --target GlissARA_VST3
# 出来るもの: plugin\build\GlissARA_artefacts\Release\VST3\Gliss.vst3\Contents\x86_64-win\Gliss.vst3
```

最初の構成で JUCE（約 130 MB）・ARA SDK（サブモジュール込み）・WebView2 の NuGet パッケージ（URL とハッシュを固定）を取る。2 回目以降はネットワークが要らない。`COPY_PLUGIN_AFTER_BUILD` は切ってある（`C:\Program Files\Common Files\VST3` に書かない）。
同じ構成が `GlissHostCheck`（検証用のホスト）も作る（`-DGLISS_BUILD_HOSTCHECK=OFF` で外せる）。

DAW に載せて試すときは、`Gliss.vst3` を DAW が探す場所に置く（Fender Studio Pro 8 の VST3 の追加の場所に `plugin\build\GlissARA_artefacts\Release\VST3` を足すか、`%CommonProgramFiles%\VST3` へ管理者権限でコピーする。どちらも未確認で、段階 1 の完了後に実機で確かめる）。
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
- 対応するソースは、このリポジトリの同じ版のタグの `plugin/` と、`plugin/CMakeLists.txt` が版を固定して取る依存（JUCE・ARA SDK・`Microsoft.Web.WebView2` の NuGet）。`Gliss.vst3` の `NOTICE.txt` に場所を書いている。
- 依存のライセンス文: JUCE（`LICENSE.md` と AGPLv3 の全文）・ARA SDK（Apache-2.0。`NOTICE.txt` も）・WebView2 のローダ（BSD-3-Clause）・JUCE が同梱してリンクされる第三者のコード（zlib・HarfBuzz・SheenBidi・LunaSVG・PlutoVG・libpng・jpeglib・libwebp・FLAC・Ogg Vorbis・Opus・VST3 SDK（MIT）・PreSonus の拡張ヘッダ）。
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
powershell -NoProfile -File plugin\scripts\test-plugin.ps1              # ビルドして、下の 3 つを流す（初回は依存の取得と JUCE・ARA_Examples のビルドで数分から 10 分ほど）
powershell -NoProfile -File plugin\scripts\test-plugin.ps1 -SkipBuild   # 流すだけ（約 5 秒）
```

| 検証 | 見ること |
|---|---|
| ARA SDK の **TestHost**（`-vst3 Gliss.vst3`、全 12 項目） | プロパティ更新・コンテンツ更新・読み出し・クローン・アーカイブ・分割アーカイブ・ドラッグ＆ドロップ・再生・EditorView・処理アルゴリズム・音声ファイルのチャンク。終了コード 0 |
| TestHost の `PlaybackRendering` ＋ `verify_render_trace.py` | プラグインが返した音を、SDK の試験信号（5 秒・44.1 kHz のパルス状の正弦波）と、ブロックごとの総和・二乗和で突き合わせる（`tests/verify_render_trace.py`）。全ブロックが一致すること |
| **GlissHostCheck**（`plugin/tests/hostcheck`、JUCE のホスト） | VST3 として見つかる・`hasARAExtension`・ARA ファクトリの ID が決めたとおり・ARA に結び付かない `processBlock` が入力を変えない・エディタを画面の外に作って WebView2 の HTML が読み込まれ `ready` が届く・エディタとインスタンスを閉じて落ちない |

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
| `plugin/src/GlissEditor.*` | WebView2 のエディタ・`selection` の送信・ユーザーデータのフォルダ |
| `plugin/src/Diagnostics.*`・`ProcessUtils.*` | 検証用のログ・環境変数、プロセスの ID と生死 |
| `plugin/web/index.html` | エディタに出す静的な HTML |
| `plugin/tests/hostcheck/` | GlissHostCheck |
| `plugin/tests/verify_render_trace.py` | 再生の記録を試験信号と突き合わせる（numpy が要る） |
| `plugin/scripts/test-plugin.ps1` | ビルドと検証の一式 |

## 段階と残り

実装の計画（2026-10-03 承認）は次の 5 段階。

0. 土台の確認（済）。
1. 最小の ARA プラグイン（このリポジトリの状態）。残り: **Fender Studio Pro 8 で ARA の拡張として開き、鳴るか・保存して開き直せるか・エディタが出るか**（人の許可を取って、親が行う）。
2. エンジンとの接続: DocumentController がエンジンを子プロセスで起動し、`AudioSource` ごとに裏のスレッドでホストの音を読み、`open_project`（offset・length・source_id）→ 解析。編集を当てた音をキャッシュに持ち、PlaybackRenderer はそれを読む。サンプリング周波数の変換。
3. 編集と再生: WebView の `window.gliss` を JUCE のネイティブ関数・イベントで作り直し、画面からの編集 → エンジン → `render_region` → キャッシュの差し替え。アーカイブに編集リスト（`to_archive` / `from_archive`）。リージョンの移動・トリムへの追従。
4. DAW に返すもの・配布: content reader（ノート）。配布（インストーラ・エンジンの exe とモデルの場所・THIRD_PARTY_NOTICES）は済み（上の「配布」。Cubase 用の ARA フォルダは文書の手順だけ）。

### 既知の制約・未解決（段階 1）

- サンプリング周波数がホストと違うソースは鳴らさない（段階 2）。ステレオのソースと、モノラルのソースをステレオのバスで鳴らす経路は、TestHost の `-file` が使えないため自動の検証が無い（コードを読んで確かめただけ）。
- ホストが音声ソースへのアクセスを外して戻したとき（`enableAudioSourceSamplesAccess`）、先読みのリーダーは作り直さない（リージョンの追加・削除と同じく、ARA の規則ではレンダラーが準備されている間は変わらない前提）。段階 2 で `AudioSource` ごとのキャッシュを DocumentController が持つ形にして直す。
- 再生の開始直後（途中から再生を始めたとき）は、その位置の先読みができるまで、最初の 1 ブロック分（32768 サンプル以内）が無音になりうる。
- WebView2 の複数インスタンス・開閉の繰り返し・保存と再読み込み・2 つの DAW の同時起動は、実機で確かめていない（JUCE のフォーラムなどに、複数の DAW や複数のインスタンスで固まる報告がある）。危ないと分かったら、エディタをプラグインの窓に埋めず、別ウィンドウの Electron で出す形にする。
- JUCE 9 は AGPLv3、Gliss は GPL-3.0-or-later（GPLv3 §13 と AGPLv3 §13 が結合を認める）。配布物は AGPLv3 の条件になる（上の「配布」の「ライセンス」）。
