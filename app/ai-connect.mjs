// AI とつなぐ（ヘルプ > AI とつなぐ…）。main.mjs から使う。Electron に依存しない（テストから直接呼べる）。
//
//  - serverConfig(): AI のクライアントに登録する MCP サーバーの設定（command / args / env）を 1 か所で組み立てる。
//    開発版は画面がエンジンの起動に使っている `.mcp.json` の gliss をもとに、**どのフォルダーから起動しても動く**形
//    （cwd に頼らず PYTHONPATH にエンジンのディレクトリ）にする。配布版（app.isPackaged）は同梱の実行ファイルを指す。
//  - Claude Code: `claude mcp add-json gliss '<json>' --scope user`（ユーザー全体）。登録済みかは `claude mcp get gliss`。
//  - Claude Desktop: %APPDATA%\Claude\claude_desktop_config.json の mcpServers に gliss を 1 件だけ足す
//    （ほかのキーは保持。無ければ作る。書く前にバックアップ）。
//  - bridge.json: 画面と AI 側のエンジンの橋渡し（AI に許可・画面で開いている曲。engine/vocal_engine/bridge.py）。
//
// テストでは本物の `claude` と Claude Desktop の設定に触らない: 環境変数 GLISS_CLAUDE（claude の実行ファイル。
// 空文字 = 無いことにする）と GLISS_CLAUDE_DESKTOP_CONFIG（設定ファイルのパス）で差し替える。
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { packagedEngineExe } from './paths.mjs';

export const SERVER_ID = 'gliss';

// ---------------------------------------------------------------- 登録する設定
/**
 * AI のクライアントに登録する MCP サーバーの設定。
 * @param {object} o
 * @param {boolean} o.packaged  配布版（app.isPackaged）
 * @param {{command: string, args?: string[]}} [o.engine]  開発版: 画面がエンジンの起動に使っている command / args
 * @param {string} [o.engineDir]  開発版: エンジンのディレクトリ（vocal_engine の親。PYTHONPATH に入れる）
 * @param {string} [o.resourcesDir]  配布版: process.resourcesPath
 * @param {string} [o.bridge]  bridge.json のパス（画面の userData の中）
 * @param {object} [o.env]  画面の環境変数（エンジンに渡しているもののうち、登録にも要るものを写す）
 */
export function serverConfig({ packaged, engine, engineDir, resourcesDir, bridge, env = {} }) {
  const out = packaged
    // 配布版: 同梱の単体 exe（engine/packaging/vocal-engine.spec のフォルダ形式。画面が起動するものと同じ）を指す。
    // 重みの置き場は画面が決める（%LOCALAPPDATA%\Gliss\models）が、AI 側の exe には環境変数が渡らないので、
    // エンジンの既定（config.py）が同じ場所を見る必要がある（packaged のとき exe の隣の models/ ではなく LOCALAPPDATA）
    ? { command: packagedEngineExe(resourcesDir), args: [], env: {} }
    : {
      command: engine.command,
      args: [...(engine.args || [])],
      // Claude Code の設定は cwd を効かせないので、エンジンの場所は PYTHONPATH で渡す
      env: { PYTHONPATH: engineDir, PYTHONIOENCODING: 'utf-8' },
    };
  // 重みの置き場を差し替えて動かしている（worktree など）ときは、AI 側のエンジンにも同じ場所を渡す
  if (!packaged && env.VOCAL_ENGINE_MODELS_DIR) out.env.VOCAL_ENGINE_MODELS_DIR = env.VOCAL_ENGINE_MODELS_DIR;
  if (bridge) out.env.GLISS_BRIDGE = bridge;
  return out;
}

/** mcpServers の JSON 断片（「その他」の設定をコピー）。 */
export function snippet(cfg) {
  return JSON.stringify({ mcpServers: { [SERVER_ID]: cfg } }, null, 2);
}

// ---------------------------------------------------------------- Claude Code
/** PATH から実行ファイルを探す（Windows は PATHEXT の拡張子。拡張子の無いシェルスクリプトは使わない）。 */
export function which(name, env = process.env) {
  const dirs = String(env.PATH || env.Path || '').split(path.delimiter).filter(Boolean);
  const exts = process.platform === 'win32'
    ? String(env.PATHEXT || '.COM;.EXE;.BAT;.CMD').split(';').filter(Boolean)
    : [''];
  for (const d of dirs) {
    for (const e of exts) {
      const p = path.join(d, name + e.toLowerCase());
      try { if (fs.statSync(p).isFile()) return p; } catch { /* 無い */ }
    }
  }
  return null;
}

