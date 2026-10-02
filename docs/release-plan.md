# Gliss の公開・配布・自動更新の計画

作成: 2026-10-02（公開までのギャップ調査（2026-09-23。非公開）の配布・ライセンス・CI・公開の準備の実行計画。決めたこと・順番・未決との対応をまとめる）

ゴール: **GitHub の public リポジトリから、タグを打つだけで Windows のインストーラが GitHub Releases に並び、利用者のアプリが自分で新しい版を見つけて更新できる**。ソース・ライセンス文・リリースノートが同じ場所にそろう（GPL-3.0-or-later）。

ギャップ調査が「何が足りないか」の表なのに対して、ここは「どう進めるか」。調査の文書は内部向けなので公開リポジトリには入れておらず、この文書だけで読めるように書く。

## 0. 決まっていること（2026-10-02）

| 項目 | 決定 | 理由・補足 |
|---|---|---|
| 1.0 の対応 OS | **Windows のみ**（x64） | issue #8 の未決 4。macOS・Linux は「ソースから動かす（best effort）」と明記し、配布版は需要が出てから |
| インストーラ | electron-builder の **NSIS・ユーザー単位**（`perMachine: false`） | 管理者権限が要らず、自動更新（electron-updater）が静かに入れ替えられる。インストール先は `%LOCALAPPDATA%\Programs\Gliss` |
| 識別子 | appId `io.github.tekalu1.gliss`・productName `Gliss`・実行ファイル名 `Gliss.exe` | **最初に決めて以後変えない**。変えると自動更新が別のアプリ扱いになり、ピン留め・通知・userData（`%APPDATA%\Gliss`）が切れる（Pleiad が ADR 0019 で同じ理由で識別子を据え置いている） |
| 成果物の名前 | `Gliss-${version}-${os}-${arch}.${ext}`（例 `Gliss-0.1.0-beta.1-win-x64.exe`） | |
| エンジン | PyInstaller のフォルダ形式（`engine/packaging/vocal-engine.spec`・torch 無し）を `resources/engine/vocal-engine/` に同梱 | 起動 0.7 秒。1 ファイル形式は毎回 150 MB を展開して 3 秒超（`docs/daw-stage0.md` §4） |
| 重み | **同梱しない**。初回の「モデルの準備」画面が `%LOCALAPPDATA%\Gliss\models` へ取得 | `app/renderer/first-run.js`・`app/model-download.mjs`（実装済み） |
| 任意の依存（漢字の歌詞の読み） | **インストーラに入れない**。「アドオン」として同じ画面（とヘルプ > モデルと追加の機能…）から `%LOCALAPPDATA%\Gliss\addons` へ取得。配布元は同じ版の Release の添付 | §11（実装済み）。聞き取り（faster-whisper）はライセンスの問題でまだ配らない（§11-6） |
| テスト素材 | 同梱しない・公開リポジトリにも入れない | §1 |
| 版の正本 | `app/package.json` の `version` **1 か所** | エンジン側（`engine/pyproject.toml`・`vocal_engine.__version__`）は `scripts/sync-version.mjs` で写し、`engine/tests/test_config.py` が一致を確かめる |
| 自動更新 | electron-updater・公開リポジトリの GitHub provider・認証なし | 実装済み（第 2 段階。§4） |
| 署名 | **最初は無署名**。SignPath Foundation に申請（§5） | |

## 1. 公開するもの・しないもの

**公開は新しい public リポジトリに、掃除した状態の 1 コミットで始める。** 今の `tekalu1/gliss` は private のまま開発の履歴の保管庫にする（Pleiad も「履歴なしの root コミットで新しい public リポジトリへ」で公開した）。理由: 履歴にはコミットのメールアドレス（219 コミット）・個人のパス・第三者の素材名・歌詞の引用が残っていて、可視性を切り替えるだけでは全部露出する。履歴の書き換え（`git filter-repo`）より、新しく作る方が確実で簡単。公開前に、旧履歴に対して秘密の走査（gitleaks 等）を 1 回かけておく（これまでの走査は正規表現のみ）。

### 入れるもの（製品部分だけ）

| 場所 | 中身 |
|---|---|
| `engine/` | Python エンジン（`vocal_engine`）・テスト・`packaging/`（spec・固定した依存） |
| `app/` | Electron の画面・テスト・`electron-builder.yml` |
| `assets/logo/` | ロゴ・アイコン |
| `docs/` | 利用者向け・開発者向けの文書（`release-plan.md` を含む。素材名・歌詞・個人パスを除いたもの。内部向けのギャップ調査は入れない） |
| `scripts/` | ビルド・リリース用（`build-engine.mjs`・`sync-version.mjs`・`smoke-packaged.mjs`・`smoke-update.mjs`・`release-*.mjs`・`third-party-notices.mjs`・`gpl-sources.mjs`・`build-addon.mjs`・`addon-catalog.mjs`・`smoke-addon.mjs`） |
| `releases/` | リリースノートの正本（`<version>.json`。§3） |
| ルート | `LICENSE`・`README.md`（公開向けに書き直す）・`SECURITY.md`・`.github/` |

