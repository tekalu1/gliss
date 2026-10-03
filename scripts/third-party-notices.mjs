// 配布物に入れたサードパーティのソフトウェアのライセンス文を 1 つにまとめる（THIRD_PARTY_NOTICES.txt）。
//
//   node scripts/third-party-notices.mjs                 app/THIRD_PARTY_NOTICES.txt を書く（生成物。.gitignore）
//   node scripts/third-party-notices.mjs --out <file>
//
// 集めるもの:
//   - エンジン exe（PyInstaller）に入る Python のパッケージ: エンジン exe を作った venv（.venv-exe。scripts/build-engine.mjs）の
//     *.dist-info（METADATA の License-Expression / License / 分類と、ライセンスのファイル）。Python 本体（PSF）も
//   - 画面（app.asar）に入る Node のパッケージ: app/package.json の dependencies から辿れるもの（devDependencies は入らない）
//   - エンジン exe に入る Gliss の F0 モデル（試作）の学習コード・学習データの出典（engine/vocal_engine/analysis/models/gliss-f0.NOTICE.txt）
//   - 任意のアドオン（scripts/build-addon.mjs が作った engine/packaging/dist-addons/<id>.LICENSES.txt。作っていれば）
// Electron・Chromium のライセンス文（LICENSE.electron.txt・LICENSES.chromium.html）は electron-builder がインストール先の直下に置く。
// electron-builder.yml の extraResources が resources/THIRD_PARTY_NOTICES.txt に入れる。
// copyleft のパッケージ（GPL・LGPL・MPL）のソースは scripts/gpl-sources.mjs が Release に添付する。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const EXE_VENV = process.env.GLISS_EXE_VENV ? path.resolve(process.env.GLISS_EXE_VENV) : path.join(ROOT, '.venv-exe');
const LICENSE_FILE = /^(licen[cs]e|copying|notice|copyright)/i;

/** METADATA（RFC 822 風のヘッダー。続きの行は空白で始まる）の先頭のヘッダー。 */
export function parseMetadata(text) {
  const head = text.split(/\r?\n\r?\n/)[0];
  const out = {};
  let last = null;
  for (const line of head.split(/\r?\n/)) {
    if (/^\s/.test(line) && last) { out[last][out[last].length - 1] += `\n${line.trim()}`; continue; }
    const m = /^([A-Za-z-]+):\s?(.*)$/.exec(line);
    if (!m) continue;
    last = m[1].toLowerCase();
    (out[last] ||= []).push(m[2]);
  }
  return out;
}

/** ライセンスの短い名前（License-Expression → License の 1 行目 → 分類）。 */
export function licenseName(meta) {
  const expr = meta['license-expression']?.[0]?.trim();
  if (expr) return expr;
  const classifiers = (meta.classifier || []).filter((c) => c.startsWith('License ::')).map((c) => c.split('::').pop().trim());
  const lic = (meta.license?.[0] || '').split('\n')[0].trim();
  // License に全文を入れているパッケージがある（1 行目が長い・記号だけ）。そのときは分類を使う
  if (lic && lic.length <= 60 && /[A-Za-z]/.test(lic) && !/^copyright/i.test(lic)) return lic;
  return classifiers.join(' / ') || lic.slice(0, 60) || '不明';
}

/** copyleft（ソースの提供が要る）か。PyInstaller はブートローダーの例外付き（同梱したアプリのライセンスを縛らない）。 */
export function isCopyleft(name, license) {
  if (/^pyinstaller/i.test(name)) return false;
  return /\b(L?GPL|MPL)|General Public License|Mozilla Public License/i.test(license);
}

function filesUnder(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
    if (e.isFile()) out.push(path.join(e.parentPath, e.name));
  }
  return out.sort();
}

/** venv（.venv-exe）の Python のパッケージ。[{ name, version, license, copyleft, texts: [{ file, text }] }] */
export function pythonPackages(venv = EXE_VENV) {
  const sp = [path.join(venv, 'Lib', 'site-packages'), ...(fs.existsSync(path.join(venv, 'lib'))
    ? fs.readdirSync(path.join(venv, 'lib')).map((d) => path.join(venv, 'lib', d, 'site-packages')) : [])].find((d) => fs.existsSync(d));
  if (!sp) throw new Error(`${venv} に site-packages が無い（node scripts/build-engine.mjs で作る）`);
  return sitePackages(sp);
}

