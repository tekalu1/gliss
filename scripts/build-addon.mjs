// 任意機能のアドオン（配布版のエンジン exe に入れていない Python の依存）を zip にする。
//
//   node scripts/build-addon.mjs <id>          engine/packaging/addons/<id>/requirements.txt（固定）から作る
//   node scripts/build-addon.mjs --all         全部のアドオンを作る
//   node scripts/build-addon.mjs <id> --lock   requirements.in を、エンジン exe の固定（requirements-exe.txt）を制約にして解き直す
//
// 出力: engine/packaging/dist-addons/Gliss-addon-<id>-<key>.zip と <id>.json（画面に埋める目録の 1 項目。
//       scripts/addon-catalog.mjs が app/addons-catalog.json にまとめる）・<id>.LICENSES.txt（THIRD_PARTY_NOTICES に載せる）。
// zip の直下:
//   gliss-addon.json  manifest（id・互換キー・互換の条件。エンジンの vocal_engine/addons.py が読む）
//   LICENSES.txt      入れたパッケージと、パッケージに組み込まれたもの（Open JTalk など）のライセンス文
//   site-packages/    exe に**無い**パッケージだけ（wheel を展開したもの。Python 3.13・win_amd64）
// 互換キー（key）: Python の版（ABI）と OS、exe と共有するパッケージ（numpy など。requires）の版、アドオン自身のパッケージの版の要約。
// exe の側の関係する依存が変わらなければ、アプリを更新してもキーは変わらない（利用者は取り直さなくてよい）。
// エンジン exe（engine/packaging/dist/vocal-engine）があれば、その exe にアドオンを読ませて import できるかも確かめる。
// 前提: uv が PATH にあること（build-engine.mjs と同じ）。
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sitePackages } from './third-party-notices.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGING = path.join(ROOT, 'engine', 'packaging');
export const ADDONS_SRC = path.join(PACKAGING, 'addons');
export const ADDONS_DIST = path.join(PACKAGING, 'dist-addons');
const EXE_LOCK = path.join(PACKAGING, 'requirements-exe.txt');
const EXE = path.join(PACKAGING, 'dist', 'vocal-engine', 'vocal-engine.exe');
const PYTHON_VERSION = '3.13';
export const PYTHON_TAG = `cp${PYTHON_VERSION.replace('.', '')}`;
export const PLATFORM_TAG = 'win_amd64';
const UV_PLATFORM = 'x86_64-pc-windows-msvc';
export const FORMAT = 1;

/** PEP 503 の正規化（numpy・NumPy・pydantic_core → pydantic-core）。 */
export const normalize = (name) => name.toLowerCase().replace(/[-_.]+/g, '-');

/** uv pip compile の出力（ハッシュ付き）→ Map<正規化した名前, { name, version, block }>。 */
export function parseLock(text) {
  const out = new Map();
  let cur = null;
  for (const line of text.replace(/\r\n/g, '\n').split('\n')) {
    const m = /^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)/.exec(line);
    if (m) {
      cur = { name: m[1], version: m[2], lines: [line] };
      out.set(normalize(m[1]), cur);
    } else if (cur && /^\s+--hash=/.test(line)) cur.lines.push(line);
    else cur = null;
  }
  for (const v of out.values()) { v.block = v.lines.join('\n').replace(/\s*\\$/, ''); delete v.lines; }
  return out;
}

/** 互換キー。同じ条件なら同じ文字列（並びに依らない）。 */
export function addonKey({ python, platform, requires, packages }) {
  const sorted = (o) => Object.fromEntries(Object.entries(o).sort(([a], [b]) => a.localeCompare(b)));
  const text = JSON.stringify({ format: FORMAT, python, platform, requires: sorted(requires), packages: sorted(packages) });
  return crypto.createHash('sha256').update(text).digest('hex').slice(0, 12);
}

/** アドオンの固定と exe の固定から、manifest の互換の条件を作る。exe と版が食い違えば止める。 */
export function planAddon(def, addonLock, exeLock) {
  const requires = {};
  const packages = {};
  const install = [];
  for (const [key, p] of addonLock) {
    const shared = exeLock.get(key);
    if (shared) {
      if (shared.version !== p.version) {
        throw new Error(`${def.id}: ${p.name} の版が exe（${shared.version}）と違う（${p.version}）。node scripts/build-addon.mjs ${def.id} --lock で作り直す`);
      }
      requires[key] = p.version;
    } else {
      packages[key] = p.version;
      install.push(p.block);
    }
  }
  for (const name of def.uses || []) {
    const shared = exeLock.get(normalize(name));
    if (!shared) throw new Error(`${def.id}: uses の ${name} が exe の依存に無い`);
    requires[normalize(name)] = shared.version;
  }
  if (!install.length) throw new Error(`${def.id}: exe に無いパッケージが 1 つも無い`);
  const cond = { python: PYTHON_TAG, platform: PLATFORM_TAG, requires, packages };
  return { ...cond, key: addonKey(cond), install };
}

