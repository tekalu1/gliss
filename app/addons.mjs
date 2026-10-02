// 任意機能のアドオン（配布版のエンジン exe に入れていない Python の依存）の目録・取得・削除。Electron 非依存。
//
// アドオンは Gliss 自身の GitHub Release に添付する zip（scripts/build-addon.mjs が作る
// Gliss-addon-<id>-<key>.zip）。取得先とサイズ・SHA-256 は、ビルドのときに作る目録（addons-catalog.json。
// scripts/addon-catalog.mjs）をアプリに埋めて持つ。URL はアプリ自身の版の Release:
//   https://github.com/tekalu1/gliss/releases/download/v<版>/<file>
// 置き場は %LOCALAPPDATA%\Gliss\addons\<id>\（エンジンの vocal_engine/addons.py が起動時に読む。
// 画面が起動したエンジンにも、AI クライアントに登録したエンジンにも効く）。
//
// 差し替え（確かめるとき）:
//   GLISS_ADDONS_DIR        置き場（開発版はこれを渡したときだけアドオンを扱う。エンジンにも同じ値が渡る）
//   GLISS_ADDON_BASE_URL    取得先の URL の前半（ローカルの HTTP サーバーなど。末尾の / は付けても付けなくてもよい）
//   GLISS_ADDON_CATALOG     目録の JSON のパス（埋めたものの代わりに使う）
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fetchArchive } from './model-download.mjs';
import { extractZip } from './unzip.mjs';

export const MANIFEST = 'gliss-addon.json';
export const REMOVE_MARK = 'gliss-addon.remove';
export const RELEASE_BASE = 'https://github.com/tekalu1/gliss/releases/download';

/** 画面に出すアドオン（目録に無いときも行は出す。「この版には含まれていません」）。 */
export const ADDONS = [
  {
    id: 'lyrics-ja', title: '漢字の歌詞の読み',
    description: '漢字まじりの歌詞を読みに開く。無くても、かなの歌詞は使える',
  },
];

/** アドオンの置き場。開発版は GLISS_ADDONS_DIR を渡したときだけ（venv に入れた依存を使う）。 */
export function addonDirectory({ packaged, env = process.env, localAppData }) {
  if (env.GLISS_ADDONS_DIR) return path.resolve(env.GLISS_ADDONS_DIR);
  if (packaged) return path.join(localAppData || path.join(os.homedir(), 'AppData', 'Local'), 'Gliss', 'addons');
  return null;
}

/** 目録 → [{ id, title, key, file, size, sha256, url, licenses, ... }]。無ければ []。 */
export function loadCatalog({ file, env = process.env, version }) {
  const src = env.GLISS_ADDON_CATALOG ? path.resolve(env.GLISS_ADDON_CATALOG) : file;
  let list = [];
  try { list = JSON.parse(fs.readFileSync(src, 'utf8')).addons || []; } catch { return []; }
  const base = (env.GLISS_ADDON_BASE_URL || `${RELEASE_BASE}/v${version}`).replace(/\/+$/, '');
  return list.filter((a) => a && a.id && a.file && a.size > 0 && /^[0-9a-f]{64}$/i.test(a.sha256 || ''))
    .map((a) => ({ ...a, url: `${base}/${encodeURIComponent(a.file)}` }));
}

/** 置き場にある manifest（読めなければ null）。 */
export function readInstalled(dir, id) {
  if (!dir) return null;
  try {
    const m = JSON.parse(fs.readFileSync(path.join(dir, id, MANIFEST), 'utf8'));
    return { ...m, removing: fs.existsSync(path.join(dir, id, REMOVE_MARK)) };
  } catch { return null; }
}

/**
 * 画面の行の状態。engine はエンジンの engine_info().addons（vocal_engine/addons.py の summary()）。
 * state: unavailable（この版には無い）・missing・ready（使える）・environment（開発の venv に入っている）・
 *        incompatible（このエンジンと合わない。取り直す）・removing（次の起動で消す）・unknown（エンジン未接続）
 */
