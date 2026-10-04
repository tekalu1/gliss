// Electron main（Gliss の画面。issue #29 で旧称 vocal-editor から改名）。
//
//  - Python エンジンを **MCP クライアント（stdio）として起動**する。配布版は同梱の単体 exe、開発版は
//    リポジトリ直下の `.mcp.json`（サーバー ID `gliss`）か `.venv`（起動方法の決め方は paths.mjs の engineLaunch）。
//  - renderer は preload 経由の IPC でツールを呼ぶ（`engine.call(name, args)`）。
//    **画面も AI も同じツールを通す**（renderer からエンジンの内部関数は呼ばない）。
//  - `project.json` を `fs.watch` し、外部（Claude Code）が書き換えたら renderer に
//    「再読込」を通知する。自分の編集で変わったぶんは中身のハッシュで弾く。
//
// メニュー（ファイル／編集／ノート／表示／ヘルプ）の中身は **main が作る Electron の Menu が唯一の正**。
// **項目の名前・キーは renderer のコマンドの表から作る**（issue #17・#22。`renderer/commands.js`。キーは設定で
// 変えられる）: renderer が `app.setAppMenu` で並びを送り、main が Electron の Menu に直す（最近使ったもの・
// role の項目は main が足す）。押された項目は `menu` の IPC で renderer に返す。キーの処理も renderer がする
// （accelerator は表記だけ。`registerAccelerator: false`）。
// ウィンドウは**タイトルバー一体型**（`titleBarStyle: 'hidden'` + `titleBarOverlay`）。最小化・最大化・閉じるは
// OS が描き（スナップレイアウトが効く）、メニューバーは画面の上端のバー（`renderer/titlebar.js`）が描く:
// main は作った Menu を**そのまま写した形**（`menuModel`）を `app-menu` で送り、押された項目は
// `app.menuClick`（項目の位置）で受けて Menu の同じ項目の click を呼ぶ。Menu のテンプレートに足した項目は
// そのまま画面に出る。
//
// ヘルプ > AI とつなぐ…: Claude Code / Claude Desktop に gliss を登録する・AI に許可（編集・保存・書き出し）。
// 許可と「画面で開いている曲」は userData の bridge.json に書き、AI 側のエンジンが読む（ai-connect.mjs）。
//
// 自動更新（ヘルプ > 更新を確認…。docs/release-plan.md §4）: 配布版で resources/app-update.yml があるときだけ動く。
// 状態機械は updates.mjs、画面は renderer/updates.js。起動の 15 秒後・4 時間おき・手動で確かめ、取得が済んだら
// 「再起動して更新」を出す（黙って再起動しない・終了時の自動適用もしない）。押されたら、AI 側のエンジンも
// 止まることを知らせ → 未保存の「保存しますか」→ 画面のエンジンを止める → インストール先の vocal-engine.exe を
// すべて止める → quitAndInstall（installUpdate）。
//
// 起動引数: `--project <file.gliss>` でプロジェクトを開く（issue #33）。
//           `--take <wav>` `--guide <wav>` を渡すとテイクとして開く（テスト用。`--project-dir` と一緒なら旧形式のまま）。
//           オプションでない最初の引数が既存の音声ファイルなら `--take` と同じ（`Gliss.exe <WAV>`。DAW の外部エディタ）。
//           `--lyrics "あいうえお…"` を渡すと歌詞つきで開く（音素アラインメントが走る）。
//           `--project-dir <dir>` でプロジェクトの置き場を指定できる（テストの分離用）。
//           `--user-data-dir <dir>` で設定（state.json）の置き場を指定できる（同上）。
//           `--legacy-user-data-dir <dir>` で旧名の設定の置き場を指定する（設定の引き継ぎのテスト用。
//           `--user-data-dir` を渡したときは、これも渡さないと引き継がない）。
//           `--mute`（か環境変数 `VOCAL_EDITOR_MUTE=1`）で**音を一切出さない**（テスト用。issue #27）。
//           Chromium の `--mute-audio`・webContents の setAudioMuted・renderer の出力の音量 0 の三重にする
//           （打合せ・画面共有の最中にテストが走っても鳴らさない）。Playwright は playwright.config.js で必ず付ける。
//           `VOCAL_EDITOR_HIDDEN=1` で透明（opacity 0）のまま表示し、見えず前面も奪わずに撮影できるようにする（テスト用）。
import { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, shell, net } from 'electron';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { parseArgs } from './args.mjs';
import { downloadModels, modelDirectory, modelPresence, sourcesFromEnvironment, totalModelBytes } from './model-download.mjs';
import { addonDirectory, addonStates, cleanupAddons, downloadAddon, loadCatalog, removeAddon } from './addons.mjs';

import electronUpdater from 'electron-updater';

import * as AI from './ai-connect.mjs';
import { findEngineProcesses, stopEngineProcesses } from './engine-processes.mjs';
import { assetsDir, engineLaunch, packagedEngineExe } from './paths.mjs';
import { newestReleaseProvider, readUpdateConfig } from './update-provider.mjs';
import { Updates } from './updates.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 開発版: リポジトリのルート。配布版では resources/（app.asar の親）を指すので、リポジトリ相対のものは使わない
const REPO = path.dirname(HERE);
// アプリ名。userData のフォルダ名（%APPDATA%\Gliss）と app.getName() は、Electron が起動時に
// package.json の productName から決める。ここ（タイトル・About）と package.json を揃えておく
const APP_NAME = 'Gliss';
// ウィンドウ・タスクバー・About のアイコン（ロゴ一式は <repo>/assets/logo/。配布版は resources/assets/logo/。issue #29）
const ASSETS = assetsDir({ packaged: app.isPackaged, resourcesDir: process.resourcesPath, repo: REPO });
const ICON_ICO = path.join(ASSETS, 'logo', 'gliss.ico');
const ICON_PNG = path.join(ASSETS, 'logo', 'gliss-icon-256.png');
// 旧名（vocal-editor）のときの userData のフォルダ名（package.json の name が vocal-editor-app だった）
const LEGACY_USER_DATA = 'vocal-editor-app';
// タイトルバー（renderer の .titlebar と同じ色・高さ）。最小化・最大化・閉じるは OS がこの色で描く
const TITLE_BAR = { color: '#111113', symbolColor: '#d6d6d6', height: 32 };

const state = {
  win: null,
  client: null,
  transport: null,
  engineError: null,
  projectDir: null,
  watcher: null,
  lastHash: null,
  watchTimer: null,
  sessionDir: null,     // セッション（session.json）の置き場。トラックのプロジェクト・波形・音はこの下
  sessionWatcher: null,
  lastSessionHash: null,
  sessionTimer: null,
  playable: new Set(),  // render_tracks が返した音のファイル（元の WAV はプロジェクトの外にあるので個別に許す）
  jobs: new Set(),      // 自分が走らせたジョブ。終わるまで「外部の変更」を出さない
  inflight: 0,          // 呼んでいる最中のツールの数（同上）
  externalPending: false, // 呼ぶ前に見つけた、まだ知らせていない外部の変更（呼び終わったら知らせる）
};

// ---------------------------------------------------------------- 設定の記憶
function settingsPath() {
  return path.join(app.getPath('userData'), 'state.json');
}

function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsPath(), 'utf8'));
  } catch {
    return {};
  }
}

function saveSettings(patch) {
  const next = { ...loadSettings(), ...patch };
  try {
    fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
    fs.writeFileSync(settingsPath(), JSON.stringify(next, null, 2), 'utf8');
  } catch (e) {
    console.error('設定を保存できなかった:', e.message);
  }
  return next;
}

const MAX_RECENT = 8;

