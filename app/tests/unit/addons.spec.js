// 任意機能のアドオン: 画面側（addons.mjs）と、作る側（scripts/build-addon.mjs・addon-catalog.mjs・third-party-notices.mjs）。
// Electron は起動しない。
//
//   (A1) 置き場: 配布版は %LOCALAPPDATA%\Gliss\addons、開発版は GLISS_ADDONS_DIR を渡したときだけ
//   (A2) 目録: URL はアプリ自身の版の Release（GLISS_ADDON_BASE_URL で差し替え）。形の悪い項目は捨てる
//   (A3) 行の状態: 無い・使える・合わない（更新が要る）・削除の待ち・開発環境・この版に無い・エンジン未接続
//   (A4) 展開して入れ替える: manifest の id・key を目録と照らす。前の版をどけ、一時フォルダを残さない
//   (A5) 削除: 使用中（Windows で .pyd を掴まれている）なら印を置いて次の起動で消す
//   (A6) 作る側: 固定の読み取り・exe と共有するものは入れない・版の食い違いで止める・互換キーは並びに依らない
//   (A7) いまのリポジトリのアドオンの固定が、エンジン exe の固定と食い違っていない
//   (A8) 目録とライセンス文: zip の大きさの照合・THIRD_PARTY_NOTICES のアドオンの節
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  ADDONS, MANIFEST, REMOVE_MARK, addonDirectory, addonStates, cleanupAddons, installAddonArchive, loadCatalog, removeAddon,
} from '../../addons.mjs';
import { addonKey, parseLock, planAddon } from '../../../scripts/build-addon.mjs';
import { builtAddons } from '../../../scripts/addon-catalog.mjs';
import { addonLicenses, notices } from '../../../scripts/third-party-notices.mjs';
import { storeZip } from './store-zip.js';