### 入れないもの

- `stage0/`・`stage2/`・`stage3-render/`（評価用。NC の重みを前提にしたコード・素材由来の JSON・A/B ページを含む）と、`REPORT-*.md` などの開発の記録。製品が `stage0/models`・`stage0/clips` を参照している箇所は、公開前に外す（モデルの既定は `%LOCALAPPDATA%\Gliss\models`、開発時は環境変数 `VOCAL_ENGINE_MODELS_DIR`）。
- 素材由来のスクリーンショット（`app/docs/*.png`）・歌詞の引用・個人のパス（個人のクラウドドライブ・開発機の絶対パス）。差し替えるか消す。
- `.mcp.json`（開発機の絶対パス）。例（`engine/.mcp.json.example`）だけ置く。画面の開発時の起動は、`.mcp.json` が無ければ `<repo>/.venv/Scripts/python.exe -m vocal_engine.mcp` を既定にした（`app/paths.mjs`）。

### テスト素材は同梱しない

素材が要るテストは、**環境変数 `GLISS_TEST_MATERIALS` で素材のある場所を指定し、無ければ skip** にする（`docs/testing.md`）。CI は素材なしで回る範囲だけを回す。権利のはっきりした公開用の素材（合成音、または本人の短いフレーズの CC0 / CC BY）は別に用意する。配布版の確認には、`scripts/smoke-packaged.mjs` が**合成した WAV**を使う（素材はコードで作るのでリポジトリに入れない）。

## 2. 識別子と版

- 版は SemVer。1.0 まで `0.x.y`。先行版は `0.x.y-beta.N`（タグ `v0.x.y-beta.N`）。`pyproject.toml` には PEP 440 に直した版（`0.1.0b1`）を書く。
- タグ `v<version>` は `app/package.json` の `version` と一致していること（CI がタグ名と照合して、ずれたら止める）。タグは `main` の上にあること。
- 一度配布した版のファイルは**上書きしない**（既存のリリースがあれば公開を拒む）。直すときは版を上げる。

## 3. ビルドと配布

### ビルド（実装済み。第 1 段階）

```
pnpm build:engine     # uv で Python 3.13 の .venv-exe を作り、requirements-exe.txt（ハッシュ付きの固定）で入れて PyInstaller → --check
pnpm dist:dir         # 展開版 app/dist/win-unpacked/Gliss.exe（インストールせずに動かせる）
pnpm dist             # NSIS のインストーラ app/dist/Gliss-<版>-win-x64.exe・.blockmap・latest.yml（build:engine を先に走らせる）
node scripts/smoke-packaged.mjs [--models <重みのフォルダ>]   # 展開版を起動して、同梱のエンジンへ MCP で接続し解析まで確かめる
node scripts/smoke-update.mjs --old <古い版の win-unpacked> --new <新しい版の出力>   # 更新の通し（§4。インストールはしない）
pnpm build:addons     # 任意のアドオンの zip（engine/packaging/dist-addons/。§11）。dist:dir / dist の前に作ると目録がアプリに入る
node scripts/smoke-addon.mjs --models <重みのフォルダ>   # 展開版でアドオンの取得・読み込み・互換・削除の通し（ローカルの HTTP サーバー）
```

`dist:dir` と `dist` は electron-builder の前に `app/release-info.json`（アプリ内の「更新の内容」）と `app/THIRD_PARTY_NOTICES.txt`（同梱した依存のライセンス文。`resources/` に入る）を作る（`pnpm prepack:files`。どちらも生成物で、コミットしない）。`--dir` の展開版には `resources/app-update.yml` が入らない（electron-builder は nsis のときだけ作る）ので、自動更新は「この版は自動で更新できません」になる。

依存の固定: `engine/packaging/requirements-exe.in`（直接の依存。torch は入れない）→ `uv pip compile --generate-hashes` で `requirements-exe.txt`。GPL の「対応するソース」にはビルドの手順（スクリプト）も含まれるので、これらは公開リポジトリに入れる。

### CI（実装済み。第 2 段階。実行はまだ）