/** 最近使ったプロジェクト。`.gliss`（`path`。issue #33）と、旧形式（テイクのパス `take`）のどちらか。同じものは 1 つ。 */
function recentKey(r) {
  return String(r?.path || r?.take || '').toLowerCase();
}
function pushRecent(entry) {
  if (!entry?.take && !entry?.path) return;
  const key = recentKey(entry);
  const list = (loadSettings().recent || []).filter((r) => recentKey(r) !== key);
  list.unshift(entry.path ? { path: entry.path, at: Date.now() }
    : { take: entry.take, guide: entry.guide || null, at: Date.now() });
  saveSettings({ recent: list.slice(0, MAX_RECENT) });
  buildMenu();
}

// ---------------------------------------------------------------- 起動引数（args.mjs）
const ARGS = parseArgs(process.argv.slice(1));
const MODELS_DIR = modelDirectory({ packaged: app.isPackaged, env: process.env,
  localAppData: process.env.LOCALAPPDATA, repo: REPO });
// ダウンロード先と Python エンジンが重みを探す場所を揃える。
process.env.VOCAL_ENGINE_MODELS_DIR = MODELS_DIR;
// 任意機能のアドオン（addons.mjs）。配布版は %LOCALAPPDATA%\Gliss\addons（エンジン exe の既定と同じ）。
// 開発版は GLISS_ADDONS_DIR を渡したときだけ扱う（無ければ venv に入れた依存を使う）
const ADDONS_DIR = addonDirectory({ packaged: app.isPackaged, env: process.env, localAppData: process.env.LOCALAPPDATA });
if (ADDONS_DIR) process.env.GLISS_ADDONS_DIR = ADDONS_DIR;
const ADDON_CATALOG = loadCatalog({ file: path.join(HERE, 'addons-catalog.json'), env: process.env, version: app.getVersion() });
// タスクバーのまとまり（Windows）。electron.exe の既定のまとまりに入らず、ウィンドウのアイコンで出る。
// electron-builder の appId（app/electron-builder.yml）と同じにする（インストーラが作るショートカットと一致させる）。
// 一度公開したら変えない（変えるとピン留めと通知の紐づけが切れる）
if (process.platform === 'win32') app.setAppUserModelId('io.github.tekalu1.gliss');
// 設定（state.json: 最近使ったファイル・前回の表示範囲・ウィンドウの位置）の置き場を分ける。
// テストはこれで毎回まっさらにする（共有すると、人が使ったときの表示範囲でノートが画面外に
// 出てドラッグが外れる・テストが人の「最近使ったファイル」やウィンドウ位置を書き換える）。
if (ARGS.userDataDir) app.setPath('userData', path.resolve(ARGS.userDataDir));

/** 旧名（vocal-editor）の設定を引き継ぐ（issue #29）。アプリ名を変えると userData の場所が
 * `%APPDATA%\vocal-editor-app` → `%APPDATA%\Gliss` に変わり、最近使ったファイル・キーの設定・
 * 前回の表示範囲・ウィンドウの位置が消えて見える。新しい場所にまだ state.json が無く、旧い場所に
 * あれば**写す**（元は消さない。旧版に戻しても元の設定のまま）。一度写したら（state.json ができたら）
 * もう見ない。state.json の外（Chromium のキャッシュなど）は使っていないので写さない。
 * `--user-data-dir` で置き場を分けたとき（テスト）は `--legacy-user-data-dir` を渡したときだけ写す
 * （人の設定をテストに持ち込まない）。返り値は写した元のフォルダ（写さなかったら null）。 */
function migrateLegacySettings() {
  const legacyDir = ARGS.legacyUserDataDir ? path.resolve(ARGS.legacyUserDataDir)
    : ARGS.userDataDir ? null : path.join(app.getPath('appData'), LEGACY_USER_DATA);
  if (!legacyDir) return null;
  const dst = settingsPath();
  const src = path.join(legacyDir, 'state.json');
  if (path.resolve(src) === path.resolve(dst) || fs.existsSync(dst) || !fs.existsSync(src)) return null;
  try {
    const old = JSON.parse(fs.readFileSync(src, 'utf8'));   // 壊れていたら写さない
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.writeFileSync(dst, JSON.stringify({ ...old, migratedFrom: legacyDir }, null, 2), 'utf8');
    console.log(`旧名の設定を引き継いだ: ${src} → ${dst}`);
    return legacyDir;
  } catch (e) {
    console.error('旧名の設定を引き継げなかった:', e.message);
    return null;
  }
}
// 音を出さない（テスト）。app の ready より前に Chromium のスイッチを入れる。renderer（preload）は環境変数を見る
const MUTED = process.argv.includes('--mute') || process.env.VOCAL_EDITOR_MUTE === '1';
const HIDDEN = process.env.VOCAL_EDITOR_HIDDEN === '1';
if (MUTED) {
  process.env.VOCAL_EDITOR_MUTE = '1';
  app.commandLine.appendSwitch('mute-audio');
}

// ---------------------------------------------------------------- エンジン接続
function engineConfig() {
  return engineLaunch({ packaged: app.isPackaged, resourcesDir: process.resourcesPath, repo: REPO,
    bridge: bridgePath(), env: process.env });
}

// ---------------------------------------------------------------- AI とつなぐ（ヘルプ > AI とつなぐ…）
/** bridge.json（画面と AI 側のエンジンの橋渡し）。既定は userData の中（%APPDATA%\Gliss）。GLISS_BRIDGE で差し替える。 */
function bridgePath() {
  return process.env.GLISS_BRIDGE ? path.resolve(process.env.GLISS_BRIDGE) : path.join(app.getPath('userData'), 'bridge.json');
}

function writeBridge(patch) {
  try {
    return AI.writeBridge(bridgePath(), patch);
  } catch (e) {
    console.error('bridge.json を書けなかった:', e.message);
    return null;
  }
}

/** AI のクライアントに登録する設定（配布版・開発版の分岐は ai-connect.mjs の serverConfig）。 */
function aiServerConfig() {
  const packaged = app.isPackaged;
  const cfg = packaged ? null : engineConfig();
  return AI.serverConfig({
    packaged,
    engine: cfg ? { command: cfg.command, args: cfg.args } : null,
    engineDir: cfg?.engineDir,
    resourcesDir: process.resourcesPath,
    bridge: bridgePath(),
    env: process.env,
  });
}

/** ツールの返り値から、画面で開いている曲（と編集中のトラック）を bridge.json に写す。 */
function noteOpenProject(out) {
  const pj = AI.projectFromResult(out, state.bridgeProject);
  if (pj === undefined) return;
  state.bridgeProject = pj;
  writeBridge({ project: pj });
}

function defaultLogPath() {
  return path.join(process.env.VOCAL_ENGINE_LOG_DIR || path.join(os.homedir(), '.vocal-editor'),
    'engine.log');
}

async function connectEngine() {
  const cfg = engineConfig();
  if (!fs.existsSync(cfg.command)) {
    throw new Error(`${cfg.source === 'packaged' ? 'エンジン（vocal-engine.exe）' : 'エンジンの python'}が見つからない:\n${cfg.command}`);
  }
  // ピッチ検出の方式（編集 > ピッチ検出の方式。ユーザー設定）はエンジンの既定として起動時に渡す
  // （画面が set_f0_estimator を呼ぶ前に、前回のトラックの解析・裏の準備が始まるため）
  const f0 = loadSettings().f0Estimator;
  const env = f0 ? { ...cfg.env, GLISS_F0_ESTIMATOR: f0 } : cfg.env;
  state.transport = new StdioClientTransport({
    command: cfg.command, args: cfg.args, cwd: cfg.cwd, env, stderr: 'pipe',
  });
  state.client = new Client({ name: 'gliss-app', version: app.getVersion() }, { capabilities: {} });
  await state.client.connect(state.transport);
  const tools = await state.client.listTools();
  console.log(`エンジンに接続した（ツール ${tools.tools.length} 個）`);
  return tools.tools.map((t) => t.name);
}