function run(cmd, args, opt = {}) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, { stdio: 'inherit', cwd: PACKAGING, ...opt });
  if (r.error) throw new Error(`${cmd} を起動できない: ${r.error.message}`);
  if (r.status !== 0) throw new Error(`${cmd} が終了コード ${r.status} で失敗した`);
  return r;
}

function readDef(id) {
  const dir = path.join(ADDONS_SRC, id);
  const def = JSON.parse(fs.readFileSync(path.join(dir, 'addon.json'), 'utf8'));
  if (def.id !== id) throw new Error(`${dir}/addon.json の id が ${def.id}`);
  return { def, dir };
}

function lock(id) {
  const { dir } = readDef(id);
  const exeLock = parseLock(fs.readFileSync(EXE_LOCK, 'utf8'));
  const constraints = path.join(dir, 'constraints-exe.txt');
  fs.writeFileSync(constraints, [...exeLock.values()].map((p) => `${p.name}==${p.version}`).join('\n') + '\n');
  try {
    run('uv', ['pip', 'compile', 'requirements.in', '-c', 'constraints-exe.txt', '--python-version', PYTHON_VERSION,
      '--python-platform', 'windows', '--generate-hashes', '--no-annotate',
      '--custom-compile-command', `node scripts/build-addon.mjs ${id} --lock`, '-o', 'requirements.txt'], { cwd: dir });
  } finally { fs.rmSync(constraints, { force: true }); }
}

function dirStats(dir) {
  let bytes = 0; let files = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
    if (!e.isFile()) continue;
    files += 1;
    bytes += fs.statSync(path.join(e.parentPath, e.name)).size;
  }
  return { bytes, files };
}

/** LICENSES.txt: 入れたパッケージの dist-info のライセンス文＋ addon.json の licenses（組み込まれたもの）。 */
function licensesText(def, dir, site, plan) {
  const lines = [
    `Gliss のアドオン「${def.title}」（${def.id}・${plan.key}）に含まれるソフトウェアのライセンス`,
    '',
    'このアドオンは Gliss（GPL-3.0-or-later）のエンジンが読み込む Python のパッケージをまとめたもの。',
    '',
  ];
  const sep = (title) => lines.push('-'.repeat(78), title, '-'.repeat(78), '');
  for (const p of sitePackages(site)) {
    sep(`${p.name} ${p.version}（${p.license}）`);
    if (!p.texts.length) lines.push('（パッケージにライセンスのファイルが含まれていない。上のライセンス名を参照）', '');
    for (const t of p.texts) lines.push(`[${t.file}]`, t.text.replace(/\r\n/g, '\n').trimEnd(), '');
  }
  for (const l of def.licenses || []) {
    const file = l.file ? path.join(dir, l.file) : path.join(site, l.site);
    sep(`${l.name}（${l.license}）${l.note ? ` — ${l.note}` : ''}`);
    lines.push(fs.readFileSync(file, 'utf8').replace(/\r\n/g, '\n').trimEnd(), '');
  }
  return lines.join('\n');
}

/** 組み上げたアドオンを、固めたエンジン exe に読ませて確かめる（exe が無ければ飛ばす）。 */
function checkWithExe(addonsDir, def) {
  if (!fs.existsSync(EXE)) { console.log(`（${EXE} が無いので exe での確認は飛ばす）`); return null; }
  console.log(`> ${EXE} --check（GLISS_ADDONS_DIR=${addonsDir}）`);
  const r = spawnSync(EXE, ['--check'], { encoding: 'utf8', maxBuffer: 16 << 20, timeout: 180000,
    env: { ...process.env, GLISS_ADDONS_DIR: addonsDir, PYTHONIOENCODING: 'utf-8' } });
  if (r.status !== 0) throw new Error(`--check が終了コード ${r.status}:\n${r.stderr}`);
  const info = JSON.parse(r.stdout);
  const st = (info.addons || []).find((a) => a.id === def.id);
  if (!st?.active) throw new Error(`exe がアドオンを読まなかった: ${JSON.stringify(st)}`);
  const bad = Object.entries(info.addon_imports || {}).filter(([, v]) => String(v).startsWith('ERROR'));
  if (bad.length) throw new Error(`exe でアドオンの import に失敗した: ${bad.map(([k, v]) => `${k} ${v}`).join(', ')}`);
  console.log(`  exe で読めた: ${JSON.stringify(info.addon_imports)}${info.addon_probe ? ` probe ${JSON.stringify(info.addon_probe)}` : ''}`);
  return info;
}