- **`.github/workflows/release.yml`**: タグ `v*` の push（と、既存のタグを指定した手動実行。main から）。工程:
  1. 確かめる: 所有者が走らせた（`github.actor == github.repository_owner`）・タグの形（`^v\d+\.\d+\.\d+(-beta\.\d+)?$`）・タグが `main` の上（`git merge-base --is-ancestor`）・同じ Release がまだ無い・`scripts/release-check.mjs --tag`（タグ＝`app/package.json` の版、engine 側の写し、`releases/<版>.json` があり直前の版と同じでない）
  2. 作る: `scripts/build-engine.mjs`（uv）→ 任意のアドオン（`scripts/build-addon.mjs --all`。作った exe に読ませて確かめる。§11）→ 単体テスト（`pnpm test:unit`）→ リリースノートの Markdown・アドオンの目録（`scripts/addon-catalog.mjs --require`）・THIRD_PARTY_NOTICES → `electron-builder --win nsis --publish never` → 展開版のスモーク（重みなしで起動して同梱のエンジンにつながる）
  3. そろえる（`scripts/release-assets.mjs`）: インストーラ・`.blockmap`・`latest.yml`（版と SHA-512・大きさを照らす）・`THIRD_PARTY_NOTICES.txt`・copyleft のパッケージの sdist（`scripts/gpl-sources.mjs`）・アドオンの zip（アプリに埋めた目録と大きさ・SHA-256 を照らす）・`SHA256SUMS.txt`
  4. 公開する（`scripts/release-publish.mjs`）: **下書きで作って全部載せてから公開**（載せている途中の Release を自動更新が見ないように）。beta は `--prerelease` で「Latest」にしない、正式版は「Latest」。既にあれば止める
- **electron-builder 自身にはアップロードさせない**（Pleiad と同じ。成果物の検証を挟むため）。`permissions` は既定 `contents: read`、公開のジョブだけ `contents: write`。アクションはコミットの SHA で固定。
- **`.github/workflows/test.yml`**: PR と `main` への push。`engine`（ubuntu）は素材・重みなしの pytest（要るものは skip）、`app`（windows）は `sync-version --check`・`release-check`・単体テスト（`app/tests/unit`。Electron を起動しない）。Electron を起動する Playwright（`app/tests/*.spec.js`）はエンジンの venv と素材が要るので回さない（開発機で回す）。`secrets`（ubuntu）は gitleaks で全履歴の秘密情報を探す（gitleaks 本体を公式 Release から取り、SHA-256 を照らす。gitleaks-action は v2 から独自のライセンスなので使わない）。
- 公開リポジトリなら GitHub ホストのランナー（Windows を含む）は無料。

### リリースの手順（人がすること）

1. `app/package.json` の `version` を上げる（例 `0.1.0-beta.2`）→ `node scripts/sync-version.mjs`（engine 側に写す）
2. `releases/<版>.json` を書く（`title`・`date`・`sections[].items[]`）→ `node scripts/release-check.mjs`
3. コミットして `main` に入れる → `git tag v<版>` → `git push origin v<版>`（release.yml が走る）

### リリースノート（実装済み）

- **正本は `releases/<version>.json`**（`version`・`title`・`date`・`sections[].items[]`）。`scripts/release-notes.mjs` が、アプリ内の表示用（`app/release-info.json`。いまの版までのノート）と Release の本文（Markdown。`--markdown <file>`）を作る。`--generate-notes` は使わない。直前の版と題・中身が同じなら `release-check.mjs` が止める。
- copyleft のパッケージ（parselmouth＝GPL-3.0-or-later、soxr＝LGPL-2.1-or-later、certifi＝MPL-2.0。エンジン exe の venv のメタデータで判定）の対応するソース（PyPI の sdist。SHA-256 を照らす）と、THIRD_PARTY_NOTICES を Release に添付する。Gliss 自身のソースは GitHub がタグの「Source code」として付ける。

## 4. 自動更新（electron-updater。実装済み。第 2 段階）

方針（ギャップ調査の段階の案「最初は『新しい版があります』の通知だけ」を、**自動適用まで**に格上げする）。main 側は `app/updates.mjs`（状態機械。Electron 非依存）・`app/update-provider.mjs`・`app/engine-processes.mjs`・`app/main.mjs`、画面は `app/renderer/updates.js`。