function engineFailureMessage(err) {
  let cfg = null;
  try { cfg = engineConfig(); } catch { /* .mcp.json 自体が読めない */ }
  return [
    'Python エンジンに接続できなかった。',
    '',
    `理由: ${err.message}`,
    '',
    cfg?.configPath ? `設定: ${cfg.configPath}` : '',
    cfg ? `command: ${cfg.command}` : '',
    cfg ? `args: ${cfg.args.join(' ')}` : '',
    cfg ? `cwd: ${cfg.cwd}` : '',
    '',
    `ログ: ${defaultLogPath()}`,
    '（プロジェクトを開いたあとは <project>/engine.log）',
  ].filter(Boolean).join('\n');
}

/** CallToolResult → 素の dict。サーバーは JSON テキストで返す。 */
function payload(res) {
  const sc = res?.structuredContent;
  if (sc && typeof sc === 'object') return ('result' in sc) ? sc.result : sc;
  for (const c of res?.content || []) {
    if (typeof c.text === 'string') {
      try { return JSON.parse(c.text); } catch { return { ok: true, text: c.text }; }
    }
  }
  return {};
}

async function callTool(name, args) {
  if (!state.client) return { ok: false, error: state.engineError || 'エンジン未接続' };
  // 呼ぶ前の project.json が覚えているものと違う = watcher の 120 ms の間に外部が書いた（まだ知らせていない）。
  // 呼んだ後にハッシュを覚え直すとそれを吸収してしまうので、覚えておいて呼び終わったら知らせる。
  // 他のツールを呼んでいる最中・ジョブの間は、その書き込みと区別できないので見ない（watcher と同じ）。
  if (state.inflight === 0 && !state.jobs.size && name !== 'open_project' && state.lastHash) {
    const pre = hashProject();
    if (pre && pre !== state.lastHash) state.externalPending = true;
  }
  // session.json も同じ（watcher の間に外部が書いたぶんを、呼んだ後に覚え直して吸収しない）
  if (state.inflight === 0 && !state.jobs.size && name !== 'open_project' && state.lastSessionHash) {
    const pre = hashSession();
    if (pre && pre !== state.lastSessionHash) state.sessionPending = true;
  }
  state.inflight += 1;
  let res;
  try {
    res = await state.client.callTool({ name, arguments: args || {} },
      undefined, { timeout: 600000 });
  } finally {
    state.inflight -= 1;
  }
  const out = payload(res);
  // 画面で開いている曲・編集中のトラックを bridge.json に（AI 側の load_project() が開く）
  noteOpenProject(out);
  // 編集対象のプロジェクト（トラックを切り替えると変わる）と、セッションの置き場を見張る
  if (out?.ok !== false && out?.project_dir) watchProject(out.project_dir);
  const sdir = out?.session?.dir || (name === 'list_tracks' ? out?.dir : null);
  if (sdir) watchSession(sdir);

  // ジョブ（analyze_take など）は**終わってから** project.json を書くので、
  // ツールが返った時点のハッシュでは足りない。走っている間は「外部の変更」を出さず、
  // get_job が終わりを返したところで覚え直す（覚えないと、自分で走らせた解析が
  // 「外部の変更」として二重に読み直される ＝ 158 秒の素材で描き直しが 2 回走る）。
  if (out?.status === 'running' && out.job_id) state.jobs.add(out.job_id);
  if (name === 'get_job' && out?.status !== 'running') {
    state.jobs.delete(args?.job_id);
    rememberProjectHash();
  }
  // 再生する音のファイル（render_tracks。ジョブなら get_job の結果）は読んでよい（音声の拡張子だけ）
  if (Array.isArray(out?.tracks)) {
    for (const t of out.tracks) {
      if (t && typeof t.path === 'string' && t.id && /\.(wav|wave|bwf|flac|aiff?)$/i.test(t.path)) {
        state.playable.add(path.resolve(t.path));
      }
    }
  }
  // **自分が呼んだツールが返ったら、どのツールでも**いまの project.json を「自分のもの」として
  // 覚える。以前は書き込むツールを列挙していたが、後から足した apply_plan・split_note・
  // merge_notes・set_transition・set_connection・render_preview が漏れていて、自分の編集が
  // 120 ms 後に「外部の変更」として読み直されていた（取り消しのまとまりが消え、ドラッグ中なら
  // ピッチの差分が消えて離しても何も当たらなかった）。列挙はツールが増えるたびに漏れるので持たない。
  // 読むだけのツールの直前に外部が書いたぶんは、上の externalPending で拾って知らせる。
  rememberProjectHash();
  rememberSessionHash();
  if (state.externalPending && state.inflight === 0 && !state.jobs.size) {
    state.externalPending = false;
    state.win?.webContents.send('project-changed', { project_dir: state.projectDir });
  }
  if (state.sessionPending && state.inflight === 0 && !state.jobs.size) {
    state.sessionPending = false;
    state.win?.webContents.send('session-changed', { dir: state.sessionDir });
  }
  return out;
}

// ---------------------------------------------------------------- project.json の監視
function projectJson() {
  return state.projectDir ? path.join(state.projectDir, 'project.json') : null;
}

function hashProject() {
  const p = projectJson();
  if (!p) return null;
  try {
    return crypto.createHash('sha1').update(fs.readFileSync(p)).digest('hex');
  } catch {
    return null;
  }
}

function rememberProjectHash() {
  state.lastHash = hashProject();
}

function watchProject(dir) {
  if (state.projectDir === dir && state.watcher) return;
  if (state.watcher) { try { state.watcher.close(); } catch { /* noop */ } }
  state.projectDir = dir;
  rememberProjectHash();
  try {
    state.watcher = fs.watch(dir, { persistent: false }, (_ev, name) => {
      if (name && !String(name).startsWith('project.json')) return;
      clearTimeout(state.watchTimer);
      state.watchTimer = setTimeout(() => {
        const h = hashProject();
        if (!h || h === state.lastHash) return;   // 自分の編集で変わったぶんは無視
        state.lastHash = h;
        // 自分が呼んでいる最中／自分が走らせたジョブの書き込みは「外部の変更」ではない。
        // 1 つのツールが project.json を 2 回書く（set_lyrics）と、その途中の
        // ハッシュを拾って余計な描き直しが走っていた。
        if (state.jobs.size || state.inflight > 0) return;
        state.win?.webContents.send('project-changed', { project_dir: dir });
      }, 120);
    });
  } catch (e) {
    console.error('project.json を監視できなかった:', e.message);
  }
}

// ---------------------------------------------------------------- session.json の監視
// 外部（Claude Code）がトラックを足した・位置やガイドを変えたら renderer に知らせる（list_tracks で読み直す）。
function sessionJson() {
  return state.sessionDir ? path.join(state.sessionDir, 'session.json') : null;
}

function hashSession() {
  const p = sessionJson();
  if (!p) return null;
  try {
    return crypto.createHash('sha1').update(fs.readFileSync(p)).digest('hex');
  } catch {
    return null;
  }
}

function rememberSessionHash() {
  state.lastSessionHash = hashSession();
}

