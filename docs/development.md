# 開発者向け

変更作業の手順（ブランチと worktree・検証・公開リポジトリで書かないもの・コミットと PR）は [AGENTS.md](../AGENTS.md)。

## 構成

Electron の画面（`app/`）が、Python のエンジン（`engine/`）を MCP（stdio）の別プロセスとして起動する。
同じエンジンを Claude Code などの AI クライアントからも MCP で使う（ツールの仕様は [MCP.md](../engine/docs/MCP.md)）。

| 場所 | 内容 |
|---|---|
| `engine/` | Python エンジン `vocal_engine`（RMVPE の F0、音符分割、HubertFA の音素、MFCC DTW、非破壊の編集リストと undo、Praat（parselmouth）の TD-PSOLA による再合成（自前 TD-PSOLA・WORLD も選べる）、複数トラックのセッション、プロジェクトのファイル（`.gliss`）、ノートのフェード、テンポ（iXML のテンポマップ）、MCP サーバー）。`engine/packaging/` に単体 exe（PyInstaller）の spec と固定した依存 |
| `app/` | Electron の画面（vanilla JS + SVG。ビルド工程なし）。`main.mjs` がエンジンを MCP のクライアントとして起動する。`electron-builder.yml` が配布版の設定 |
| `assets/logo/` | ロゴ・アイコン（SVG・PNG 16〜1024・`gliss.ico`）。文字ロゴ `gliss-wordmark(-light).svg`、横組み `gliss-lockup.svg`、マーク `gliss-mark(-light).svg`、アプリのアイコン `gliss-icon.svg`（小さいサイズ用 `gliss-icon-small.svg`） |
| `docs/` | 使い方・設計・計画の文書 |
| `scripts/` | 版の同期・エンジン exe のビルド・リリースノート・ライセンス文・リリースの道具（[release-plan.md](release-plan.md) §3） |
| `releases/` | リリースノートの正本（`<版>.json`） |
| `.github/workflows/` | CI（`test.yml`）とリリース（`release.yml`。タグ `v<版>` で Windows のインストーラを作って公開） |

## 環境

- Windows 11（1.0 は Windows のみ）
- Python 3.13（[uv](https://docs.astral.sh/uv/) で作る `.venv`）
- Node 22 以降 + pnpm 10

```powershell
# エンジン
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -e "engine[dev,g2p]"
#   任意: asr（聞き取り。faster-whisper）・asr-cuda（GPU で聞き取る）・world（WORLD の再合成）
# 画面
cd app
pnpm install
pnpm start
```

`engine[g2p]`（pyopenjtalk-plus）は漢字混じりの歌詞を読むため。無くてもかなの歌詞なら動く。配布版では同じものを任意のアドオンとして画面から取得する（[release-plan.md](release-plan.md) §11）。

### エンジンの起動のしかた（開発版）

画面（`app/paths.mjs`）は、リポジトリ直下に `.mcp.json`（git 管理外）があればその `mcpServers.gliss` で、
無ければ `<repo>\.venv\Scripts\python.exe -m vocal_engine.mcp`（cwd は `engine`）でエンジンを起動する。
別の場所の python を使うときは `engine/.mcp.json.example` を直下に `.mcp.json` として写し、`<repo>` を書き換える。
`VOCAL_ENGINE_CWD` でエンジンの作業ディレクトリだけを差し替えることもできる。

配布版は同梱のエンジン exe（`resources\engine\vocal-engine\vocal-engine.exe`）を起動する。

### 解析モデルの重み

開発版も配布版と同じく `%LOCALAPPDATA%\Gliss\models` を既定にする。画面の初回の「モデルの準備」から取得できる
（公開元の zip を取得し、大きさと SHA-256 を確かめてから置く。`app/model-download.mjs`）。
別の場所に置くときは `VOCAL_ENGINE_MODELS_DIR`（旧名 `VOCAL_ENGINE_MODELS` も読む）で指す。
聞き取り（Whisper）の重みは `%LOCALAPPDATA%\Gliss\models\asr`（`GLISS_ASR_MODELS_DIR`）。

### AI クライアントから開発版のエンジンを使う

画面の **ヘルプ > AI とつなぐ…** は開発版でも使える（開発版の python と `engine` を登録する）。手で書くときは
`engine/.mcp.json.example` の形で、`<repo>` をこのリポジトリの場所に書き換える。

- `command` は **venv の python の絶対パス**（`python` ではダメ。依存が入っていない）
- サーバー ID は `gliss`（アンダースコアを入れない）

## テスト

[testing.md](testing.md)。テストの素材（実際の歌声）はリポジトリに入れていないので、手元の素材を `GLISS_TEST_MATERIALS` で指す。

## 配布版・リリース

[release-plan.md](release-plan.md)。要点:

- 版の正本は `app/package.json`。`node scripts/sync-version.mjs` で engine 側に写す
- エンジン exe は `pnpm build:engine`（`scripts/build-engine.mjs`。torch 無し・依存を固定してビルド）
- 展開版は `pnpm dist:dir`、インストーラは `pnpm dist`
- リリースは `releases/<版>.json` を書いてタグ `v<版>` を push する（`.github/workflows/release.yml`）

## 内部の名前

**内部の名前は旧称（vocal-editor）のまま**: Python パッケージ `vocal_engine`、環境変数 `VOCAL_ENGINE_*`・`VOCAL_EDITOR_MUTE`、
ログの `~/.vocal-editor/`、プロジェクトの形式名（`vocal-editor-session` など）、書き出しの `_ve.wav` は変えていない
（利用者の目にほとんど触れず、変えると import・設定・既存のプロジェクト・DAW 側の扱いへの影響が大きいため）。