| 項目 | 方針（実装） |
|---|---|
| 配信 | 公開リポジトリの GitHub Releases（`publish: { provider: github, owner: tekalu1, repo: gliss, channel: latest }`・認証なし）。private provider と `gh auth token` の層は要らない（Pleiad は private のためそれを持つ。持ち込まない）。各 Release の更新情報は beta の版でも `latest.yml`（`channel: latest`。既定のままだと版の `-beta` から `beta.yml` を作る） |
| 有効になる条件 | 配布版（`app.isPackaged`）で `resources/app-update.yml` があるとき。開発版・`--dir` の展開版は `unavailable`（electron-updater を作らない） |
| チャネル | **チャネル名ではなく GitHub Release の prerelease 印で分ける**。設定 `channel: stable / beta` → `allowPrerelease`。版が beta のアプリは既定で beta。安定版の利用者に beta は配らない |
| ダウングレード | **`allowDowngrade` は常に `false`**。electron-updater は `channel` を設定すると `allowDowngrade` を `true` にする（`AppUpdater` の `set channel`）ので、`updater.channel` は触らず、`allowPrerelease` を変えた後・確認の直前にも `false` に戻す |
| 最新の選び方 | stable は `/releases/latest`（GitHub が「Latest」とした正式版）。beta は electron-updater の `GitHubProvider` だと `releases.atom` の**並びの先頭から最初に合う版**を取り、版の大小を比べない。GitHub の並びは版の順ではない（作成日時の順でもない。2026-10-02 に Pleiad の一覧で、後から作った `android-v0.1.0-676` が `beta.58` の後ろに並ぶのを確認。Pleiad は `beta.9` が `beta.11` より前に並んだ）。そこで feed の版を **semver で比べて一番新しいもの**を選び直す（`update-provider.mjs`。feed は直近 10 件） |
| 確認 | 起動の 15 秒後・4 時間おき・ウィンドウに戻ったとき（前回から 4 時間以上）・手動（ヘルプ > 更新を確認…）。**設定で自動確認・自動ダウンロードをオフにできる**（既定はオン）。設定は `userData/updates.json`（一時ファイル → rename） |
| 適用 | ダウンロード（と SHA-512 の検証）が終わったら、タイトルバーの右に「**再起動して更新**」。**黙って再起動しない**。終了時の自動適用（`autoInstallOnAppQuit`）もしない |
| 再起動の前 | (1) インストール先のエンジン exe で動く**ほかのプロセス**（AI クライアントが起動したもの）があれば「AI クライアントから使っているエンジンも止まります」と確かめる → (2) 未保存の「保存しますか」（キャンセルなら中止）→ (3) 画面のエンジンを止める → (4) `resources/engine/vocal-engine/` の下の `vocal-engine.exe` をすべて止める（第 1 段階の申し送りの案 A）→ (5) `quitAndInstall(true, true)`（画面を出さずに入れ替え、終わったら起動し直す。入れ先は前回のまま） |
| インストーラ側 | electron-builder 26 の NSIS の既定（`CHECK_APP_RUNNING`）が、PowerShell が使えれば**インストール先の下で動くプロセス**（Path が `$INSTDIR` で始まるもの）を探して止める。ただし判定が `(Get-CimInstance … \| ? {…}).Count -gt 0` で、**該当が 1 つだけのとき** Windows PowerShell 5.1 では `.Count` が `$null` になり「動いていない」と見なす。AI クライアント 1 つのエンジン（`resources/engine/vocal-engine/vocal-engine.exe`）だけが動いている状態がこれに当たり、止められないまま古い版のアンインストール（ファイルを順に退避し、掴まれていれば戻して終了コード 2）が 5 回失敗して「Glissが終了できません」→「古いアプリケーションファイルのアンインストールに失敗しました: 2」になる（実機で確認。静かなモードは約 9 分後に終了コード 2 で何も入らない）。そこで `app/build/installer.nsh` の `customCheckAppRunning` で、`$INSTDIR\resources\engine\` の下のプロセスを**先に止めてから**既定を呼ぶ（Gliss の画面は複数のプロセスなので既定のままで見つかる）。`customCheckAppRunning` を定義するとテンプレートは `getProcessInfo.nsh` と `Var pid` を入れないので installer.nsh が自前で入れる。`app/tests/unit/release.spec.js` の R6 がテンプレートの形と installer.nsh を確かめる（electron-builder を上げたら見直す） |
| アンインストール | 既定では `%LOCALAPPDATA%\gliss-updater`（`installer.exe` ＝差分更新のためのインストーラの写しと、取得した更新 `pending\`。計約 440 MB）が残る。`app/build/installer.nsh` の `customUnInstall` で消す。**`--updated` が付いていないとき（利用者のアンインストール）だけ**: 新しい版のインストーラが古い版のアンインストーラを呼ぶときは（アプリからの更新も手動の上書きも）常に `--updated` が付き、アプリからの更新では動いているインストーラ自身が `pending\` にある。重み・設定（`%APPDATA%\Gliss`・`%LOCALAPPDATA%\Gliss`）は消さない（`deleteAppDataOnUninstall: false`）。`app/tests/unit/release.spec.js` の R6b |
| 初回インストール | 更新の通知は出さない。更新した後の最初の起動で、1 回だけ「<版> に更新しました」と「内容」（`release-info.json` のこの版のノート） |
| 失敗 | 生のエラーは見せず 3 種類にまとめる: 検証（署名・チェックサム）「更新のファイルを確かめられませんでした。公式のページ（GitHub の Releases）から入れ直してください」、ネットワーク、その他。公開された版が無い（stable を選んだが正式版がまだ無い）は失敗にせず「最新です」。自動の確認の失敗はタイトルバーに出さない（ダイアログにだけ）。リトライは次の確認で。生のエラーは `userData/updates.log` |
| 無署名の間 | 署名検証はしない（`win.verifyUpdateCodeSignature: false`。`app-update.yml` に `publisherName` が無いと electron-updater は検証を飛ばす）。SHA-512 は `latest.yml` に入るので、ダウンロードの完全性は見る。**署名を足す版から、発行元名を固定して検証を有効にする**（§5） |
| 差分 | `.blockmap` を作る（設定済み）。差分の取得は、前の版の Release の `.blockmap`（同じリポジトリの `download/v<前の版>/…blockmap`）を使う。無ければ全体を取る（ローカルの確認では全体を取った）。エンジン exe は毎回ほぼ丸ごと変わるので、差分の効きは実地（第 4 段階）で見る |

## 5. 署名

- **最初は無署名**で出す。SmartScreen の「Windows によって PC が保護されました」が出るので、README に回避の手順と SHA-256 の照合方法を書く。
- **SignPath Foundation**（OSS 向けの無料署名。GitHub Actions から署名）に申請する。審査待ちの間も無署名で出し続けてよい。Microsoft の Trusted Signing（Public Trust）は、個人は米国・カナダのみ、日本は法人だけが対象（Pleiad の調査 2026-09-18）なので、個人の開発では使えない見込み。
- **発行元名（証明書の Common Name）は一度付けたら変えない。** electron-updater は署名の発行元名を照合するので、変えると既存の利用者が更新できなくなる。鍵を替えても同じ名前を使う。
- 署名しても SmartScreen の警告が消えることは保証されない（評判が付くまで出る）。

## 6. 重み（5-B）

- **RMVPE は再配布しない。** こちらの Release に重みを置かず、上流（yxlllc/RMVPE の GitHub Releases）から取得する。取得画面でライセンスが不明（「記載なし」「未確認」）と**そのまま出す**（実装済み）。
- 上流（yxlllc/RMVPE、wolfgitpr/HubertFA）に Issue で許諾を問い合わせる（**最初の週に出す**）。回答が来なければ、**1.0 までに結論**を出す: 「ライセンス不明と表示して任意ダウンロード」か「代替の F0（重みの要らない Praat の自己相関ピッチや pYIN、許諾のはっきりした学習済みの FCPE・SwiftF0 など）が育つまで既定から外す」か（issue #8 の未決 6）。
- 重み無しでも落ちないこと（第 1 段階で確認・修正済み: 取得画面へ案内する）。重み無しで使える範囲を広げる代替 F0（Praat の自己相関ピッチなど）は別の作業。

## 7. 進める順番

| 段階 | 中身 | 状態 |
|---|---|---|
| **1. 配布版を作る** | 配布版のエンジン起動・置き場、エンジン exe の再現ビルド、electron-builder（NSIS）、展開版での確認、本書 | 済み |
| **2. 自動更新** | electron-updater（§4）、publish 設定、更新の設定画面と「再起動して更新」、更新時のエンジン停止・保存、AI 側エンジン exe の扱い、リリースのワークフロー（§3） | 済み |
| **3. public リポジトリを作る** | §1 の掃除（素材・歌詞・個人パス・スクリーンショット）、公開向け README・SECURITY.md・About の法的告知（THIRD_PARTY_NOTICES とタグ起点の CI は第 2 段階で用意済み）、テスト素材を環境変数化、新リポジトリに 1 コミットで push | 済み（2026-10-03） |
| **4. 実地の更新確認** | **beta.1・beta.2 を実際に GitHub Releases に出し**、beta.1 を入れた実機で beta.2 への更新が通ることを確かめる（クリーンな Windows。日本語・空白を含むユーザー名のパスも）。ここまで済んでから 1.0 の議論 | beta.1 → beta.2 は通った（2026-10-03。GitHub Releases から差分で取得し、「再起動して更新」で約 30 秒）。日本語・空白を含むユーザー名・入れ先を変えたとき・AI クライアントのつなぎ直しは未確認 |

順番の理由: 配布版が動かないと自動更新の確認ができない（1→2）。公開リポジトリが無いと、更新の配信元（GitHub Releases）で実地の確認ができない（3→4）。電子署名は 4 までは無署名のまま進め、並行して申請する。

## 8. issue #8 の未決との対応

| # | 未決 | この計画での扱い |
|---|---|---|
| 4 | 1.0 の対応範囲 | **Windows のみ・日本語のみ**で進める（§0）。issue #8 で確定する |
| 5 | テスト素材 | **同梱しない・入れない**。環境変数で場所を指定、無ければ skip（§1）。公開用の素材（合成音／本人の CC0・CC BY）は 3 段階の中で用意 |
| 6 | ライセンス不明の重みの扱い | 上流に問い合わせ、**1.0 までに結論**（§6）。当面は「不明と表示して任意ダウンロード」 |
| 7 | 著作権表示の名前 | **`tekalu` に決めた**（2026-10-03）。`app/package.json` の `author`（exe のプロパティの発行元・著作権表示、NSIS の発行元）は `tekalu`。About・README・THIRD_PARTY_NOTICES の前置きは公開の準備（第 3 段階）でそろえる |
| 8 | コミットのメールアドレス | 新しい public リポジトリは 1 コミットなので、**GitHub の noreply アドレスで作る**ことにすれば決着する。決めるのはユーザー |
| 9 | 公開用リポジトリ | **新しく作って 1 コミットで始める**（§1）。旧リポジトリは private で保管 |

## 9. 第 1 段階で分かった・残したもの

- 配布版（`app.isPackaged`）はエンジン exe を `resources/engine/vocal-engine/vocal-engine.exe` から起動する（`app/paths.mjs`）。AI クライアントに登録するコマンドも同じ exe（`app/ai-connect.mjs`）。
- 単体 exe の重みの既定を exe の隣の `models/` から `%LOCALAPPDATA%\Gliss\models` に変えた。Claude Code に登録した exe には画面の環境変数が渡らないので、既定を画面の取得先に揃える必要があった。
- この exe には**任意の依存（pyopenjtalk-plus＝漢字の歌詞の読み、faster-whisper＝聞き取り、pyworld）を入れていない**。漢字の歌詞は読めず（かな歌詞は動く）、聞き取りは無効になる。入れると約 320 MB（sudachidict 208 MB）増える。→ **漢字の歌詞の読みは exe に入れず、画面から後で取得するアドオンにした**（§11）。
- `THIRD_PARTY_NOTICES` の置き場は `resources/THIRD_PARTY_NOTICES.txt`（第 2 段階で `scripts/third-party-notices.mjs` が作るようにした）。Electron の `LICENSE.electron.txt`・`LICENSES.chromium.html` はインストール先の直下に自動で入る（確認済み）。

## 10. 第 2 段階で分かった・残したもの

- `--dir` の展開版には `app-update.yml` が無い（自動更新は unavailable）。更新を試すときは nsis の出力の `win-unpacked` を使うか、`scripts/smoke-update.mjs` のように足す。
- 画面が起動したエンジン（`console=True` の exe）はコンソールの窓を出さない（MCP SDK の `StdioClientTransport` が Windows で `windowsHide` を付ける。窓の無い conhost が付くだけ）。AI クライアントが起動するときの窓はクライアント次第。
- 更新の適用は `/S`（画面なし）なので、入れ替えの間（数十秒）は何も見えずに Gliss が起動し直す。長いようなら Pleiad のように進捗だけ見せるインストーラの画面（`customFinishPage` など）を検討する（第 4 段階で時間を測る）。
- 実機でしか確かめられないこと（インストール・`quitAndInstall`・AI クライアントがエンジンを掴んだ状態の更新）は第 4 段階で。

## 11. 任意機能のアドオン（実装済み。漢字の歌詞の読み）

配布版のエンジン exe に入れていない大きな任意の依存を、**重みと同じように画面から後で取得して使う**仕組み。最初のアドオンは「漢字の歌詞の読み」（`lyrics-ja`。pyopenjtalk-plus・SudachiPy・SudachiDict。zip 約 94 MB・展開後 317 MB）。無ければ今までどおり、かなの歌詞は動き、漢字の歌詞は「ヘルプ > モデルと追加の機能… から入れる」と案内する。

### 11-1. 中身と作り方

| 項目 | 決めたこと |
|---|---|
| 定義 | `engine/packaging/addons/<id>/`: `addon.json`（題・説明・`modules`＝読めるか確かめるモジュール・`uses`＝exe 側で使うもの・組み込まれたもののライセンス）、`requirements.in`（直接の依存）、`requirements.txt`（**`requirements-exe.txt` を制約にして**解いたハッシュ付きの固定。`node scripts/build-addon.mjs <id> --lock`）、`licenses/`（wheel に入っていないライセンス文。Open JTalk・hts_engine API・MeCab・SudachiDict の LEGAL を sdist から取った） |
| 作る | `node scripts/build-addon.mjs <id>`（`--all`）: 固定のうち **exe に無いものだけ**を wheel（Python 3.13・win_amd64・`--only-binary`・ハッシュ照合）から `site-packages/` に展開する。exe と共有するもの（numpy・pydantic など）は入れない（二重に持たない・版がずれない）。版が exe と食い違えば止まる（`--lock` で作り直す）。`gliss-addon.json`（manifest）と `LICENSES.txt` を付けて zip にする（名前の順・日時を固定。同じ中身なら同じ SHA-256。実測で 2 回のビルドが一致）。固めた exe があれば、その exe に読ませて import と漢字の読みを 1 回試す |
| 出力 | `engine/packaging/dist-addons/Gliss-addon-<id>-<key>.zip`・`<id>.json`（目録の 1 項目）・`<id>.LICENSES.txt`（THIRD_PARTY_NOTICES の「任意のアドオン」の節に入る） |

### 11-2. 互換キー

- manifest に `python`（`cp313`）・`platform`（`win_amd64`）・`requires`（exe と共有するパッケージの版。`uses` の onnxruntime を含む）・`packages`（アドオン自身の版）を書く。**キー**はこれらの要約（SHA-256 の先頭 12 桁）。
- **エンジンは読み込む前に、Python の版・OS・`requires` の版がこのエンジンと一致するかを確かめる**（`vocal_engine/addons.py`）。合わなければ読まず、理由（例「numpy 2.3.0（エンジンは 2.4.6）」）を `engine_info().addons` に出す。固めた exe には dist-info がほとんど入らないので、spec がビルドの venv から `gliss-engine-packages.json` を exe に入れておく。
- キーが変わるのは、exe 側の**関係する依存**（今は numpy・pydantic・pydantic-core・typing-extensions・annotated-types・typing-inspection・onnxruntime）・Python・アドオン自身の固定が変わったときだけ。アプリを更新しても、これらが同じなら取り直さない（キーが違っても互換なら取り直しを促さない）。

### 11-3. 置き場と読み込み

- `%LOCALAPPDATA%\Gliss\addons\<id>\{gliss-addon.json, LICENSES.txt, site-packages\}`。エンジンは**起動時に互換のあるアドオンの `site-packages` を `sys.path` の末尾に足す**（exe の中が先に見つかる）。画面が起動したエンジンにも、AI クライアントに登録したエンジン exe にも、環境変数なしで効く（`GLISS_ADDONS_DIR` で差し替え可）。
- 取得の直後は画面が `engine_info(reload_addons=True)` で読み直させる（**再起動は要らない**）。AI クライアントのエンジンは次に起動したときから（画面に「つなぎ直すと使えます」）。
- 開発版（`.venv` の python）は `GLISS_ADDONS_DIR` を渡したときだけ読む。manifest の `modules` が venv で import できるならアドオンより venv を使う（`source: environment`。画面は「使えます（開発環境の Python）」）。
- 日本語と空白を含むパス（`…\ユーザー 名\addons`）でも pyopenjtalk の辞書が開けることを確かめた（短い名前への逃がしは要らなかった）。

### 11-4. 配布元とリリース

- **Gliss 自身の GitHub Release の添付**（`Gliss-addon-lyrics-ja-<key>.zip`。`SHA256SUMS.txt` にも入る）。release.yml が exe の直後に作り、`scripts/addon-catalog.mjs` が**目録（ファイル名・大きさ・SHA-256・ライセンスの一覧）をアプリに埋める**（`app/addons-catalog.json`。生成物）。`release-assets.mjs --addons` は、埋めた目録と大きさ・SHA-256 が合う zip だけを載せる。
- アプリは**自分の版の Release**（`https://github.com/tekalu1/gliss/releases/download/v<版>/<file>`）から取得し、大きさ・SHA-256 を照らす（取得そのものは重みと同じ `fetchArchive`。Electron net・転送の追従・取り消し）。目録の SHA-256 は署名済みになりうるアプリの中にあるので、Release の添付だけを差し替えても通らない。
- 確かめるときは `GLISS_ADDON_BASE_URL`（取得先の前半）・`GLISS_ADDON_CATALOG`（目録の差し替え）。`scripts/smoke-addon.mjs` はローカルの HTTP サーバーで通す。
- 目録を作らずに `dist:dir` した版は、行に「この版には含まれていません」と出る。