function watchSession(dir) {
  if (state.sessionDir === dir && state.sessionWatcher) return;
  if (state.sessionWatcher) { try { state.sessionWatcher.close(); } catch { /* noop */ } }
  if (state.sessionDir !== dir) state.playable.clear();   // 別のセッション: 前の音のファイルはもう読まない
  state.sessionDir = dir;
  rememberSessionHash();
  try {
    state.sessionWatcher = fs.watch(dir, { persistent: false }, (_ev, name) => {
      if (name && !String(name).startsWith('session.json')) return;
      clearTimeout(state.sessionTimer);
      state.sessionTimer = setTimeout(() => {
        const h = hashSession();
        if (!h || h === state.lastSessionHash) return;   // 自分の操作で変わったぶんは無視
        state.lastSessionHash = h;
        if (state.inflight > 0) return;                  // 自分が呼んでいる最中の書き込み
        // ジョブ（解析など。session.json は書かない）の間に外部が書いた: 終わってから知らせる
        if (state.jobs.size) { state.sessionPending = true; return; }
        state.win?.webContents.send('session-changed', { dir });
      }, 150);
    });
  } catch (e) {
    console.error('session.json を監視できなかった:', e.message);
  }
}

// ---------------------------------------------------------------- ファイル読み
function allowed(p) {
  const abs = path.resolve(p);
  if (state.playable.has(abs)) return true;
  // 開発版だけ、リポジトリの projects/（旧形式の置き場）も読んでよい
  const roots = [state.projectDir, state.sessionDir, ARGS.projectDir,
    ...(app.isPackaged ? [] : [path.join(REPO, 'projects')])]
    .filter(Boolean).map((r) => path.resolve(r));
  return roots.some((r) => abs === r || abs.startsWith(r + path.sep));
}

// ---------------------------------------------------------------- メニュー（中身の正。画面はタイトルバーが描く）
function send(cmd, arg) {
  state.win?.webContents.send('menu', { cmd, arg });
}

function recentItems() {
  const list = (loadSettings().recent || []).filter((r) => fs.existsSync(r.path || r.take || ''));
  if (!list.length) return [{ label: '（まだ無い）', enabled: false }];
  return list.map((r) => ({
    // .gliss はファイル名から拡張子を外して、旧形式（テイクの WAV）はファイル名のまま
    label: r.path ? path.basename(r.path, path.extname(r.path)) : path.basename(r.take),
    toolTip: r.path || r.take,
    click: () => send('open-recent', r),
  }));
}

/** renderer から来た並び（commands.js の appMenuTemplate）→ Electron のメニュー。 */
function menuItem(it) {
  if (it.sep) return { type: 'separator' };
  if (it.recent) return { label: it.label, submenu: recentItems() };
  if (it.submenu) return { label: it.label, submenu: it.submenu.map(menuItem) };
  if (it.role) return { role: it.role, label: it.label };
  const m = {
    label: it.label, enabled: it.enabled !== false, click: () => send(it.cmd),
    // キーは renderer が処理する（設定で変えたキーと二重に効かないように、表記だけ）
    registerAccelerator: false,
  };
  if (it.accelerator) m.accelerator = it.accelerator;
  if ('checked' in it) { m.type = 'checkbox'; m.checked = !!it.checked; }
  return m;
}

function buildMenu() {
  const tpl = state.menuTemplate || [
    // 画面が並びを送ってくるまで（起動の一瞬）
    { label: 'ファイル', submenu: [{ role: 'quit', label: '終了' }] },
  ];
  const t = tpl.map((top) => ({ label: top.label, submenu: top.submenu.map(menuItem) }));
  state.menu = Menu.buildFromTemplate(t);
  // アプリのメニューにも入れておく（main で足した項目の accelerator が効く・既定のメニューを出さない）。
  // タイトルバーを隠したウィンドウでは Windows のメニューバーは出ない
  Menu.setApplicationMenu(state.menu);
  sendMenuModel();
}

/** 画面のメニューバーに渡す accelerator の表記（Windows のメニューと同じ「Ctrl+Shift+S」）。 */
function acceleratorLabel(a) {
  if (!a) return null;
  return String(a).split('+').map((p) => (/^(CmdOrCtrl|CommandOrControl|Control|Ctrl)$/i.test(p) ? 'Ctrl'
    : /^(Option|AltGr)$/i.test(p) ? 'Alt' : /^num(\d)$/.test(p) ? `Num${p.slice(3)}` : p)).join('+');
}

/** Electron の Menu → 画面のメニューバーが描く形（JSON）。id は項目の位置（"0.3.1"）で、押されたら
 * `menuItemAt` で同じ項目を引く。名前・有効・チェック・キー・下の段はすべて Menu の値をそのまま写す。 */
function menuModel(menu = state.menu, prefix = '') {
  if (!menu) return [];
  return menu.items.map((it, i) => {
    const id = prefix ? `${prefix}.${i}` : String(i);
    const accel = it.accelerator || (it.role && typeof it.getDefaultRoleAccelerator === 'function'
      ? it.getDefaultRoleAccelerator() : null) || null;
    return {
      id,
      type: it.type,                       // normal / separator / submenu / checkbox / radio
      label: it.label || '',
      role: it.role || null,
      enabled: it.enabled !== false,
      visible: it.visible !== false,
      checked: !!it.checked,
      accelerator: it.accelerator || null, // Electron の表記のまま（テストが見る）
      accelLabel: acceleratorLabel(accel), // 画面に出す表記
      toolTip: it.toolTip || '',
      submenu: it.submenu ? menuModel(it.submenu, id) : null,
    };
  });
}

function menuItemAt(id) {
  let items = state.menu?.items;
  let it = null;
  for (const k of String(id ?? '').split('.')) {
    it = items?.[Number(k)];
    if (!it) return null;
    items = it.submenu?.items;
  }
  return it;
}

function sendMenuModel() {
  const wc = state.win?.webContents;
  if (!wc || wc.isDestroyed?.()) return;
  wc.send('app-menu', menuModel());
}