const ROOT = path.resolve(import.meta.dirname, '..', '..', '..');
const made = [];
const tmp = () => { const d = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-addon-test-')); made.push(d); return d; };
test.afterAll(() => { for (const d of made) fs.rmSync(d, { recursive: true, force: true }); });

const manifest = (o = {}) => JSON.stringify({ format: 1, id: 'lyrics-ja', key: 'k-new', modules: ['pyopenjtalk'], ...o });

test('(A1) 置き場', () => {
  expect(addonDirectory({ packaged: true, env: {}, localAppData: 'C:\\L' })).toBe(path.join('C:\\L', 'Gliss', 'addons'));
  expect(addonDirectory({ packaged: false, env: {}, localAppData: 'C:\\L' })).toBe(null);
  expect(addonDirectory({ packaged: false, env: { GLISS_ADDONS_DIR: 'D:\\x' } })).toBe(path.resolve('D:\\x'));
});

test('(A2) 目録', () => {
  const dir = tmp();
  const file = path.join(dir, 'addons-catalog.json');
  const good = { id: 'lyrics-ja', key: 'k1', file: 'Gliss-addon-lyrics-ja-k1.zip', size: 10, sha256: 'a'.repeat(64) };
  fs.writeFileSync(file, JSON.stringify({ addons: [good, { ...good, id: 'bad', sha256: 'xyz' }] }));
  const [a, ...rest] = loadCatalog({ file, env: {}, version: '0.1.0-beta.2' });
  expect(rest).toEqual([]);
  expect(a.url).toBe('https://github.com/tekalu1/gliss/releases/download/v0.1.0-beta.2/Gliss-addon-lyrics-ja-k1.zip');
  expect(loadCatalog({ file, env: { GLISS_ADDON_BASE_URL: 'http://127.0.0.1:9/a/' }, version: 'x' })[0].url)
    .toBe('http://127.0.0.1:9/a/Gliss-addon-lyrics-ja-k1.zip');
  expect(loadCatalog({ file: path.join(dir, 'none.json'), env: {}, version: 'x' })).toEqual([]);
});

test('(A3) 行の状態', () => {
  const dir = tmp();
  const catalog = [{ id: 'lyrics-ja', title: '漢字の歌詞の読み', size: 98e6, licenses: [{ name: 'x', license: 'MIT' }] }];
  const state = (o) => addonStates({ catalog, dir, ...o })[0].state;
  expect(ADDONS.map((a) => a.id)).toEqual(['lyrics-ja']);
  expect(state({ engine: { installed: [] } })).toBe('missing');
  expect(state({ catalog: [], engine: { installed: [] } })).toBe('unavailable');
  expect(addonStates({ catalog, dir: null, engine: null, g2pAvailable: true })[0].state).toBe('environment');
  expect(addonStates({ catalog, dir: null, engine: null, g2pAvailable: false })[0].state).toBe('unavailable');
  fs.mkdirSync(path.join(dir, 'lyrics-ja'));
  fs.writeFileSync(path.join(dir, 'lyrics-ja', MANIFEST), manifest({ key: 'k-old' }));
  expect(state({ engine: null })).toBe('unknown');
  expect(state({ engine: { installed: [{ id: 'lyrics-ja', active: true, compatible: true }] } })).toBe('ready');
  const bad = addonStates({ catalog, dir, engine: { installed: [{ id: 'lyrics-ja', active: false, compatible: false, reason: 'numpy' }] } })[0];
  expect(bad.state).toBe('incompatible');
  expect(bad.reason).toBe('numpy');
  expect(bad.key).toBe('k-old');
  expect(state({ engine: { installed: [{ id: 'lyrics-ja', source: 'environment', compatible: true }] } })).toBe('environment');
  fs.writeFileSync(path.join(dir, 'lyrics-ja', REMOVE_MARK), '');
  expect(state({ engine: { installed: [] } })).toBe('removing');
});

test('(A4) 展開して入れ替える', async () => {
  const dir = tmp();
  const zip = path.join(tmp(), 'a.zip');
  const entry = { id: 'lyrics-ja', key: 'k-new' };
  // 前の版（合わなくなったもの）
  fs.mkdirSync(path.join(dir, 'lyrics-ja', 'site-packages'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'lyrics-ja', MANIFEST), manifest({ key: 'k-old' }));
  fs.writeFileSync(path.join(dir, 'lyrics-ja', 'site-packages', 'old.py'), '');

  storeZip(zip, { [MANIFEST]: manifest({ key: 'k-other' }), 'site-packages/new.py': 'x = 1\n' });
  await expect(installAddonArchive(zip, entry, dir)).rejects.toThrow('目録と違う');
  storeZip(zip, { [MANIFEST]: manifest({ id: 'other' }), 'site-packages/new.py': '' });
  await expect(installAddonArchive(zip, entry, dir)).rejects.toThrow('中身が違う');
  // 失敗しても前の版はそのまま、一時フォルダは残らない
  expect(JSON.parse(fs.readFileSync(path.join(dir, 'lyrics-ja', MANIFEST), 'utf8')).key).toBe('k-old');
  expect(fs.readdirSync(dir)).toEqual(['lyrics-ja']);

  storeZip(zip, { [MANIFEST]: manifest(), 'LICENSES.txt': 'MIT', 'site-packages/new.py': 'x = 1\n' });
  await installAddonArchive(zip, entry, dir);
  expect(JSON.parse(fs.readFileSync(path.join(dir, 'lyrics-ja', MANIFEST), 'utf8')).key).toBe('k-new');
  expect(fs.existsSync(path.join(dir, 'lyrics-ja', 'site-packages', 'new.py'))).toBe(true);
  expect(fs.existsSync(path.join(dir, 'lyrics-ja', 'site-packages', 'old.py'))).toBe(false);
  expect(fs.readdirSync(dir)).toEqual(['lyrics-ja']);
});

test('(A5) 削除（使用中なら次の起動で）', () => {
  const dir = tmp();
  const make = () => {
    fs.mkdirSync(path.join(dir, 'lyrics-ja', 'site-packages'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'lyrics-ja', MANIFEST), manifest());
  };
  make();
  expect(removeAddon(dir, 'lyrics-ja')).toEqual({ pending: false });
  expect(fs.readdirSync(dir)).toEqual([]);

  make();
  const rename = fs.renameSync;
  fs.renameSync = () => { throw Object.assign(new Error('busy'), { code: 'EBUSY' }); };
  try {
    expect(removeAddon(dir, 'lyrics-ja')).toEqual({ pending: true });
  } finally { fs.renameSync = rename; }
  expect(fs.existsSync(path.join(dir, 'lyrics-ja', REMOVE_MARK))).toBe(true);
  // 次の起動（エンジンを起動する前）に消える。途中で残った一時フォルダも片付ける
  fs.mkdirSync(path.join(dir, '.staging-lyrics-ja-1'));
  fs.mkdirSync(path.join(dir, '.trash-lyrics-ja-2'));
  expect(cleanupAddons(dir)).toEqual(['lyrics-ja']);
  expect(fs.readdirSync(dir)).toEqual([]);
  expect(cleanupAddons(path.join(dir, 'none'))).toEqual([]);
  expect(cleanupAddons(null)).toEqual([]);
});

const LOCK = (rows) => rows.map(([n, v]) => `${n}==${v} \\\n    --hash=sha256:${'0'.repeat(64)}\n    # via x`).join('\n');

test('(A6) 作る側: 固定・共有・互換キー', () => {
  const exe = parseLock(LOCK([['numpy', '2.4.6'], ['pydantic_core', '2.46.5'], ['onnxruntime', '1.30.0']]));
  expect([...exe.keys()]).toEqual(['numpy', 'pydantic-core', 'onnxruntime']);
  expect(exe.get('numpy').block).toContain('--hash=sha256:');
  const def = { id: 'lyrics-ja', uses: ['onnxruntime'] };
  const plan = planAddon(def, parseLock(LOCK([['numpy', '2.4.6'], ['pydantic-core', '2.46.5'], ['pyopenjtalk-plus', '0.4.1.post9']])), exe);
  expect(plan.requires).toEqual({ numpy: '2.4.6', 'pydantic-core': '2.46.5', onnxruntime: '1.30.0' });
  expect(plan.packages).toEqual({ 'pyopenjtalk-plus': '0.4.1.post9' });
  expect(plan.install).toHaveLength(1);
  expect(plan.install[0]).toMatch(/^pyopenjtalk-plus==0\.4\.1\.post9 \\\n {4}--hash=sha256:0+$/);
  expect(plan.python).toBe('cp313');
  expect(plan.platform).toBe('win_amd64');
  // exe の numpy が上がったら止まる（作り直しを促す）
  expect(() => planAddon(def, parseLock(LOCK([['numpy', '2.4.5'], ['x', '1']])), exe)).toThrow('--lock で作り直す');
  // 互換キー: 並びに依らず、共有する版・自分の版が変わると変わる
  const cond = { python: 'cp313', platform: 'win_amd64', requires: { numpy: '2.4.6', a: '1' }, packages: { p: '1' } };
  expect(addonKey(cond)).toBe(addonKey({ ...cond, requires: { a: '1', numpy: '2.4.6' } }));
  expect(addonKey(cond)).not.toBe(addonKey({ ...cond, requires: { numpy: '2.4.7', a: '1' } }));
  expect(addonKey(cond)).not.toBe(addonKey({ ...cond, packages: { p: '2' } }));
  expect(addonKey(cond)).toMatch(/^[0-9a-f]{12}$/);
});

test('(A7) いまのアドオンの固定がエンジン exe の固定と合う', () => {
  const packaging = path.join(ROOT, 'engine', 'packaging');
  const exe = parseLock(fs.readFileSync(path.join(packaging, 'requirements-exe.txt'), 'utf8'));
  for (const id of fs.readdirSync(path.join(packaging, 'addons'))) {
    const def = JSON.parse(fs.readFileSync(path.join(packaging, 'addons', id, 'addon.json'), 'utf8'));
    const plan = planAddon(def, parseLock(fs.readFileSync(path.join(packaging, 'addons', id, 'requirements.txt'), 'utf8')), exe);
    expect(Object.keys(plan.packages).length).toBeGreaterThan(0);
    for (const l of def.licenses || []) if (l.file) expect(fs.existsSync(path.join(packaging, 'addons', id, l.file))).toBe(true);
    expect(def.modules.length).toBeGreaterThan(0);
  }
});

test('(A8) 目録とライセンス文', () => {
  const dir = tmp();
  fs.writeFileSync(path.join(dir, 'Gliss-addon-a-k.zip'), Buffer.alloc(5));
  fs.writeFileSync(path.join(dir, 'a.json'), JSON.stringify({ id: 'a', file: 'Gliss-addon-a-k.zip', size: 5 }));
  expect(builtAddons(dir).map((a) => a.id)).toEqual(['a']);
  fs.writeFileSync(path.join(dir, 'a.json'), JSON.stringify({ id: 'a', file: 'Gliss-addon-a-k.zip', size: 6 }));
  expect(() => builtAddons(dir)).toThrow('大きさ');
  expect(builtAddons(path.join(dir, 'none'))).toEqual([]);

  fs.writeFileSync(path.join(dir, 'a.LICENSES.txt'), 'pyopenjtalk MIT\r\nOpen JTalk BSD\r\n');
  const addons = addonLicenses(dir);
  expect(addons).toEqual([{ id: 'a', text: 'pyopenjtalk MIT\r\nOpen JTalk BSD\r\n' }]);
  const text = notices({ version: '0.1.0', python: [], node: [], addons });
  expect(text).toContain('任意のアドオン');
  expect(text).toContain('Open JTalk BSD');
  expect(notices({ version: '0.1.0', python: [], node: [] })).not.toContain('任意のアドオン');
});
