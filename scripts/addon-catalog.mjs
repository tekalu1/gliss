// アプリに埋めるアドオンの目録（app/addons-catalog.json。生成物）を、作ったアドオン（engine/packaging/dist-addons/<id>.json。
// scripts/build-addon.mjs）から作る。画面（app/addons.mjs）は、ここにある zip の名前・サイズ・SHA-256 で、
// アプリ自身の版の Release から取得して照らす。
//
//   node scripts/addon-catalog.mjs              app/addons-catalog.json を書く（pnpm prepack:files が electron-builder の前に呼ぶ）
//   node scripts/addon-catalog.mjs --require    アドオンが 1 つも無ければ止める（リリース。.github/workflows/release.yml）
//
// アドオンを作っていなければ空の目録になる（その版の画面は「この版には含まれていません」と出す）。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const ADDONS_DIST = path.join(ROOT, 'engine', 'packaging', 'dist-addons');
export const CATALOG = path.join(ROOT, 'app', 'addons-catalog.json');

/** dist-addons の <id>.json（と、その zip があること）。 */
export function builtAddons(dir = ADDONS_DIST) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir).filter((n) => /^[a-z0-9-]+\.json$/.test(n)).sort().map((n) => {
    const entry = JSON.parse(fs.readFileSync(path.join(dir, n), 'utf8'));
    const zip = path.join(dir, entry.file);
    if (!fs.existsSync(zip) || fs.statSync(zip).size !== entry.size) throw new Error(`${entry.file} が無いか、大きさが ${n} と違う`);
    return entry;
  });
}

function main() {
  const addons = builtAddons().map(({ id, title, description, key, file, size, sha256, installedSize, packages, licenses }) =>
    ({ id, title, description, key, file, size, sha256, installedSize, packages, licenses }));
  if (!addons.length && process.argv.includes('--require')) throw new Error(`${ADDONS_DIST} にアドオンが無い（node scripts/build-addon.mjs --all）`);
  fs.writeFileSync(CATALOG, JSON.stringify({ addons }, null, 1) + '\n');
  console.log(`書いた: ${path.relative(ROOT, CATALOG)}（${addons.map((a) => `${a.id} ${a.key}`).join('、') || 'アドオンなし'}）`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