### 11-5. 画面と削除

- 初回の「モデルの準備」画面の解析モデルの下に、同じ見た目・操作の行（「漢字の歌詞の読み　任意 · 約 99 MB　[ライセンス] [ダウンロード]」、取得中は進み具合と [取り消し]、入れた後は [削除]）。**ヘルプ > モデルと追加の機能…** は同じ行（解析モデルの行を含む）をダイアログへ移して出す（曲を開いている間も使える）。
- エンジンと合わない（アプリの更新で共有する依存が変わった）ものは「更新が必要です · 約 99 MB」と [更新]（理由はツールチップ）。取り直すと前のものと入れ替える（展開は一時フォルダ → manifest の id・キーを目録と照らす → 前のものをどけて rename）。
- 削除: エンジンが拡張モジュール（.pyd）を読み込んでいると Windows では消せない。そのときは `gliss-addon.remove` を置いて「Gliss を次に起動したときに削除します」。次の起動で**エンジンより先に**消す。エンジンはこの印のあるものを読まない（AI クライアントのエンジンも）。まだ読み込んでいなければその場で消える（漢字を読めるかの表示は import せずに `find_spec` で見る。`engine_info` のたびに .pyd を掴まないように）。
- **zip の展開は worker スレッドで行う**（`app/unzip.mjs`）。展開版をテストの見えないウィンドウで動かすと、main のイベントループが 1 回りに約 45 ms かかり（setImmediate 1000 回に 45 秒）、extract-zip（yauzl → zlib のストリーム）が 100 KB/s 程度になって 317 MB の展開が終わらなかった。worker は同じ条件で 1.7 秒。重みの zip の展開も同じ関数にした。

