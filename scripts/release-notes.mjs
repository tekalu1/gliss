// リリースノートの正本は releases/<version>.json（title・date・sections[].items[]）。ここから 2 つを作る。
//
//   node scripts/release-notes.mjs                         app/release-info.json（アプリ内の「更新の内容」）を書く
//   node scripts/release-notes.mjs --markdown <file>       あわせて Release の本文（Markdown）を書く（CI の gh release create --notes-file）
//
// app/release-info.json は生成物（.gitignore）。pnpm dist / dist:dir が electron-builder の前に作り、app.asar に入る。
// `--generate-notes` は使わない（docs/release-plan.md §3）。
//
// 正本の形:
//   { "version": "0.1.0-beta.1", "title": "…", "date": "2026-10-02",
//     "sections": [{ "title": "できること", "items": ["…", "…"] }] }
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const RELEASES_DIR = path.join(ROOT, 'releases');
export const RELEASE_INFO = path.join(ROOT, 'app', 'release-info.json');

// 配布する版の形（先行版は beta だけ）。タグはこれに v を付けたもの
export const RELEASE_VERSION = /^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$/;

/** 版の大小（-1 / 0 / 1）。RELEASE_VERSION の形だけを扱う（先行版は同じ数字の正式版より前）。 */
export function compareVersions(a, b) {
  const pa = RELEASE_VERSION.exec(a);
  const pb = RELEASE_VERSION.exec(b);
  if (!pa || !pb) throw new Error(`版の形が違う: ${pa ? b : a}`);
  for (let i = 1; i <= 3; i++) {
    const d = Number(pa[i]) - Number(pb[i]);
    if (d) return Math.sign(d);
  }
  if (pa[4] === undefined || pb[4] === undefined) {
    return pa[4] === pb[4] ? 0 : pa[4] === undefined ? 1 : -1;
  }
  return Math.sign(Number(pa[4]) - Number(pb[4]));
}

/** 1 つの正本の中身の検査。問題の文の配列（無ければ空）。 */
export function releaseProblems(r, file = '') {
  const where = file ? `${file}: ` : '';
  const out = [];
  if (!r || typeof r !== 'object') return [`${where}JSON のオブジェクトではない`];
  if (!RELEASE_VERSION.test(String(r.version))) out.push(`${where}version が 0.1.0 か 0.1.0-beta.1 の形ではない: ${r.version}`);
  if (file && r.version && path.basename(file, '.json') !== r.version) out.push(`${where}ファイル名と version が違う`);
  if (typeof r.title !== 'string' || !r.title.trim()) out.push(`${where}title が無い`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(r.date))) out.push(`${where}date が YYYY-MM-DD ではない: ${r.date}`);
  if (!Array.isArray(r.sections) || !r.sections.length) out.push(`${where}sections が無い`);
  else {
    r.sections.forEach((s, i) => {
      if (typeof s?.title !== 'string' || !s.title.trim()) out.push(`${where}sections[${i}].title が無い`);
      if (!Array.isArray(s?.items) || !s.items.length || !s.items.every((x) => typeof x === 'string' && x.trim())) {
        out.push(`${where}sections[${i}].items が空か、文字列でないものがある`);
      }
    });
  }
  return out;
}

/** releases/ の正本をすべて読む（新しい順）。形が違うものがあれば例外。 */
export function readReleases(dir = RELEASES_DIR) {
  const files = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith('.json')) : [];
  const releases = [];
  const problems = [];
  for (const f of files) {
    let r;
    try {
      r = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
    } catch (e) {
      problems.push(`${f}: JSON として読めない（${e.message}）`);
      continue;
    }
    const p = releaseProblems(r, f);
    if (p.length) problems.push(...p);
    else releases.push({ version: r.version, title: r.title, date: r.date, sections: r.sections });
  }
  if (problems.length) throw new Error(`リリースノートの正本に問題がある:\n  ${problems.join('\n  ')}`);
  return releases.sort((a, b) => compareVersions(b.version, a.version));
}

/** Release の本文（Markdown）。見出しは版、その下に日付と題、節ごとの箇条書き。 */
export function releaseMarkdown(r) {
  const body = r.sections.map((s) => `## ${s.title}\n\n${s.items.map((i) => `- ${i}`).join('\n')}`).join('\n\n');
  return `# Gliss ${r.version}\n\n${r.date} · ${r.title}\n\n${body}\n`;
}

/** アプリ内の表示用（main が読んで画面に渡す）。いまの版と、それまでの版のノート（新しい順）。 */
export function releaseInfo(version, releases) {
  return { version, releases: releases.filter((r) => compareVersions(r.version, version) <= 0) };
}

export function appVersion() {
  return JSON.parse(fs.readFileSync(path.join(ROOT, 'app', 'package.json'), 'utf8')).version;
}

function main() {
  const i = process.argv.indexOf('--markdown');
  const markdown = i > 0 ? process.argv[i + 1] : null;
  if (i > 0 && !markdown) throw new Error('--markdown の後に書き出す先のファイルを書く');
  const version = appVersion();
  const releases = readReleases();
  const current = releases.find((r) => r.version === version);
  if (!current) throw new Error(`releases/${version}.json が無い（app/package.json の版のリリースノート）`);
  fs.writeFileSync(RELEASE_INFO, JSON.stringify(releaseInfo(version, releases), null, 2) + '\n', 'utf8');
  console.log(`書いた: ${path.relative(ROOT, RELEASE_INFO)}（${version}）`);
  if (markdown) {
    fs.mkdirSync(path.dirname(path.resolve(markdown)), { recursive: true });
    fs.writeFileSync(markdown, releaseMarkdown(current), 'utf8');
    console.log(`書いた: ${markdown}`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(); } catch (e) { console.error(`失敗: ${e.message}`); process.exit(1); }
}