// ---------------------------------------------------------------- ウィンドウ
function createWindow() {
  const s = loadSettings();
  const b = s.bounds || {};
  // テスト（VOCAL_EDITOR_HIDDEN=1）: 画面の上に**透明（opacity 0）**で showInactive する。見えず、前面も奪わない。
  // show しないままだと撮影（page.screenshot）が返らない。画面外に置くとタイトルバー一体型の中身の幅が
  // 窓より 15px 広くなり（titlebar.spec の T6）、最大化で画面に出てしまうので、画面の上で透明にする
  state.win = new BrowserWindow({
    width: b.width || 1280,
    height: b.height || 760,
    x: b.x, y: b.y,
    backgroundColor: '#111113',
    show: false,
    opacity: HIDDEN ? 0 : 1,
    skipTaskbar: HIDDEN,
    focusable: !HIDDEN,
    title: APP_NAME,
    icon: fs.existsSync(ICON_ICO) ? ICON_ICO : undefined,
    // タイトルバー一体型: 画面が上端まで描き、最小化・最大化・閉じるは OS が右上に重ねる
    titleBarStyle: 'hidden',
    titleBarOverlay: TITLE_BAR,
    webPreferences: {
      preload: path.join(HERE, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 隠れても再生（ループの予約・再生位置）を止めない
      backgroundThrottling: false,
    },
  });
  if (MUTED) state.win.webContents.setAudioMuted(true);
  // テスト（Playwright）: 人の実マウスを受けない（ウィンドウの上を通っても、下のウィンドウへ素通しにする）。
  // テストの操作（CDP）はそのまま届く。テストのドラッグの最中に人がマウスをウィンドウの上で動かすと、
  // Chromium はポインタのキャプチャを外し、ボタンを押していない move を送ってくる。画面はそれを
  // 「離したことが届かなかった」として扱う（state.js の buttonReleased）ので、テストのドラッグがそこで終わってしまう
  // 透明のとき（HIDDEN）は、見えない窓が人のクリックを取らないよう必ず素通しにする
  if (HIDDEN || process.env.VOCAL_EDITOR_IGNORE_MOUSE === '1') state.win.setIgnoreMouseEvents(true);
  buildMenu();
  state.win.once('ready-to-show', () => HIDDEN ? state.win.showInactive() : state.win.show());
  state.win.on('close', (e) => {
    // 保存していない変更があれば、閉じる前に「保存しますか」（issue #33）。答えは renderer が聞いて、
    // 保存・捨てるを済ませてから app.closeNow で閉じ直す
    if (state.doc?.dirty && !state.allowClose && !state.win.webContents.isCrashed?.()) {
      e.preventDefault();
      state.win.webContents.send('confirm-close');
      return;
    }
    // テスト（HIDDEN）が変えた大きさ・位置を設定に残さない
    if (!HIDDEN) {
      try {
        const nb = state.win.getNormalBounds();
        saveSettings({ bounds: { x: nb.x, y: nb.y, width: nb.width, height: nb.height } });
      } catch { /* noop */ }
    }
  });
  // Alt+ドラッグ（接続を切って端を動かす）の後に Alt を離すと、Windows はメニューバーに
  // フォーカスを移してしまう（次のキーがメニューに取られる）。renderer が「Alt をドラッグに
  // 使った」と知らせてきたときだけ、その Alt の keyUp を止める。
  state.win.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'Alt' && input.type === 'keyUp' && state.altConsumed) {
      state.altConsumed = false;
      event.preventDefault();
    }
  });
  // WAV をウィンドウにドロップしたとき、renderer が受け損ねてもファイルへ移動（画面が消える）しない
  state.win.webContents.on('will-navigate', (event) => event.preventDefault());
  state.win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  state.win.loadFile(path.join(HERE, 'renderer', 'index.html'));
}

// ---------------------------------------------------------------- IPC
ipcMain.on('app.consumeAlt', () => { state.altConsumed = true; });

// ---------------------------------------------------------------- プロジェクトのファイル（issue #33）
/** 開いているプロジェクト（名前・未保存）。タイトルに「名前* — Gliss」（未保存なら *）。 */
ipcMain.on('app.setDoc', (_e, doc) => {
  state.doc = doc || null;
  const t = doc?.name ? `${doc.name}${doc.dirty ? '*' : ''} — ${APP_NAME}` : APP_NAME;
  if (state.win && state.win.getTitle() !== t) state.win.setTitle(t);
  sendTitle();
});
/** タイトルバーの中央に出す名前（ウィンドウのタイトルから「 — Gliss」を外したもの。何も開いていなければ空）。 */
function docTitle() {
  const d = state.doc;
  return d?.name ? `${d.name}${d.dirty ? '*' : ''}` : '';
}
function sendTitle() {
  const wc = state.win?.webContents;
  if (!wc || wc.isDestroyed?.()) return;
  wc.send('app-title', { title: state.win.getTitle(), doc: docTitle() });
}
/** 閉じる前の「保存しますか」に答え終わった: 閉じ直す。 */
ipcMain.on('app.closeNow', () => {
  state.allowClose = true;
  state.win?.close();
});
/** 「保存しますか」: 'save' / 'discard' / 'cancel'。 */
ipcMain.handle('app.askSave', async (_e, name) => {
  const r = await dialog.showMessageBox(state.win, {
    type: 'warning',
    buttons: ['保存', '保存しない', 'キャンセル'],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
    title: APP_NAME,
    message: `「${name || '無題'}」の変更を保存しますか？`,
    detail: '保存しないと、最後に保存してからの変更は失われます。',
  });
  return ['save', 'discard', 'cancel'][r.response] || 'cancel';
});
/** 開く（Ctrl+O）: .gliss・旧形式のプロジェクト（session.json / project.json）・音声（新しいプロジェクトのトラックに）。 */
ipcMain.handle('app.pickProject', async () => {
  const s = loadSettings();
  const r = await dialog.showOpenDialog(state.win, {
    title: 'プロジェクトを開く',
    defaultPath: s.lastProjectDir || s.lastDir || undefined,
    properties: ['openFile'],
    filters: [
      { name: 'Gliss のプロジェクト・音声', extensions: ['gliss', 'wav', 'flac', 'aiff', 'aif'] },
      { name: 'Gliss のプロジェクト', extensions: ['gliss'] },
      { name: '旧形式のプロジェクト（projects の session.json / project.json）', extensions: ['json'] },
      { name: '音声', extensions: ['wav', 'flac', 'aiff', 'aif'] },
    ],
  });
  if (r.canceled || !r.filePaths.length) return null;
  saveSettings({ lastProjectDir: path.dirname(r.filePaths[0]) });
  return r.filePaths[0];
});
/** 名前を付けて保存（Ctrl+Shift+S）の保存先。既定は最初のトラックの音声の隣（renderer が決める）。 */
ipcMain.handle('app.saveProjectDialog', async (_e, defaultPath) => {
  const r = await dialog.showSaveDialog(state.win, {
    title: 'プロジェクトを保存',
    defaultPath: defaultPath || undefined,
    filters: [{ name: 'Gliss のプロジェクト', extensions: ['gliss'] }],
    properties: ['createDirectory', 'showOverwriteConfirmation'],
  });
  if (r.canceled || !r.filePath) return null;
  saveSettings({ lastProjectDir: path.dirname(r.filePath) });
  return r.filePath;
});
// メニューバーの並び・名前・キーの表記（「元に戻す: ○○」を含む。issue #16・#17・#22）
ipcMain.on('app.setAppMenu', (_e, tpl) => {
  if (!Array.isArray(tpl)) return;
  state.menuTemplate = tpl;
  buildMenu();
});
// 画面のメニューバー（タイトルバー）: 中身（Menu を写したもの）と、押された項目
ipcMain.handle('app.getMenu', () => ({ menu: menuModel(), title: state.win?.getTitle() || APP_NAME, doc: docTitle() }));
ipcMain.handle('app.menuClick', (_e, id) => {
  const it = menuItemAt(id);
  if (!it || it.type === 'separator' || it.type === 'submenu' || it.enabled === false || it.visible === false) return false;
  // Electron の MenuItem.click: role の項目は role の動き、それ以外は click（チェックの項目は反転してから）
  it.click({ triggeredByAccelerator: false }, state.win, state.win?.webContents);
  return true;
});
ipcMain.handle('engine.call', async (_e, name, args) => {
  try {
    return await callTool(name, args);
  } catch (err) {
    return { ok: false, error: String(err?.message || err), tool: name };
  }
});

let modelDownload = null;
let modelProgress = { phase: 'idle', bytes: 0, total: totalModelBytes };
function publishModelProgress(progress) {
  modelProgress = progress;
  if (state.win && !state.win.isDestroyed()) state.win.webContents.send('app.modelProgress', progress);
}
ipcMain.handle('app.modelState', () => modelProgress);
ipcMain.handle('app.modelCancel', () => {
  modelDownload?.abort();
  return !!modelDownload;
});
ipcMain.handle('app.modelStart', async (_e, ids) => {
  if (modelDownload) return modelProgress;
  // ids: 取得するモデル（省くと必須のものだけ）。知らない id は無視する
  const known = new Set(sourcesFromEnvironment().map((s) => s.id));
  const wanted = Array.isArray(ids) ? ids.filter((id) => known.has(id)) : null;
  const info = await callTool('engine_info');
  if (info.ok === false) throw new Error(info.error || 'エンジンに接続できない');
  const controller = new AbortController();
  modelDownload = controller;
  // IPC の返答を待たせずに開始し、進捗をすぐ画面へ送る。
  void downloadModels({ net, sources: sourcesFromEnvironment(), modelsDir: MODELS_DIR,
    present: modelPresence(info), ids: wanted, signal: controller.signal, progress: publishModelProgress })
    .catch((err) => publishModelProgress({ phase: controller.signal.aborted ? 'cancelled' : 'failed',
      error: String(err.message || err), bytes: modelProgress.bytes, total: modelProgress.total }))
    .finally(() => { modelDownload = null; });
  return modelProgress;
});