### 11-6. 聞き取り（faster-whisper）をアドオンにできるか

- **仕組みとしては載る**: `faster-whisper==1.2.1`・`ctranslate2==4.8.2`・`av==18.1.0`・`tokenizers`・`huggingface-hub` を同じ手順で固定すると、exe に無いものは 13 パッケージ・zip 53.6 MB（展開後 150 MB）。固めた exe に読ませて import でき、`faster-whisper-tiny` で CPU の推論まで通った（`--check` の `GLISS_CHECK_ASR_MODEL`。2 秒の合成音で 0.54 秒）。標準ライブラリの不足も無かった。
- **ライセンスの問題があるので配らない**（定義はリポジトリに入れていない）:
  1. **CTranslate2 の Windows の wheel は Intel oneMKL を静的に含み**（`ctranslate2.dll` に「Intel(R) oneAPI Math Kernel Library」）、`libiomp5md.dll`（Intel OpenMP）と `cudnn64_9.dll`（NVIDIA cuDNN）も同梱している。どれも独自のライセンスで、GPL のプログラムと一緒に配る（エンジンが同じプロセスで読む）と GPL と両立しない。Gliss 自身の作者は例外を足せるが、同じプロセスに入る praat-parselmouth（Praat。GPL）の側の許可は無い。
  2. **PyAV の Windows の wheel は FFmpeg を libx264・libx265（GPL-2.0+）・LAME・libiconv（LGPL）などとともに同梱している**。配るなら対応するソースの提供が要る。ただし Gliss は numpy の配列を渡すので PyAV は使わない（faster-whisper の `decode_audio` がファイルを読むときだけ）。import だけ通す小さな代わりの `av` を入れれば FFmpeg は外せる。
