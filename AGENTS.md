# 開発規則

Gliss を変える作業の標準の手順。エージェントは作業を始める前にこれを読む。ユーザーの個別の指示があれば、そちらを優先する。
詳しい説明は `docs/` にある。ここには「何をどの順でするか」と「どこを見ればよいか」だけを書く。

| 知りたいこと | 見る場所 |
|---|---|
| 何をするアプリか | `README.md` |
| 構成・環境・内部の名前 | `docs/development.md` |
| テストと素材 | `docs/testing.md` |
| 配布・自動更新・アドオン・リリース | `docs/release-plan.md` |
| エンジンの MCP のツール | `engine/docs/MCP.md` |
| 次にやること | `gh issue list --repo tekalu1/gliss` |

## 作業開始

- 直す前に `git status --short --branch` と `git worktree list` を見る。他人の未コミットの変更・ブランチ・worktree は触らない（stash・破棄・上書きしない）。
- 作業ごとにブランチと worktree を作る。main の作業ディレクトリで直接直さない。
  - 置き場はリポジトリの隣の `../<リポジトリのフォルダ名>-wt-<作業名>`（例 `git worktree add -b fix/<作業名> ../gliss-wt-<作業名> main`）。
    リポジトリの中に置くと、main の作業ディレクトリを丸ごと見る道具（`gitleaks dir` など）に同じツリーが二重に入る。パスが短いほど `node_modules` の深いパスが Windows のパス長の制限にかかりにくい。
  - worktree へのコマンドは `git -C <wt>` と絶対パスで打つ。シェルを worktree に `cd` したままにしない（掴まれて worktree を消せなくなる）。
- worktree の準備:
  - 画面の依存: `<wt>\app` で `pnpm install`。依存が main と同じなら、PowerShell から `cmd /c mklink /J <wt>\app\node_modules <main>\app\node_modules` でもよい。
  - エンジン: worktree に `.venv` は作らない。main の `.venv` の python で、worktree の `engine` を読ませる。
    - 画面: worktree の直下に `.mcp.json`（git 管理外）を置く。`engine/.mcp.json.example` を写し、`command` を `<main>\.venv\Scripts\python.exe`、`cwd` を `<wt>\engine` にする。
      `.mcp.json` が無いと、画面は `<wt>\.venv` を探して起動できない。`VOCAL_ENGINE_CWD` でも cwd だけ差し替えられる（`app/paths.mjs`）。
    - pytest: `<wt>\engine` を cwd にして `<main>\.venv\Scripts\python.exe -m pytest`。`.venv` には main の `engine` が editable で入っている。
      `python -m` は cwd を `sys.path` の先頭に入れるので、cwd が `<wt>\engine` のときだけ worktree の `vocal_engine` が読まれる。
      迷ったら `python -c "import vocal_engine; print(vocal_engine.__file__)"` で確かめる。
  - 解析モデルの重みは `%LOCALAPPDATA%\Gliss\models` を共有する（読むだけ）。

## 構成

`app/`（Electron の画面。ビルド工程なし）・`engine/`（Python のエンジン `vocal_engine`。MCP サーバー）・`scripts/`（版・ビルド・スモーク・リリースの道具）・
`releases/`（リリースノートの正本）・`docs/`・`assets/logo/`・`.github/workflows/`（`test.yml` と `release.yml`）。中身は `docs/development.md` の「構成」。
内部の名前は旧称のまま（`vocal_engine`・`VOCAL_ENGINE_*`・`VOCAL_EDITOR_*`・`_ve.wav`。理由は `docs/development.md` の「内部の名前」）。環境変数は旧称と `GLISS_*` が混ざっている。

## 環境変数