// ---------------------------------------------------------------- 任意機能のアドオン（ヘルプ > モデルと追加の機能…）
let addonDownload = null;
let addonProgress = { id: null, phase: 'idle', bytes: 0, total: 0 };
function publishAddonProgress(progress) {
  addonProgress = progress;
  if (state.win && !state.win.isDestroyed()) state.win.webContents.send('app.addonProgress', progress);
}

/** 画面の行の状態（addons.mjs の addonStates）。reload: エンジンに置き場を見直させる（取得・削除の後）。 */
async function addonsSnapshot({ reload = false } = {}) {
  const info = state.client ? await callTool('engine_info', reload ? { reload_addons: true } : {}).catch(() => null) : null;
  const engine = info && info.ok !== false ? info.addons || null : null;
  return {
    dir: ADDONS_DIR,
    addons: addonStates({ catalog: ADDON_CATALOG, engine, dir: ADDONS_DIR,
      g2pAvailable: info?.phonemes?.g2p === 'pyopenjtalk-plus' }),
    progress: addonProgress,
  };
}
ipcMain.handle('app.addonsState', () => addonsSnapshot());
ipcMain.handle('app.addonCancel', () => {
  addonDownload?.abort();
  return !!addonDownload;
});
ipcMain.handle('app.addonStart', (_e, id) => {
  if (addonDownload) return addonProgress;
  const entry = ADDON_CATALOG.find((a) => a.id === id);
  if (!entry) throw new Error('この版には含まれていません');
  if (!ADDONS_DIR) throw new Error('開発版ではアドオンを扱いません（GLISS_ADDONS_DIR で置き場を指定します）');
  const controller = new AbortController();
  addonDownload = controller;
  publishAddonProgress({ id, phase: 'downloading', bytes: 0, total: entry.size });
  void downloadAddon({ net, entry, dir: ADDONS_DIR, signal: controller.signal,
    progress: (p) => { if (p.phase !== 'done') publishAddonProgress(p); } })
    // 画面のエンジンはその場で読み直す（再起動しなくてよい）。AI クライアントのエンジンは次に起動したときから
    .then(() => addonsSnapshot({ reload: true }))
    .then(() => publishAddonProgress({ id, phase: 'done', bytes: entry.size, total: entry.size }))
    .catch((err) => publishAddonProgress({ id, phase: controller.signal.aborted ? 'cancelled' : 'failed',
      error: String(err.message || err), bytes: addonProgress.bytes, total: entry.size }))
    .finally(() => { addonDownload = null; });
  return addonProgress;
});
ipcMain.handle('app.addonRemove', async (_e, id) => {
  const a = (await addonsSnapshot()).addons.find((x) => x.id === id);
  if (!a || !ADDONS_DIR) return { removed: false };
  const r = await dialog.showMessageBox(state.win, {
    type: 'question', title: APP_NAME, noLink: true,
    message: `「${a.title}」を削除しますか？`,
    detail: '使うときは、もう一度ダウンロードしてください。',
    buttons: ['削除', 'キャンセル'], defaultId: 1, cancelId: 1,
  });
  if (r.response !== 0) return { removed: false };
  const { pending } = removeAddon(ADDONS_DIR, id);
  return { removed: true, pending, ...(await addonsSnapshot({ reload: true })) };
});

ipcMain.handle('app.bootstrap', () => {
  const s = loadSettings();
  // 起動引数が無ければ前回のプロジェクト（lastDoc: .gliss / 無題の作業場所。旧形式は lastTake）
  const last = (!ARGS.take && !ARGS.project) ? (s.lastDoc || null) : null;
  const lastOk = !!last && fs.existsSync(last.path || last.work || '');
  const take = ARGS.take || (!ARGS.project && !lastOk ? s.lastTake : null) || null;
  // ガイドはセッション（テイクのプロジェクトの session.json）が覚えている。起動引数のときだけ渡す
  const guide = ARGS.guide !== undefined ? ARGS.guide : null;
  return {
    engineReady: !!state.client,
    engineError: state.engineError,
    take: take && fs.existsSync(take) ? take : null,
    guide: guide && fs.existsSync(guide) ? guide : null,
    lyrics: ARGS.lyrics || null,
    guideLyrics: ARGS.guideLyrics || null,
    projectDir: ARGS.projectDir || null,
    project: ARGS.project || (lastOk ? (last.path || last.work) : null),
    fromArgs: !!(ARGS.take || ARGS.project),
    view: s.view || null,
    keys: s.keys || {},               // キーボードショートカットの設定（既定と違うものだけ。issue #22）
    grid: s.grid || null,             // スナップのオン・オフとグリッドの細かさ（issue #18）
    preview: s.preview !== false,     // ノートをつかんでいる間に鳴らす（issue #27。既定は鳴らす）
    muted: MUTED,                     // 音を出さない起動（テスト）
    modelSizes: Object.fromEntries(sourcesFromEnvironment().map((s) => [s.id, s.size])),
  };
});

ipcMain.handle('app.pickFiles', async (_e, kind) => {
  const s = loadSettings();
  const r = await dialog.showOpenDialog(state.win, {
    title: kind === 'guide' ? 'ガイドの WAV を選ぶ（任意）'
      : kind === 'track' ? 'トラックに足す音声を選ぶ（ボーカルのテイク・伴奏）' : 'テイクの WAV を選ぶ',
    defaultPath: s.lastDir || undefined,
    properties: ['openFile'],
    filters: [{ name: '音声', extensions: ['wav', 'flac', 'aiff', 'aif'] }],
  });
  if (r.canceled || !r.filePaths.length) return null;
  saveSettings({ lastDir: path.dirname(r.filePaths[0]) });
  return r.filePaths[0];
});

ipcMain.handle('app.readJson', (_e, p) => {
  if (!allowed(p)) throw new Error(`読み取りを許していない場所: ${p}`);
  return JSON.parse(fs.readFileSync(p, 'utf8'));
});

ipcMain.handle('app.readFile', (_e, p) => {
  if (!allowed(p)) throw new Error(`読み取りを許していない場所: ${p}`);
  const buf = fs.readFileSync(p);
  return new Uint8Array(buf).buffer;
});

ipcMain.handle('app.saveState', (_e, patch) => saveSettings(patch || {}));

ipcMain.handle('app.pushRecent', (_e, entry) => pushRecent(entry));

/** 書き出し先を選ぶ（ファイル > 書き出し…）。既定は engine が決めたパス。 */
ipcMain.handle('app.saveDialog', async (_e, defaultPath) => {
  const r = await dialog.showSaveDialog(state.win, {
    title: 'WAV を書き出す',
    defaultPath: defaultPath || undefined,
    filters: [{ name: 'WAV', extensions: ['wav'] }],
    properties: ['createDirectory', 'showOverwriteConfirmation'],
  });
  return r.canceled ? null : r.filePath;
});