- 案: (a) CTranslate2 を MKL・CUDA 無し（OpenBLAS か Ruy）で自前でビルドした wheel を作る（CI が要る。速さは要測定）＋ `av` の代わり、(b) 推論を onnxruntime（exe に入っている）で行う Whisper に替える（アドオンは tokenizer と重みだけになる）、(c) 配布版では聞き取りを出さず、開発版（自分で venv に入れる）だけにする（今の状態）。決めるのはユーザー。

### 11-7. 残したもの

- 署名を付けた後も、アドオンの zip は署名しない（中の .pyd は wheel のまま）。完全性は目録の SHA-256 で見る。
- 古い版の Release を消すと、その版のアプリからは（キーが同じでも）取得できなくなる（取得先が自分の版の Release のため）。消さない運用にする。
- 実機（インストールした Gliss・GitHub の実際の Release）での取得は未確認（第 4 段階で）。
- 配布版の「聞き取る」が使えない理由の文（`asr/recognize.py` の `INSTALL_HINT`）は、まだ venv に入れる案内のまま。

## 12. 公開の準備（第 3 段階の掃除）で決めたこと

- 評価用の `stage0/`・`stage2/`・`stage3-render/`、`REPORT-*.md`、評価・調査の文書、実素材の計測スクリプト、素材が写るスクリーンショットは外した（旧リポジトリに残る）。
- 開発版の重みの既定も `%LOCALAPPDATA%\Gliss\models`（初回の画面で取得でき、worktree・配布版と共有できる）。`VOCAL_ENGINE_MODELS_DIR` は残す。
- テスト素材は `GLISS_TEST_MATERIALS` で指す。素材のファイル名・歌詞・素材から作った回帰値は素材のフォルダの `materials.json` 側に置く（`docs/testing.md`）。
- 利用者向けの README、`docs/user-guide.md`・`docs/development.md`、`SECURITY.md`・`NOTICE`、About の法的告知を足した。内部向けのギャップ調査の文書は外した。