export function build(id) {
  if (process.platform !== 'win32') throw new Error('アドオンは Windows（win_amd64）向けだけ。Windows で作る');
  const { def, dir } = readDef(id);
  const exeLock = parseLock(fs.readFileSync(EXE_LOCK, 'utf8'));
  const addonLock = parseLock(fs.readFileSync(path.join(dir, 'requirements.txt'), 'utf8'));
  const plan = planAddon(def, addonLock, exeLock);
  console.log(`${id}: key ${plan.key}・入れる ${Object.keys(plan.packages).join(', ')}・exe と共有 ${Object.keys(plan.requires).join(', ')}`);

  // build/addons/<id>/ を GLISS_ADDONS_DIR にすれば、そのまま exe に読ませられる形で組む
  const work = path.join(PACKAGING, 'build', 'addons');
  const stage = path.join(work, id);
  fs.rmSync(stage, { recursive: true, force: true });
  const site = path.join(stage, 'site-packages');
  fs.mkdirSync(site, { recursive: true });
  const req = path.join(work, `${id}.requirements.txt`);
  fs.writeFileSync(req, plan.install.join('\n') + '\n');
  run('uv', ['pip', 'install', '--target', site, '--python', PYTHON_VERSION, '--python-platform', UV_PLATFORM,
    '--no-deps', '--require-hashes', '--only-binary', ':all:', '--no-config', '-r', req]);
  // コマンドの入口（bin/・Scripts/）と __pycache__ は要らない
  for (const d of ['bin', 'Scripts']) fs.rmSync(path.join(site, d), { recursive: true, force: true });
  for (const e of fs.readdirSync(site, { withFileTypes: true, recursive: true })) {
    if (e.isDirectory() && e.name === '__pycache__') fs.rmSync(path.join(e.parentPath, e.name), { recursive: true, force: true });
  }
  // exe と同じものを二重に持たない（制約で解いているので起きないはずだが、念のため dist-info で確かめる）
  const installed = sitePackages(site).map((p) => normalize(p.name));
  const dup = installed.filter((n) => plan.requires[n] || exeLock.has(n));
  if (dup.length) throw new Error(`exe に入っているパッケージがアドオンに入った: ${dup.join(', ')}`);
  const missing = Object.keys(plan.packages).filter((n) => !installed.includes(n));
  if (missing.length) throw new Error(`入っていない: ${missing.join(', ')}`);

  const manifest = {
    format: FORMAT, id, title: def.title, key: plan.key,
    python: plan.python, platform: plan.platform, requires: plan.requires, packages: plan.packages,
    modules: def.modules,
  };
  fs.writeFileSync(path.join(stage, 'gliss-addon.json'), JSON.stringify(manifest, null, 1) + '\n');
  const licenses = licensesText(def, dir, site, plan);
  fs.writeFileSync(path.join(stage, 'LICENSES.txt'), licenses);
  const { bytes, files } = dirStats(stage);
  checkWithExe(work, def);

  fs.mkdirSync(ADDONS_DIST, { recursive: true });
  const file = `Gliss-addon-${id}-${plan.key}.zip`;
  const zip = path.join(ADDONS_DIST, file);
  for (const old of fs.readdirSync(ADDONS_DIST).filter((n) => n.startsWith(`Gliss-addon-${id}-`))) fs.rmSync(path.join(ADDONS_DIST, old));
  run('uv', ['run', '--no-project', '--python', PYTHON_VERSION, '--', 'python', path.join(PACKAGING, 'addon_zip.py'), stage, zip]);
  const data = fs.readFileSync(zip);
  const entry = {
    id, title: def.title, description: def.description, key: plan.key, file,
    size: data.length, sha256: crypto.createHash('sha256').update(data).digest('hex'),
    installedSize: bytes, files, packages: plan.packages,
    licenses: [...sitePackages(site).map((p) => ({ name: p.name, license: p.license })),
      ...(def.licenses || []).map((l) => ({ name: l.name, license: l.license }))],
  };
  fs.writeFileSync(path.join(ADDONS_DIST, `${id}.json`), JSON.stringify(entry, null, 1) + '\n');
  // THIRD_PARTY_NOTICES（scripts/third-party-notices.mjs）にも載せる
  fs.writeFileSync(path.join(ADDONS_DIST, `${id}.LICENSES.txt`), licenses);
  console.log(`できた: ${zip}`);
  console.log(`  zip ${(entry.size / 1048576).toFixed(1)} MB（展開後 ${(bytes / 1048576).toFixed(0)} MB・${files} ファイル）・SHA-256 ${entry.sha256}`);
  return entry;
}

export function addonIds() {
  return fs.readdirSync(ADDONS_SRC, { withFileTypes: true })
    .filter((e) => e.isDirectory() && fs.existsSync(path.join(ADDONS_SRC, e.name, 'addon.json'))).map((e) => e.name).sort();
}

function main() {
  const args = process.argv.slice(2);
  const ids = args.includes('--all') ? addonIds() : args.filter((a) => !a.startsWith('--'));
  if (!ids.length) throw new Error(`アドオンの id を渡す（${addonIds().join(' / ')}）か --all`);
  for (const id of ids) {
    if (args.includes('--lock')) lock(id);
    else build(id);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