/** `claude` の実行ファイル（無ければ null）。GLISS_CLAUDE で差し替える（空文字 = 無い）。 */
export function findClaude(env = process.env) {
  if (env.GLISS_CLAUDE !== undefined) {
    return env.GLISS_CLAUDE && fs.existsSync(env.GLISS_CLAUDE) ? env.GLISS_CLAUDE : null;
  }
  return which('claude', env);
}

// cmd.exe に渡す引数の書き方（cross-spawn と同じ）。.cmd / .bat は cmd.exe を通さないと起動できず
// （Node は shell なしの .cmd を EINVAL で断る）、JSON の " を壊さずに渡すにはこう書く。
// npm の .cmd（中で node を %* で呼ぶ）は 2 回解釈されるので ^ を 2 重にする
const META = /([()\][%!^"`<>&|;, *?])/g;
// （実行ファイルのパスは引用符で囲まず、記号の前に ^ を付けるだけ）
function cmdQuote(arg, twice) {
  let a = String(arg);
  a = a.replace(/(?=(\\+?)?)\1"/g, '$1$1\\"');
  a = a.replace(/(?=(\\+?)?)\1$/, '$1$1');
  a = `"${a}"`;
  a = a.replace(META, '^$1');
  if (twice) a = a.replace(META, '^$1');
  return a;
}

/** コマンドを実行する。{ code, stdout, stderr }。 */
export function run(file, args, { cwd = os.homedir(), env = process.env, timeout = 60000 } = {}) {
  return new Promise((resolve) => {
    let cmd = file; let argv = args; const opt = { cwd, env, timeout, windowsHide: true, maxBuffer: 4 << 20 };
    if (process.platform === 'win32' && /\.(cmd|bat)$/i.test(file)) {
      cmd = env.ComSpec || process.env.ComSpec || 'cmd.exe';
      const line = [path.normalize(file).replace(META, '^$1'), ...args.map((a) => cmdQuote(a, true))].join(' ');
      argv = ['/d', '/s', '/c', `"${line}"`];
      opt.windowsVerbatimArguments = true;
    }
    execFile(cmd, argv, opt, (err, stdout, stderr) => {
      const code = err ? (typeof err.code === 'number' ? err.code : -1) : 0;
      resolve({ code, stdout: String(stdout || ''), stderr: String(stderr || '') || (err && code === -1 ? err.message : '') });
    });
  });
}

/** Claude Code の状態: { available, registered, reason }。 */
export async function claudeCodeStatus(env = process.env) {
  const exe = findClaude(env);
  if (!exe) return { available: false, registered: false, reason: 'claude コマンドが見つからない（Claude Code をインストールして PATH に入れる）' };
  // 作業フォルダーはホーム（今のフォルダーの .mcp.json の gliss を「登録済み」と取り違えない）
  const r = await run(exe, ['mcp', 'get', SERVER_ID], { env });
  return { available: true, registered: r.code === 0, exe };
}

/** Claude Code にユーザー全体（--scope user）で登録する。 */
export async function addClaudeCode(cfg, env = process.env) {
  const exe = findClaude(env);
  if (!exe) return { ok: false, error: 'claude コマンドが見つからない' };
  const r = await run(exe, ['mcp', 'add-json', SERVER_ID, JSON.stringify({ type: 'stdio', ...cfg }), '--scope', 'user'], { env });
  if (r.code === 0) return { ok: true };
  const msg = (r.stderr || r.stdout).trim();
  // 登録済み（状態を見た後で別の所から足された）: 追加済みとして扱う
  if (/already exists/i.test(msg)) return { ok: true, already: true };
  return { ok: false, error: msg || `claude mcp add-json が終了コード ${r.code} で失敗した` };
}

// ---------------------------------------------------------------- Claude Desktop
/** claude_desktop_config.json のパス。GLISS_CLAUDE_DESKTOP_CONFIG で差し替える。 */
export function desktopConfigPath(env = process.env) {
  if (env.GLISS_CLAUDE_DESKTOP_CONFIG) return path.resolve(env.GLISS_CLAUDE_DESKTOP_CONFIG);
  const appData = env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
  return path.join(appData, 'Claude', 'claude_desktop_config.json');
}

/** Claude Desktop が入っているか（設定のフォルダーか、インストール先があるか）。 */
export function desktopInstalled(env = process.env) {
  const p = desktopConfigPath(env);
  if (fs.existsSync(path.dirname(p))) return true;
  if (env.GLISS_CLAUDE_DESKTOP_CONFIG) return false;       // テスト: 差し替えた場所だけを見る
  const local = env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return fs.existsSync(path.join(local, 'AnthropicClaude'));
}

function readJsonFile(p) {
  if (!fs.existsSync(p)) return null;
  const text = fs.readFileSync(p, 'utf8').replace(/^﻿/, '');
  if (!text.trim()) return {};
  return JSON.parse(text);
}

/** Claude Desktop の状態: { installed, registered, path }。 */
export function desktopStatus(env = process.env) {
  const p = desktopConfigPath(env);
  const installed = desktopInstalled(env);
  let registered = false;
  try { registered = !!readJsonFile(p)?.mcpServers?.[SERVER_ID]; } catch { /* 読めない = まだ */ }
  return { installed, registered, path: p };
}

/** mcpServers に gliss を 1 件だけ足す（ほかのキーは保持。無ければ作る。書く前にバックアップ）。 */
export function addDesktop(cfg, env = process.env) {
  const p = desktopConfigPath(env);
  let doc;
  try {
    doc = readJsonFile(p);
  } catch (e) {
    return { ok: false, error: `設定ファイルが JSON として読めないので書き換えない: ${p}（${e.message}）` };
  }
  if (doc !== null && (typeof doc !== 'object' || Array.isArray(doc))) {
    return { ok: false, error: `設定ファイルの形が想定と違うので書き換えない: ${p}` };
  }
  let backup = null;
  fs.mkdirSync(path.dirname(p), { recursive: true });
  if (doc !== null) {
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '');
    backup = `${p}.gliss-backup-${stamp}`;
    fs.copyFileSync(p, backup);
  }
  const next = doc || {};
  const servers = next.mcpServers && typeof next.mcpServers === 'object' && !Array.isArray(next.mcpServers)
    ? next.mcpServers : {};
  next.mcpServers = { ...servers, [SERVER_ID]: cfg };
  const tmp = `${p}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, p);
  return { ok: true, path: p, backup };
}

// ---------------------------------------------------------------- bridge.json
export const DEFAULT_ALLOW = { edit: true, save: false };

export function readBridge(p) {
  try {
    const d = JSON.parse(fs.readFileSync(p, 'utf8'));
    return d && typeof d === 'object' ? d : {};
  } catch {
    return {};
  }
}

export function allowOf(d) {
  const a = d?.allow && typeof d.allow === 'object' ? d.allow : {};
  return { edit: 'edit' in a ? !!a.edit : DEFAULT_ALLOW.edit, save: 'save' in a ? !!a.save : DEFAULT_ALLOW.save };
}

/** bridge.json を書き換える（patch を浅く重ねる。書いた中身を返す）。 */
export function writeBridge(p, patch) {
  const next = { ...readBridge(p), ...patch, version: 1, updated_at: new Date().toISOString() };
  next.allow = allowOf(next);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = `${p}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf8');
  fs.renameSync(tmp, p);
  return next;
}

/** エンジンの返り値（document・session）→ bridge.json の project（変わらなければ undefined）。
 * 開くパス: .gliss はそのファイル、無題・旧形式は作業場所（load_project が開ける形）。 */
export function projectFromResult(out, prev) {
  if (!out || typeof out !== 'object' || out.ok === false) return undefined;
  const d = 'document' in out ? out.document : (out.session && 'document' in out.session ? out.session.document : undefined);
  if (d === undefined) return undefined;
  if (d === null) return prev === null ? undefined : null;
  const open = d.kind === 'gliss' ? d.path : d.work_dir;
  if (!open) return undefined;
  // セッション: 多くのツールは session に、list_tracks は返り値そのものが同じ形
  const sess = out.session && Array.isArray(out.session.tracks) ? out.session
    : (Array.isArray(out.tracks) && 'current' in out ? out : null);
  const same = prev && prev.path === open;
  let track = same ? prev.track : null; let trackName = same ? prev.track_name : null;
  if (sess) {
    track = sess.current || null;
    trackName = sess.tracks.find((t) => t.id === track)?.name || null;
  }
  const next = { path: open, kind: d.kind, name: d.name || null, track, track_name: trackName };
  return JSON.stringify(next) === JSON.stringify(prev || null) ? undefined : next;
}