/** site-packages のフォルダ（venv の中・アドオンの中）にある *.dist-info のパッケージ。 */
export function sitePackages(sp) {
  const out = [];
  for (const d of fs.readdirSync(sp).filter((n) => n.endsWith('.dist-info')).sort()) {
    const dir = path.join(sp, d);
    const meta = parseMetadata(fs.readFileSync(path.join(dir, 'METADATA'), 'utf8'));
    const name = meta.name?.[0];
    if (!name || /^(pip|uv)$/i.test(name)) continue;
    const license = licenseName(meta);
    const texts = filesUnder(dir).filter((f) => LICENSE_FILE.test(path.basename(f)) || path.relative(dir, f).startsWith(`licenses${path.sep}`))
      .map((f) => ({ file: path.relative(dir, f).replaceAll('\\', '/'), text: fs.readFileSync(f, 'utf8') }));
    // License に全文を入れていてファイルが無いもの
    if (!texts.length && (meta.license?.[0] || '').length > 200) texts.push({ file: 'METADATA の License', text: meta.license[0] });
    out.push({ name, version: meta.version?.[0], license, copyleft: isCopyleft(name, license), texts });
  }
  return out;
}

/** Python 本体（エンジン exe に python3xx.dll と標準ライブラリが入る）のライセンス文。 */
function pythonRuntime(venv = EXE_VENV) {
  const cfg = fs.readFileSync(path.join(venv, 'pyvenv.cfg'), 'utf8');
  const home = /^home\s*=\s*(.+)$/m.exec(cfg)?.[1]?.trim();
  const version = /^version(?:_info)?\s*=\s*(.+)$/m.exec(cfg)?.[1]?.trim();
  const file = [path.join(home || '', 'LICENSE.txt'), path.join(home || '', '..', 'LICENSE.txt')].find((f) => fs.existsSync(f));
  return { name: 'Python', version, license: 'PSF-2.0', copyleft: false, texts: file ? [{ file: 'LICENSE.txt', text: fs.readFileSync(file, 'utf8') }] : [] };
}

/** パッケージ dir から見た依存 dep の package.json のある場所（pnpm の .pnpm/<id>/node_modules も上へ辿って見つける）。 */
function findPackage(dep, fromDir) {
  for (let d = fromDir; ; d = path.dirname(d)) {
    const p = path.join(d, 'node_modules', dep, 'package.json');
    if (fs.existsSync(p)) return fs.realpathSync(path.dirname(p));
    if (path.dirname(d) === d) return null;
  }
}

/** app/package.json の dependencies から辿れる Node のパッケージ。 */
export function nodePackages(appDir = path.join(ROOT, 'app')) {
  const seen = new Map();
  const visit = (dir) => {
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    const key = `${pkg.name}@${pkg.version}`;
    if (seen.has(key)) return;
    const license = typeof pkg.license === 'string' ? pkg.license : pkg.license?.type
      || (Array.isArray(pkg.licenses) ? pkg.licenses.map((l) => l.type || l).join(' OR ') : '不明');
    const texts = fs.readdirSync(dir).filter((f) => LICENSE_FILE.test(f) && fs.statSync(path.join(dir, f)).isFile()).sort()
      .map((f) => ({ file: f, text: fs.readFileSync(path.join(dir, f), 'utf8') }));
    seen.set(key, { name: pkg.name, version: pkg.version, license, copyleft: isCopyleft(pkg.name, license), texts });
    for (const dep of Object.keys({ ...pkg.dependencies, ...pkg.optionalDependencies })) {
      const d = findPackage(dep, dir);
      if (d) visit(d);
      else if (!pkg.optionalDependencies?.[dep]) throw new Error(`${pkg.name} の依存 ${dep} が見つからない（pnpm install を先に）`);
    }
  };
  const app = JSON.parse(fs.readFileSync(path.join(appDir, 'package.json'), 'utf8'));
  for (const dep of Object.keys(app.dependencies || {})) {
    const d = findPackage(dep, appDir);
    if (!d) throw new Error(`app の依存 ${dep} が見つからない（pnpm install を先に）`);
    visit(d);
  }
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function section(title, list) {
  const lines = [`${'='.repeat(78)}`, title, `${'='.repeat(78)}`, ''];
  for (const p of list) {
    lines.push(`--- ${p.name} ${p.version || ''} (${p.license}) ${'-'.repeat(Math.max(3, 60 - p.name.length - String(p.version).length - p.license.length))}`);
    if (!p.texts.length) lines.push('（ライセンスのファイルが同梱されていない。上のライセンス名を参照）');
    for (const t of p.texts) {
      if (p.texts.length > 1) lines.push(`[${t.file}]`);
      lines.push(t.text.replace(/\r\n/g, '\n').trimEnd(), '');
    }
    lines.push('');
  }
  return lines.join('\n');
}

/** 作ったアドオン（engine/packaging/dist-addons/<id>.LICENSES.txt。scripts/build-addon.mjs）のライセンス文。 */
export function addonLicenses(dir = path.join(ROOT, 'engine', 'packaging', 'dist-addons')) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir).filter((n) => n.endsWith('.LICENSES.txt')).sort()
    .map((n) => ({ id: n.slice(0, -'.LICENSES.txt'.length), text: fs.readFileSync(path.join(dir, n), 'utf8') }));
}

