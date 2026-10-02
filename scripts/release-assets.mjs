// electron-builder の出力から、GitHub Release に載せる成果物を 1 つのフォルダにそろえて確かめる（CI の release.yml）。
//
//   node scripts/release-assets.mjs --dist app/dist --out <フォルダ> --version 0.1.0-beta.1 [--extra <ファイルかフォルダ> ...]
//                                   [--addons engine/packaging/dist-addons --catalog app/addons-catalog.json]
//
// そろえるもの: インストーラ（Gliss-<版>-win-x64.exe）・.blockmap・latest.yml・--extra（THIRD_PARTY_NOTICES.txt・
// copyleft のパッケージの sdist）・任意のアドオンの zip（--addons。Gliss-addon-<id>-<key>.zip）・SHA256SUMS.txt
// （ここで作る。上のすべての SHA-256）。
// 確かめること: latest.yml の版が --version と同じ・latest.yml が指すファイルがあり SHA-512 と大きさが合う・
// 余計なインストーラ（別の版）が混ざっていない・アドオンの zip がアプリに埋めた目録（--catalog）の大きさと SHA-256 に合う
// （アプリはこの版の Release から目録の名前で取得して照らすので、食い違うと取得できない）。
import crypto from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function loadYaml(text) {
  const appRequire = createRequire(path.join(ROOT, 'app', 'package.json'));
  const yaml = appRequire(appRequire.resolve('js-yaml', { paths: [path.dirname(appRequire.resolve('electron-updater/package.json'))] }));
  return yaml.load(text);
}

const digest = (algo, file, enc) => crypto.createHash(algo).update(fs.readFileSync(file)).digest(enc);

/** 成果物を out にそろえる。返り値は載せるファイル名の一覧。 */
/** アプリに埋めた目録（addons-catalog.json）の zip を、作ったもの（dist-addons）から探して照らす。 */
export function addonAssets({ addons, catalog }) {
  const list = JSON.parse(fs.readFileSync(catalog, 'utf8')).addons || [];
  if (!list.length) throw new Error(`${catalog} にアドオンが無い`);
  return list.map((a) => {
    const file = path.join(addons, a.file);
    if (path.basename(a.file) !== a.file || !/^Gliss-addon-[a-z0-9-]+-[0-9a-z]+\.zip$/.test(a.file)) throw new Error(`アドオンの名前が変: ${a.file}`);
    if (!fs.existsSync(file)) throw new Error(`${a.file} が無い（scripts/build-addon.mjs）`);
    if (fs.statSync(file).size !== a.size) throw new Error(`${a.file} の大きさが目録と違う`);
    if (digest('sha256', file, 'hex') !== a.sha256) throw new Error(`${a.file} の SHA-256 が目録と違う`);
    return file;
  });
}

export function prepareAssets({ dist, out, version, extra = [], addons = null, catalog = null, parseYaml = loadYaml }) {
  const installer = `Gliss-${version}-win-x64.exe`;
  const latestFile = path.join(dist, 'latest.yml');
  if (!fs.existsSync(latestFile)) throw new Error(`${latestFile} が無い（electron-builder の publish の設定を確かめる）`);
  const latest = parseYaml(fs.readFileSync(latestFile, 'utf8'));
  if (latest.version !== version) throw new Error(`latest.yml の版 ${latest.version} が ${version} と違う`);
  if (!latest.files?.length) throw new Error('latest.yml に files が無い');
  for (const f of latest.files) {
    if (path.basename(f.url) !== f.url) throw new Error(`latest.yml のファイル名が変: ${f.url}`);
    const file = path.join(dist, f.url);
    if (!fs.existsSync(file)) throw new Error(`latest.yml が指す ${f.url} が無い`);
    if (digest('sha512', file, 'base64') !== f.sha512) throw new Error(`${f.url} の SHA-512 が latest.yml と違う`);
    if (f.size !== undefined && fs.statSync(file).size !== f.size) throw new Error(`${f.url} の大きさが latest.yml と違う`);
  }
  if (latest.path !== installer || !latest.files.some((f) => f.url === installer)) {
    throw new Error(`latest.yml が ${installer} を指していない（${latest.path}）`);
  }
  const others = fs.readdirSync(dist).filter((n) => /^Gliss-.*\.exe$/.test(n) && n !== installer && !n.includes('__uninstaller'));
  if (others.length) throw new Error(`別の版のインストーラが混ざっている: ${others.join(', ')}（dist を消してから作り直す）`);
  const blockmap = `${installer}.blockmap`;
  if (!fs.existsSync(path.join(dist, blockmap))) throw new Error(`${blockmap} が無い`);

  fs.mkdirSync(out, { recursive: true });
  const names = [];
  const put = (src) => {
    const name = path.basename(src);
    if (names.includes(name)) throw new Error(`同じ名前のファイルが 2 つある: ${name}`);
    fs.copyFileSync(src, path.join(out, name));
    names.push(name);
  };
  for (const n of [installer, blockmap, 'latest.yml']) put(path.join(dist, n));
  if (addons) for (const f of addonAssets({ addons, catalog: catalog || path.join(ROOT, 'app', 'addons-catalog.json') })) put(f);
  for (const e of extra) {
    if (!fs.existsSync(e)) throw new Error(`${e} が無い`);
    if (fs.statSync(e).isDirectory()) for (const n of fs.readdirSync(e).sort()) put(path.join(e, n));
    else put(e);
  }
  const sums = names.slice().sort().map((n) => `${digest('sha256', path.join(out, n), 'hex')}  ${n}`);
  fs.writeFileSync(path.join(out, 'SHA256SUMS.txt'), sums.join('\n') + '\n', 'utf8');
  names.push('SHA256SUMS.txt');
  return names;
}

function main() {
  const argv = process.argv.slice(2);
  const opt = (k) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : null; };
  const extra = argv.flatMap((a, i) => (a === '--extra' ? [path.resolve(argv[i + 1])] : []));
  if (!opt('--dist') || !opt('--out') || !opt('--version')) throw new Error('--dist・--out・--version を付ける');
  const names = prepareAssets({ dist: path.resolve(opt('--dist')), out: path.resolve(opt('--out')), version: opt('--version'), extra,
    addons: opt('--addons') ? path.resolve(opt('--addons')) : null, catalog: opt('--catalog') ? path.resolve(opt('--catalog')) : null });
  console.log(`そろえた（${names.length} 個）:\n  ${names.join('\n  ')}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