export function addonStates({ catalog, engine, dir, g2pAvailable = null }) {
  return ADDONS.map((base) => {
    const entry = catalog.find((c) => c.id === base.id) || null;
    const installed = readInstalled(dir, base.id);
    const st = (engine?.installed || []).find((a) => a.id === base.id) || null;
    const out = {
      id: base.id, title: entry?.title || base.title, description: entry?.description || base.description,
      size: entry?.size || 0, installedSize: entry?.installedSize || 0, licenses: entry?.licenses || [],
      available: !!entry, managed: !!dir, key: installed?.key || null, reason: st?.reason || null,
    };
    if (installed?.removing) return { ...out, state: 'removing' };
    if (st?.source === 'environment' || (!dir && base.id === 'lyrics-ja' && g2pAvailable)) return { ...out, state: 'environment' };
    if (!dir) return { ...out, state: 'unavailable' };
    if (installed) {
      if (!engine) return { ...out, state: 'unknown' };
      if (st?.active || st?.compatible) return { ...out, state: 'ready' };
      return { ...out, state: 'incompatible' };
    }
    return { ...out, state: entry ? 'missing' : 'unavailable' };
  });
}

function cancelled(signal) {
  if (signal?.aborted) throw new Error('取り消した');
}

const tag = () => `${process.pid}-${crypto.randomBytes(4).toString('hex')}`;

/** フォルダをごみ箱用の名前に移してから消す。使用中（エンジンが .pyd を読み込んでいる）なら false。 */
export function discard(dir, id) {
  const target = path.join(dir, id);
  if (!fs.existsSync(target)) return true;
  const trash = path.join(dir, `.trash-${id}-${tag()}`);
  try {
    fs.renameSync(target, trash);
  } catch (e) {
    if (['EBUSY', 'EPERM', 'EACCES', 'ENOTEMPTY'].includes(e.code)) return false;
    throw e;
  }
  fs.rmSync(trash, { recursive: true, force: true, maxRetries: 3 });
  return true;
}

/** 起動のとき（エンジンを起動する前）: 削除の待ちを消し、途中で残った一時フォルダを片付ける。 */
export function cleanupAddons(dir) {
  const done = [];
  if (!dir || !fs.existsSync(dir)) return done;
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    if (name.startsWith('.trash-') || name.startsWith('.staging-')) {
      try { fs.rmSync(full, { recursive: true, force: true }); } catch { /* 使用中なら次の起動で */ }
    } else if (fs.existsSync(path.join(full, REMOVE_MARK))) {
      try { if (discard(dir, name)) done.push(name); } catch { /* 次の起動で */ }
    }
  }
  return done;
}

/** 削除。使用中なら印を置いて { pending: true }（エンジンはその印のあるアドオンを読まない）。 */
export function removeAddon(dir, id) {
  if (!dir) throw new Error('アドオンの置き場が無い');
  if (discard(dir, id)) return { pending: false };
  fs.writeFileSync(path.join(dir, id, REMOVE_MARK), `${new Date().toISOString()}\n`);
  return { pending: true };
}

/** zip を展開して確かめ、置き場の <id> と入れ替える。 */
export async function installAddonArchive(zip, entry, dir, signal) {
  fs.mkdirSync(dir, { recursive: true });
  const staging = path.join(dir, `.staging-${entry.id}-${tag()}`);
  try {
    await extractZip(zip, staging, signal);
    cancelled(signal);
    const m = JSON.parse(fs.readFileSync(path.join(staging, MANIFEST), 'utf8'));
    if (m.id !== entry.id) throw new Error(`アドオンの中身が違う（${m.id}）`);
    if (m.key !== entry.key) throw new Error(`アドオンの版が目録と違う（${m.key}）`);
    if (!fs.existsSync(path.join(staging, 'site-packages'))) throw new Error('アドオンに site-packages が無い');
    // 前のもの（合わなくなった版・削除の待ち）をどける。使用中ならあきらめる
    if (!discard(dir, entry.id)) {
      throw new Error('前のアドオンが使用中で入れ替えられません。Gliss を起動し直してから、もう一度ダウンロードしてください');
    }
    fs.renameSync(staging, path.join(dir, entry.id));
  } finally { fs.rmSync(staging, { recursive: true, force: true }); }
}

/** 取得 → 検証（サイズ・SHA-256。model-download.mjs の fetchArchive）→ 展開 → 入れ替え。 */
export async function downloadAddon({ net, entry, dir, signal, progress }) {
  if (!dir) throw new Error('アドオンの置き場が無い');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-addon-'));
  const total = entry.size;
  try {
    const zip = path.join(tmp, entry.file);
    progress({ id: entry.id, phase: 'downloading', bytes: 0, total });
    await fetchArchive(net, { ...entry, name: entry.title }, zip, signal,
      (bytes) => progress({ id: entry.id, phase: 'downloading', bytes, total }));
    cancelled(signal);
    progress({ id: entry.id, phase: 'extracting', bytes: total, total });
    await installAddonArchive(zip, entry, dir, signal);
    progress({ id: entry.id, phase: 'done', bytes: total, total });
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
}