| 名前 | 意味 |
|---|---|
| `GLISS_TEST_MATERIALS` | テストの素材のフォルダ（`materials.json` と WAV。`docs/testing.md`）。無ければ素材の要るテストは skip |
| `VOCAL_ENGINE_MODELS_DIR` | 解析モデルの重みの置き場。既定 `%LOCALAPPDATA%\Gliss\models`（旧名 `VOCAL_ENGINE_MODELS` も読む） |
| `GLISS_ASR_MODELS_DIR` | 聞き取り（Whisper）の重みの置き場。既定 `%LOCALAPPDATA%\Gliss\models\asr` |
| `GLISS_ADDONS_DIR` | アドオンの置き場。配布版の既定 `%LOCALAPPDATA%\Gliss\addons`。開発版は指定したときだけ扱う |
| `GLISS_ADDON_BASE_URL`・`GLISS_ADDON_CATALOG` | アドオンの取得先・目録の差し替え（試験用。`app/addons.mjs`） |
| `VOCAL_ENGINE_CWD` | 開発版の画面が起動するエンジンの作業ディレクトリ（worktree 用） |
| `VOCAL_ENGINE_WORK_DIR` | 作業場所。既定 `%LOCALAPPDATA%\Gliss\work`。エンジンのテストで new/save するときは一時フォルダに向ける |
| `VOCAL_ENGINE_LOG_DIR` | 曲を開く前の `engine.log` の置き場。既定 `~/.vocal-editor`（曲を開いた後は作業場所の `engine.log`） |
| `GLISS_TEST_PYTHON` | 画面のテスト（`first-run`・`addons-view`）がフィクスチャを作る python。既定はエンジンと同じ |
| `VOCAL_EDITOR_MUTE`・`VOCAL_EDITOR_HIDDEN`・`VOCAL_EDITOR_IGNORE_MOUSE` | テスト用。音を出さない・透明で前面を奪わない・人のマウスを素通しする。`app/playwright.config.js` が入れる（見るときは `pnpm test:headed`） |
| `GLISS_ASR_FAKE` | 偽の認識器（聞き取りのテスト用） |
| `GLISS_TEST_PREP` | `1` でエンジンのテストを裏の準備ありで流す（既定は止める。`engine/tests/conftest.py`） |

## 実装と検証

変えたものに合わせて回す。テストは worktree の中で回す（main で回しても worktree の変更は試せない）。

| 変えたもの | 回すもの |
|---|---|
| `engine/` | `<wt>\engine` で pytest（上の「作業開始」）。絞るなら `-k`。MCP のツールを変えたら `engine/docs/MCP.md` も直す |
| `app/` の画面 | `pnpm test:unit` と、関係する `pnpm exec playwright test tests/<名前>.spec.js` |
| 配布・更新・アドオン（`electron-builder.yml`・`build/installer.nsh`・`updates*.mjs`・`addons.mjs`・`scripts/build-*.mjs`） | `pnpm test:unit`（`release.spec.js` が NSIS のテンプレートの形を見る）。`pnpm build:engine` → `pnpm dist:dir` → `node scripts/smoke-packaged.mjs`。更新は `node scripts/smoke-update.mjs --old <古い展開版> --new <新しい出力>`、アドオンは `node scripts/build-addon.mjs --all` → `pnpm dist:dir` → `node scripts/smoke-addon.mjs --models <重み>` |
| 版・リリースノート・`scripts/release-*.mjs` | `node scripts/sync-version.mjs --check`・`node scripts/release-check.mjs`・`pnpm test:unit` |
| `.github/workflows/` | actionlint |
| 文書だけ | 書いたコマンド・パス・名前が実在するか、`git diff --check` |

- 素材も重みも無くても回るもの: エンジンの pytest の一部（残りは skip）、`pnpm test:unit`、素材を開かない Playwright（初回の画面・タイトルバー・更新の表示など）、`smoke-packaged.mjs`（重み無しで起動して、同梱のエンジンにつながるまで）。
  素材と重みが要るもの: 解析・再合成・音素の結果を見るテスト。CI は素材なしの範囲だけを回す。
- 所要時間の目安: pytest 全部 約 3〜5 分（素材なしなら 1 分以内）、`pnpm test:unit` 数秒、Playwright 全部 約 5〜11 分。
- Playwright を複数の作業で同時に流すと時間切れが出やすい。落ちたらそのファイルだけを単独で流し直して切り分ける。
- 検証が失敗したら原因を調べる。未解決のまま完了にしない。

## 公開リポジトリの衛生

このリポジトリは公開されている。コード・文書・コミットメッセージ・PR・issue に次を書かない。