/** Gliss の F0 モデル（試作）の出典の文書（モデルの隣に置いてある。CC BY・ODbL・CMU Arctic の表示を含む）。 */
export function f0ModelNotice(file = path.join(ROOT, 'engine', 'vocal_engine', 'analysis', 'models', 'gliss-f0.NOTICE.txt')) {
  return fs.readFileSync(file, 'utf8');
}

export function notices({ version, python, node, addons = [], f0Model = '' }) {
  const copyleft = [...python, ...node].filter((p) => p.copyleft).map((p) => `${p.name} ${p.version}（${p.license}）`);
  return [
    `Gliss ${version} に含まれるサードパーティのソフトウェアのライセンス`,
    '',
    'Gliss 自身は GPL-3.0-or-later（同じフォルダの LICENSE）。この文書は scripts/third-party-notices.mjs が作る。',
    'Electron と Chromium のライセンスは、インストール先の LICENSE.electron.txt と LICENSES.chromium.html にある。',
    copyleft.length ? `ソースの提供が要るもの（GitHub の Release に sdist を添付している）: ${copyleft.join('、')}` : '',
    '',
    section('エンジン（resources/engine/vocal-engine。Python と、PyInstaller で同梱したパッケージ）', python),
    section('画面（resources/app.asar。Node.js のパッケージ）', node),
    f0Model ? [`${'='.repeat(78)}`, 'エンジンに同梱した Gliss の F0 モデル（学習コードと学習データの出典）', `${'='.repeat(78)}`, '',
      `${f0Model.replace(/\r\n/g, '\n').trimEnd()}\n`].join('\n') : '',
    addons.length ? [`${'='.repeat(78)}`,
      '任意のアドオン（インストーラには入っていない。画面から取得したときだけ %LOCALAPPDATA%\\Gliss\\addons に入る。',
      '中身は各アドオンの LICENSES.txt と同じ）', `${'='.repeat(78)}`, '',
      ...addons.map((a) => `${a.text.replace(/\r\n/g, '\n').trimEnd()}\n`)].join('\n') : '',
  ].join('\n');
}

function main() {
  const i = process.argv.indexOf('--out');
  const out = i > 0 ? path.resolve(process.argv[i + 1]) : path.join(ROOT, 'app', 'THIRD_PARTY_NOTICES.txt');
  const version = JSON.parse(fs.readFileSync(path.join(ROOT, 'app', 'package.json'), 'utf8')).version;
  const python = [pythonRuntime(), ...pythonPackages()];
  const node = nodePackages();
  const addons = addonLicenses();
  fs.writeFileSync(out, notices({ version, python, node, addons, f0Model: f0ModelNotice() }), 'utf8');
  const missing = [...python, ...node].filter((p) => !p.texts.length).map((p) => p.name);
  console.log(`書いた: ${path.relative(ROOT, out)}（Python ${python.length}・Node ${node.length} パッケージ・アドオン ${addons.map((a) => a.id).join(', ') || 'なし'}）`);
  if (missing.length) console.log(`  ライセンスのファイルが無いもの（名前だけ載せた）: ${missing.join(', ')}`);
  const copyleft = [...python, ...node].filter((p) => p.copyleft);
  console.log(`  copyleft: ${copyleft.map((p) => `${p.name} ${p.version} (${p.license})`).join(', ') || 'なし'}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