/** 歌詞のテキストファイルを開いて中身を返す（ファイル > 歌詞を読み込む）。 */
ipcMain.handle('app.openLyricsFile', async () => {
  const s = loadSettings();
  const r = await dialog.showOpenDialog(state.win, {
    title: '歌詞のテキストを選ぶ',
    defaultPath: s.lastLyricsDir || undefined,
    properties: ['openFile'],
    filters: [{ name: 'テキスト', extensions: ['txt', 'lab', 'csv', 'md'] }],
  });
  if (r.canceled || !r.filePaths.length) return null;
  saveSettings({ lastLyricsDir: path.dirname(r.filePaths[0]) });
  return { path: r.filePaths[0], text: fs.readFileSync(r.filePaths[0], 'utf8') };
});

/** 歌詞付きの譜面を選ぶ。内容は Python の import_lyrics が読む。 */
ipcMain.handle('app.pickLyricsScore', async () => {
  const s = loadSettings();
  const r = await dialog.showOpenDialog(state.win, {
    title: '歌詞付きの SVP / MIDI を選ぶ',
    defaultPath: s.lastLyricsDir || undefined,
    properties: ['openFile'],
    filters: [{ name: '歌詞付き譜面', extensions: ['svp', 'mid', 'midi'] }],
  });
  if (r.canceled || !r.filePaths.length) return null;
  saveSettings({ lastLyricsDir: path.dirname(r.filePaths[0]) });
  return r.filePaths[0];
});

ipcMain.handle('app.chooseLyricsTrack', async (_e, tracks) => {
  if (!Array.isArray(tracks) || !tracks.length) return null;
  const buttons = [...tracks.map((t) => `${t.index + 1}: ${t.name}（${t.lyric_notes} 音符）`), 'キャンセル'];
  const r = await dialog.showMessageBox(state.win, {
    type: 'question', title: '読み込む歌詞のトラック',
    message: '読み込むトラックを選ぶ', buttons,
    defaultId: 0, cancelId: buttons.length - 1, noLink: true,
  });
  return r.response < tracks.length ? tracks[r.response].index : null;
});

/** 聞き取り（音声認識。issue #54）のモデルの初回ダウンロードの確認。重みは同梱しないので、
 * 大きさ・保存先・ライセンス・取得元を見せてから取得する。途中の取り消しは画面の進み具合から。 */