- 個人の絶対パス（`D:\…`・`C:\Users\<名前>`）。`<repo>`・相対パス・`%LOCALAPPDATA%` などで書く
- テストの素材（第三者の歌声）のファイル名・歌詞。素材と、素材に結び付いた期待値は `GLISS_TEST_MATERIALS` のフォルダの `materials.json` に置き、テストは記号とキーで引く（`docs/testing.md`）
- 社名・社内ツールの名前、個人のメールアドレス（コミットは GitHub の noreply アドレス）、トークン
- スクリーンショット・文書の図は合成の音で撮る。`*.wav`・`*.png` は `.gitignore` で除いている（例外は `docs/images/`・`assets/logo/`）。`git add -f` で足さない
- 調査のメモ・ログ・一回きりのスクリプトは、main の作業ディレクトリの `scratchpad/`（git 管理外）に置く。worktree の中に置くと worktree と一緒に消える

PR の前に gitleaks（公式 Release のバイナリ）で `gitleaks git <wt>` をかける。CI の `test.yml` の `secrets` ジョブも全履歴を見る。

## コミットと PR

- コミットメッセージは日本語。1 行目に何をしたかを書く（例「版を 0.1.0-beta.2 にする」）。意味ごとに分け、対象のファイルを明示してステージする。
- PR を作り、CI（`test.yml`）が通ってから main にマージする。
- **push・PR・マージ・タグ・Release・issue の作成は、ユーザーの指示があるときだけ**。エージェントが勝手に公開しない。

## 版とリリース

- 版の正本は `app/package.json` の `version`。`node scripts/sync-version.mjs` で engine 側に写す。
- リリースノートは `releases/<版>.json`。タグ `v<版>` を push すると `release.yml` が作って公開する。手順は `docs/release-plan.md` §3。
- 一度出した版のファイルは上書きしない（直すときは版を上げる）。古い版の Release は消さない（その版のアプリがアドオンを取れなくなる）。
- まだ署名していない。識別子（appId `io.github.tekalu1.gliss`・`Gliss.exe`・インストール先）は変えない（変えると自動更新が別のアプリ扱いになる）。署名を付けた後の発行元名も変えない。

## 落とし穴

- Electron を起動するスクリプト・テストは、未保存の確認ダイアログで終わらずに止まることがある。全体に timeout を付け、最後にプロセスを木ごと終了させ（`taskkill /T /F`。`scripts/smoke-*.mjs` の `killTree`）、残りが 0 であることを確かめる。
- Pleiad などから起動されたシェルには `ELECTRON_RUN_AS_NODE=1` が引き継がれていることがある。テストとスモークは消してから起動する。手で `pnpm start` するときは `env -u ELECTRON_RUN_AS_NODE` を付ける。
- 試験で `%APPDATA%\Gliss`・`%LOCALAPPDATA%\Gliss` を汚さない（開発版と配布版が同じ場所を使う）。スモークは `LOCALAPPDATA` と userData を一時フォルダに差し替えている。
- 配布版は前回開いていた曲を起動時に開き直す（利用者の実データを開いてしまう）。配布版を試すときはユーザーデータを差し替える。
- 配布版の自動更新は `--dir` の展開版では動かない（`app-update.yml` は NSIS の出力にだけ入る）。
- Windows PowerShell 5.1 の `.Count` は、該当が 1 件のとき `$null` になる。NSIS の既定の `CHECK_APP_RUNNING` がこれでエンジンを見逃していた（`app/build/installer.nsh`）。
- GitHub の Release の配信は、複数範囲の Range に 501 を返す。差分更新は electron-updater の GitHub provider が 1 範囲ずつ取るので効いている。provider を変えると全体の取得に落ちる。
- 展開版の main プロセスはイベントループが遅くなることがある。大きな zip の展開は worker スレッドで行う（`app/unzip.mjs`）。
- Git Bash から NSIS のインストーラ・アンインストーラに `/S` を渡すと MSYS が壊す。PowerShell から渡す。

## 後片付け

- マージされたら worktree とブランチを消す（`git worktree remove <wt>`・`git branch -d <ブランチ>`。強制しない）。
- **消す前に worktree の中のリンクを外す。** 外さないと、リンク先の本体（main の `node_modules` など）まで消える。
  ジャンクションの `node_modules` は `cmd /c rmdir <wt>\app\node_modules`。ほかは `Get-ChildItem <wt> -Recurse -Attributes ReparsePoint` で探し、`(Get-Item <path>).Delete()` で 1 つずつ外す。
- 起動したサーバー・Electron・エンジンが残っていないことを確かめる（`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '<wt の名前>' }`）。