ipcMain.handle('app.confirmAsrDownload', async (_e, model) => {
  if (!model || typeof model !== 'object') return false;
  const r = await dialog.showMessageBox(state.win, {
    type: 'question',
    title: '聞き取りのモデルを取得',
    message: '聞き取り（音声認識）に使うモデルをダウンロードしますか？',
    detail: [
      `モデル: ${model.label || model.id}`,
      `大きさ: ${model.size_text || ''}`,
      `保存先: ${model.path || ''}`,
      `ライセンス: ${model.license || '不明'}（${model.license_url || ''}）`,
      model.upstream ? `由来: ${model.upstream}` : null,
      `取得元: ${model.source_url || ''}`,
      '',
      '取得は初回だけです。途中で取り消せます（Esc）。',
      'モデルは Gliss に同梱していません。',
    ].filter((l) => l !== null).join('\n'),
    buttons: ['ダウンロード', 'キャンセル'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  return r.response === 0;
});

/** 書き出したファイルをエクスプローラで示す。 */
ipcMain.handle('app.reveal', (_e, p) => {
  try { shell.showItemInFolder(path.resolve(p)); } catch { /* noop */ }
});

/** 文字をクリップボードへ（右クリックの「AI に頼む」）。 */
ipcMain.handle('app.copyText', (_e, text) => {
  clipboard.writeText(String(text ?? ''));
  return true;
});

// ---------------------------------------------------------------- AI とつなぐ（ヘルプ > AI とつなぐ…）
/** ダイアログを開いたとき: Claude Code・Claude Desktop に登録済みか、AI に許可。 */
ipcMain.handle('ai.status', async () => {
  const [cc, cd] = await Promise.all([
    AI.claudeCodeStatus().catch((e) => ({ available: false, registered: false, reason: e.message })),
    Promise.resolve().then(() => AI.desktopStatus()),
  ]);
  return {
    claudeCode: { available: cc.available, registered: cc.registered, reason: cc.reason || null },
    desktop: { installed: cd.installed, registered: cd.registered },
    allow: AI.allowOf(AI.readBridge(bridgePath())),
  };
});
ipcMain.handle('ai.addClaudeCode', async () => {
  try { return await AI.addClaudeCode(aiServerConfig()); } catch (e) { return { ok: false, error: e.message }; }
});
ipcMain.handle('ai.addDesktop', () => {
  try {
    if (!AI.desktopInstalled()) return { ok: false, error: 'Claude Desktop が見つからない' };
    return AI.addDesktop(aiServerConfig());
  } catch (e) {
    return { ok: false, error: e.message };
  }
});
/** その他: mcpServers の JSON 断片をクリップボードへ。 */
ipcMain.handle('ai.copyConfig', () => {
  const text = AI.snippet(aiServerConfig());
  clipboard.writeText(text);
  return text;
});
/** AI に許可（編集・保存・書き出し）。bridge.json に書く（AI 側のエンジンがツールのたびに読む）。 */
ipcMain.handle('ai.setAllow', (_e, patch) => {
  const cur = AI.allowOf(AI.readBridge(bridgePath()));
  const next = { ...cur };
  for (const k of ['edit', 'save']) if (patch && k in patch) next[k] = !!patch[k];
  return writeBridge({ allow: next })?.allow || cur;
});

// ---------------------------------------------------------------- 自動更新（ヘルプ > 更新を確認…）
const UPDATE_FIRST_CHECK_MS = 15 * 1000;          // 起動の少し後
const UPDATE_INTERVAL_MS = 4 * 60 * 60 * 1000;    // 4 時間おき（ウィンドウに戻ったときも、前回からこれだけたっていれば）
const updateConfigFile = () => path.join(process.resourcesPath || '', 'app-update.yml');

/** 更新の記録（userData/updates.log。1 MB を超えたら作り直す）。生のエラーはここにだけ残す。 */
function updateLog(level, ...args) {
  const line = `${new Date().toISOString()} ${level} ${args.map((a) => (a instanceof Error ? (a.stack || a.message) : String(a))).join(' ')}\n`;
  (level === 'error' ? console.error : console.log)(`[更新] ${line.trimEnd()}`);
  try {
    const f = path.join(app.getPath('userData'), 'updates.log');
    if (fs.existsSync(f) && fs.statSync(f).size > 1 << 20) fs.rmSync(f, { force: true });
    fs.appendFileSync(f, line, 'utf8');
  } catch { /* 記録できなくても更新は続ける */ }
}
const updateLogger = {
  info: (...a) => updateLog('info', ...a), warn: (...a) => updateLog('warn', ...a),
  error: (...a) => updateLog('error', ...a), debug: () => {},
};

/** electron-updater の autoUpdater を用意する（配布版で app-update.yml があるときだけ）。 */
function createUpdater() {
  const { autoUpdater } = electronUpdater;
  autoUpdater.logger = updateLogger;
  autoUpdater.disableWebInstaller = true;   // Web インストーラ（nsis-web）は作らない
  // 無署名の間は Windows の署名検証をしない。electron-updater の NsisUpdater は app-update.yml に
  // publisherName が無いと検証を飛ばす（electron-builder は署名の設定が無ければ書かない）。ダウンロードの完全性は
  // latest.yml の SHA-512 で見る。**署名を付ける版から**、electron-builder.yml の win に発行元名を固定して
  // 検証を有効にする（docs/release-plan.md §5。発行元名は一度付けたら変えない）
  const config = readUpdateConfig(fs.readFileSync(updateConfigFile(), 'utf8'));
  if (config.provider === 'github') {
    // beta の人の「いちばん新しい版」を semver で選び直す（update-provider.mjs）
    autoUpdater.setFeedURL({ ...config, provider: 'custom', updateProvider: newestReleaseProvider() });
  }
  return autoUpdater;
}

/** 「再起動して更新」の中身。取り消されたら false。 */
async function installUpdate() {
  const engineDir = path.dirname(packagedEngineExe(process.resourcesPath));
  const own = state.transport?.pid ?? null;
  // AI クライアント（Claude Code / Desktop）に登録したエンジンも同じ exe で動く。止まることを先に知らせる
  const others = (await findEngineProcesses(engineDir).catch((e) => { updateLog('warn', 'エンジンを探せなかった', e); return []; }))
    .filter((pid) => pid !== own);
  if (others.length) {
    const r = await dialog.showMessageBox(state.win, {
      type: 'warning', title: APP_NAME, noLink: true,
      message: 'AI クライアントから使っているエンジンも止まります',
      detail: `Claude Code・Claude Desktop などから使っている Gliss のエンジン（${others.length} 個）を止めてから更新します。\n更新が終わったら、AI クライアントの側でつなぎ直してください。`,
      buttons: ['再起動して更新', 'キャンセル'], defaultId: 0, cancelId: 1,
    });
    if (r.response !== 0) return false;
  }
  // 保存していない変更があれば「保存しますか」（renderer の confirmDiscard。キャンセルなら中止）
  if (!await askRenderer('confirm-update')) return false;
  state.allowClose = true;
  // 画面が起動したエンジンを止める → インストール先の下で動くエンジンをすべて止める
  try { await state.client?.close(); } catch { /* noop */ }
  state.client = null;
  const { stopped, left } = await stopEngineProcesses(engineDir);
  updateLog('info', `エンジンを止めた: ${stopped.length} 個${left.length ? `、止まらなかった: ${left.join(', ')}` : ''}`);
  // 止まらなかったものはインストーラの既定の確認（CHECK_APP_RUNNING）がもう一度止める
  updateLog('info', 'quitAndInstall');
  // 画面を出さずに入れ替え（/S）、終わったら起動し直す（--force-run）。入れ先は前回のまま（HKCU の InstallLocation）
  setImmediate(() => electronUpdater.autoUpdater.quitAndInstall(true, true));
  return true;
}

let askSeq = 0;
/** renderer に確かめて答え（true / false）を待つ（「保存しますか」のように、利用者の答えを待つもの）。 */
function askRenderer(channel) {
  const wc = state.win?.webContents;
  if (!wc || wc.isDestroyed()) return Promise.resolve(true);
  const id = ++askSeq;
  return new Promise((resolve) => {
    const on = (_e, rid, ok) => {
      if (rid !== id) return;
      ipcMain.removeListener('app.answer', on);
      resolve(!!ok);
    };
    ipcMain.on('app.answer', on);
    wc.send(channel, id);
  });
}

/** アプリ内の「更新の内容」（scripts/release-notes.mjs が作る release-info.json。開発版には無いことがある）。 */
function releaseInfo() {
  try { return JSON.parse(fs.readFileSync(path.join(HERE, 'release-info.json'), 'utf8')); } catch { return null; }
}

function setupUpdates() {
  const enabled = app.isPackaged && fs.existsSync(updateConfigFile());
  let updater = null;
  try {
    // 開発版・app-update.yml の無い展開版では electron-updater を作らない（unavailable のまま何もしない）
    if (enabled) updater = createUpdater();
  } catch (e) {
    updateLog('error', '更新の準備に失敗した', e);
  }
  state.updates = new Updates({
    updater: updater || new EventEmitter(), version: app.getVersion(),
    file: path.join(app.getPath('userData'), 'updates.json'),
    enabled: !!updater, trackVersion: app.isPackaged,
    install: installUpdate, log: (e) => updateLog('error', e),
  });
  state.updates.on('state', (s) => {
    const wc = state.win?.webContents;
    if (wc && !wc.isDestroyed()) wc.send('updates-state', s);
  });
  return state.updates.init().catch((e) => updateLog('error', '更新の設定を読めなかった', e));
}

function scheduleUpdateChecks() {
  const u = state.updates;
  if (!u?.enabled) return;
  setTimeout(() => u.auto(), UPDATE_FIRST_CHECK_MS);
  setInterval(() => u.auto(UPDATE_INTERVAL_MS), UPDATE_INTERVAL_MS);
  // 寝ていた PC を起こしたとき（タイマーが遅れる）のために、ウィンドウに戻ったときも
  state.win?.on('focus', () => u.auto(UPDATE_INTERVAL_MS));
}

ipcMain.handle('updates.state', () => ({ ...(state.updates?.snapshot() || { phase: 'unavailable' }), notes: releaseInfo() }));
ipcMain.handle('updates.check', () => state.updates?.check());
ipcMain.handle('updates.download', () => state.updates?.download());
ipcMain.handle('updates.apply', () => state.updates?.apply());
ipcMain.handle('updates.setPreferences', (_e, value) => state.updates?.setPreferences(value));
ipcMain.handle('updates.dismissNotice', () => state.updates?.dismissNotice());

ipcMain.handle('app.screenshot', async (_e, name) => {
  const img = await state.win.webContents.capturePage();
  const out = path.isAbsolute(name) ? name : path.join(HERE, 'docs', name);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, img.toPNG());
  return out;
});

// ---------------------------------------------------------------- 起動
app.whenReady().then(async () => {
  migrateLegacySettings();
  // 前回の「画面で開いている曲」を残さない（開いたら callTool が書く）。AI に許可はそのまま
  state.bridgeProject = null;
  writeBridge({ project: null });
  // ヘルプ > Gliss について（メニューの role: 'about'）。GPL の告知（著作権・無保証・ソースの場所）も出す
  app.setAboutPanelOptions({
    applicationName: APP_NAME,
    applicationVersion: app.getVersion(),
    copyright: 'Copyright © 2026 tekalu',
    credits: [
      'GNU General Public License バージョン 3 以降（GPL-3.0-or-later）で配布しています。',
      'このプログラムは無保証です（法律で認められる範囲で、いかなる保証もありません）。',
      'ソースコード: https://github.com/tekalu1/gliss',
      '同梱のソフトウェアのライセンス: インストール先の resources\\THIRD_PARTY_NOTICES.txt',
    ].join('\n'),
    ...(fs.existsSync(ICON_PNG) ? { iconPath: ICON_PNG } : {}),
  });
  await setupUpdates();
  // 削除の待ちのアドオン（前回は使用中で消せなかったもの）を、エンジンが読み込む前に消す
  try {
    const removed = cleanupAddons(ADDONS_DIR);
    if (removed.length) console.log(`アドオンを削除した: ${removed.join(', ')}`);
  } catch (e) { console.error('アドオンを片付けられなかった:', e.message); }
  try {
    await connectEngine();
  } catch (err) {
    state.engineError = engineFailureMessage(err);
    console.error(state.engineError);
    // テスト時の起動失敗は前面にダイアログを出さず、ログで確認する。
    if (!HIDDEN) dialog.showErrorBox('エンジンに接続できない', state.engineError);
  }
  createWindow();
  scheduleUpdateChecks();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', async () => {
  // 画面を閉じたら「画面で開いている曲」は無い（AI の load_project() は分かるエラーを返す）
  if (state.bridgeProject) writeBridge({ project: null });
  try { await state.client?.close(); } catch { /* noop */ }
  try { state.watcher?.close(); } catch { /* noop */ }
  try { state.sessionWatcher?.close(); } catch { /* noop */ }
  app.quit();
});
