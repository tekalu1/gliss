// リリースの道具（scripts/release-notes.mjs・release-check.mjs・engine-processes.mjs）と、配布の設定の前提。
// Electron は起動しない。
//
//   (R1) 版の大小（beta は同じ数字の正式版より前・beta.9 < beta.11）
//   (R2) リリースノートの正本の形の検査
//   (R3) Release の本文（Markdown）とアプリ内の表示用 JSON
//   (R4) release-check: タグ・版・engine 側の写し・ノートの有無・直前と同じノート
//   (R5) いまのリポジトリが release-check を通る（版・ノートがそろっている）
//   (R6) electron-builder の NSIS の既定が、インストール先の下のプロセスをすべて止める（vocal-engine.exe を含む）
//   (R7) エンジンのプロセスを止める: 消えるまで待つ・止まらないものを返す
//   (R8) 成果物をそろえる（release-assets.mjs）: latest.yml の版・SHA-512・余計なインストーラ・アドオンの zip と目録・SHA256SUMS
//   (R9) 公開（release-publish.mjs）: 下書き → 公開、beta は prerelease で Latest にしない
//   (R10) THIRD_PARTY_NOTICES（third-party-notices.mjs）: METADATA・ライセンス名・copyleft の判定
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';

import { compareVersions, readReleases, releaseInfo, releaseMarkdown, releaseProblems } from '../../../scripts/release-notes.mjs';
import { releaseCheck } from '../../../scripts/release-check.mjs';
import { findEngineProcesses, stopEngineProcesses } from '../../engine-processes.mjs';

const require = createRequire(import.meta.url);
const note = (version, title = `題 ${version}`, items = ['a']) => ({ version, title, date: '2026-10-02', sections: [{ title: '変更', items }] });

test('(R1) 版の大小', () => {
  expect(compareVersions('0.1.0-beta.9', '0.1.0-beta.11')).toBe(-1);
  expect(compareVersions('0.1.0', '0.1.0-beta.11')).toBe(1);
  expect(compareVersions('0.2.0-beta.1', '0.1.9')).toBe(1);
  expect(compareVersions('1.0.0', '1.0.0')).toBe(0);
  expect(() => compareVersions('1.0', '1.0.0')).toThrow();
});

test('(R2) 正本の形の検査', () => {
  expect(releaseProblems(note('0.1.0-beta.1'), '0.1.0-beta.1.json')).toEqual([]);
  expect(releaseProblems(note('0.1.0-beta.1'), '0.1.0-beta.2.json').join()).toContain('ファイル名');
  expect(releaseProblems({ ...note('0.1.0-rc.1') }).join()).toContain('version');
  expect(releaseProblems({ ...note('0.1.0'), date: '10/2' }).join()).toContain('date');
  expect(releaseProblems({ ...note('0.1.0'), sections: [{ title: 'x', items: [] }] }).join()).toContain('items');
  expect(releaseProblems({ ...note('0.1.0'), title: ' ' }).join()).toContain('title');
});

test('(R3) Markdown とアプリ内の表示用', () => {
  const md = releaseMarkdown(note('0.1.0-beta.2', '直した', ['一つ目', '二つ目']));
  expect(md).toBe('# Gliss 0.1.0-beta.2\n\n2026-10-02 · 直した\n\n## 変更\n\n- 一つ目\n- 二つ目\n');
  const list = [note('0.1.0-beta.3'), note('0.1.0-beta.2'), note('0.1.0-beta.1')];
  const info = releaseInfo('0.1.0-beta.2', list);
  expect(info.version).toBe('0.1.0-beta.2');
  expect(info.releases.map((r) => r.version)).toEqual(['0.1.0-beta.2', '0.1.0-beta.1']);
});

test('(R4) release-check', () => {
  const releases = [note('0.1.0-beta.2', '新しい'), note('0.1.0-beta.1')];
  expect(releaseCheck({ tag: 'v0.1.0-beta.2', version: '0.1.0-beta.2', releases, stale: [] })).toEqual([]);
  expect(releaseCheck({ tag: 'v0.1.0-beta.1', version: '0.1.0-beta.2', releases, stale: [] }).join()).toContain('違う');
  expect(releaseCheck({ tag: '0.1.0-beta.2', version: '0.1.0-beta.2', releases, stale: [] }).join()).toContain('タグの形');
  expect(releaseCheck({ tag: null, version: '0.1.0-beta.2', releases, stale: ['engine/pyproject.toml'] }).join()).toContain('sync-version');
  expect(releaseCheck({ tag: null, version: '0.1.0-beta.3', releases, stale: [] }).join()).toContain('releases/0.1.0-beta.3.json が無い');
  expect(releaseCheck({ tag: null, version: '0.1', releases, stale: [] }).join()).toContain('形ではない');
  // 版だけ上げてノートを写した
  const copied = [{ ...note('0.1.0-beta.1'), version: '0.1.0-beta.2' }, note('0.1.0-beta.1')];
  expect(releaseCheck({ tag: null, version: '0.1.0-beta.2', releases: copied, stale: [] }).join()).toContain('直前の 0.1.0-beta.1 と同じ');
  // 正本の読み込み（形が違うファイルがあれば、どれかを示して止める）
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-releases-'));
  try {
    fs.writeFileSync(path.join(dir, '0.1.0-beta.1.json'), JSON.stringify(note('0.1.0-beta.1')));
    fs.writeFileSync(path.join(dir, '0.1.0-beta.10.json'), JSON.stringify(note('0.1.0-beta.10')));
    fs.writeFileSync(path.join(dir, '0.1.0-beta.9.json'), JSON.stringify(note('0.1.0-beta.9')));
    expect(readReleases(dir).map((r) => r.version)).toEqual(['0.1.0-beta.10', '0.1.0-beta.9', '0.1.0-beta.1']);
    fs.writeFileSync(path.join(dir, '0.1.0-beta.11.json'), '{');
    expect(() => readReleases(dir)).toThrow(/0\.1\.0-beta\.11\.json/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('(R5) いまのリポジトリは release-check を通る', () => {
  const version = JSON.parse(fs.readFileSync(new URL('../../package.json', import.meta.url), 'utf8')).version;
  expect(releaseCheck({ tag: `v${version}` })).toEqual([]);
});

test('(R6) NSIS の既定はインストール先の下のプロセスを止める。エンジンは installer.nsh が先に止める', () => {
  // electron-builder → app-builder-lib のテンプレート。既定に build/installer.nsh の customCheckAppRunning が一手足す前提（electron-builder.yml）
  const builder = path.dirname(require.resolve('electron-builder/package.json'));
  const lib = path.dirname(require.resolve('app-builder-lib/package.json', { paths: [builder] }));
  const nsh = fs.readFileSync(path.join(lib, 'templates', 'nsis', 'include', 'allowOnlyOneInstallerInstance.nsh'), 'utf8');
  // 探す: 実行ファイルのパスが $INSTDIR で始まるもの（名前ではない）
  expect(nsh).toMatch(/!macro FIND_PROCESS[\s\S]*?Get-CimInstance -ClassName Win32_Process \| \? \{\$\$_\.Path -and \$\$_\.Path\.StartsWith\('\$INSTDIR'/);
  // 止める: 同じ条件で Stop-Process
  expect(nsh).toMatch(/!macro KILL_PROCESS[\s\S]*?StartsWith\('\$INSTDIR'[^\n]*Stop-Process/);
  // CHECK_APP_RUNNING は customCheckAppRunning があればそれを使い、無ければ既定。customCheckAppRunning があると
  // getProcessInfo.nsh と Var pid（既定の _CHECK_APP_RUNNING が使う）をテンプレートは入れない → installer.nsh が自前で入れる
  expect(nsh).toMatch(/!ifmacrodef customCheckAppRunning\s+!insertmacro customCheckAppRunning\s+!else\s+!insertmacro IS_POWERSHELL_AVAILABLE\s+!insertmacro _CHECK_APP_RUNNING/);
  expect(nsh).toMatch(/!ifmacrondef customCheckAppRunning\s+!include "getProcessInfo\.nsh"\s+Var pid/);
  expect(nsh).toMatch(/!macro _CHECK_APP_RUNNING[\s\S]*?\$\{GetProcessInfo\} 0 \$pid/);
  // 既定の探し方は「該当が 1 つだけ」を見逃す（Windows PowerShell 5.1 は単一の CimInstance に .Count が無く $null -gt 0 になる）。
  // この行が変わった（直った）なら、installer.nsh の先止めが要るか見直す
  expect(nsh).toMatch(/\.Count -gt 0\) \{ exit 0 \} else \{ exit 1 \}/);
  // installer.nsh: resources/engine/ の下のプロセスを先に止めてから既定を呼ぶ
  const mine = fs.readFileSync(new URL('../../build/installer.nsh', import.meta.url), 'utf8');
  expect(mine).toMatch(/!include "getProcessInfo\.nsh"\s+Var pid/);
  expect(mine).toMatch(/!macro customCheckAppRunning[\s\S]*?!insertmacro IS_POWERSHELL_AVAILABLE[\s\S]*?StartsWith\('\$INSTDIR\\resources\\engine\\'[^\n]*Stop-Process[\s\S]*?!insertmacro _CHECK_APP_RUNNING\s+!macroend/);
  const yml = fs.readFileSync(new URL('../../electron-builder.yml', import.meta.url), 'utf8');
  expect(yml).not.toMatch(/^\s*include:/m);   // build/installer.nsh は既定の置き場（buildResources）から拾われる
});

test('(R6b) アンインストールで更新のキャッシュを消す。ただし新しい版が古い版を消すとき（--updated）は消さない', () => {
  const builder = path.dirname(require.resolve('electron-builder/package.json'));
  const lib = path.dirname(require.resolve('app-builder-lib/package.json', { paths: [builder] }));
  const nsisDir = path.join(lib, 'templates', 'nsis');
  const un = fs.readFileSync(path.join(nsisDir, 'uninstaller.nsh'), 'utf8');
  const util = fs.readFileSync(path.join(nsisDir, 'include', 'installUtil.nsh'), 'utf8');
  // アンインストールの section が customUnInstall を呼ぶ
  expect(un).toMatch(/Section "un\.\$\{UNINSTALL_SECTION_NAME\}"[\s\S]*?!ifmacrodef customUnInstall\s+!insertmacro customUnInstall/);
  // 新しい版のインストーラが古い版のアンインストーラを呼ぶときは、データを消さない設定でも常に --updated を付ける
  // （アプリからの更新も、手動で上から入れる場合も。アプリからの更新では、動いているインストーラ自身が gliss-updater\pending にある）
  expect(util).toMatch(/always pass --updated flag[\s\S]*?StrCpy \$0 "\$0 --updated"/);
  expect(util).toMatch(/ExecWait '"\$uninstallerFileNameTemp" \/S \/KEEP_APP_DATA \$0 _\?=\$installationDir'/);
  // installer.nsh: --updated でないとき（利用者のアンインストール）だけ、更新のキャッシュのフォルダを消す
  const mine = fs.readFileSync(new URL('../../build/installer.nsh', import.meta.url), 'utf8');
  expect(mine).toMatch(/!macro customUnInstall\s+\$\{ifNot\} \$\{isUpdated\}\s+RMDir \/r "\$LOCALAPPDATA\\gliss-updater"\s+\$\{endIf\}\s+!macroend/);
  // フォルダ名は electron-builder が付ける updaterCacheDirName（パッケージ名 + "-updater"）と同じ
  const pkg = JSON.parse(fs.readFileSync(new URL('../../package.json', import.meta.url), 'utf8'));
  expect(`${pkg.name}-updater`).toBe('gliss-updater');
});

test('(R7) エンジンのプロセスを止める', async () => {
  let alive = [11, 12];
  const killed = [];
  const r = await stopEngineProcesses('C:\\Gliss\\resources\\engine\\vocal-engine', {
    find: async () => [...alive],
    kill: async (pid) => { killed.push(pid); if (pid === 11) alive = alive.filter((p) => p !== 11); },
    sleep: async () => {}, timeoutMs: 50,
  });
  expect(killed).toEqual([11, 12]);
  expect(r).toEqual({ stopped: [11], left: [12] });
  // 探し方: フォルダの末尾に区切りを付けて渡す（Gliss2 のような隣のフォルダを拾わない）
  let seen = null;
  const pids = await findEngineProcesses('C:\\Gliss\\resources\\engine\\vocal-engine', {
    platform: 'win32', exec: async (_script, env) => { seen = env.GLISS_ENGINE_DIR; return { stdout: '123\r\n456\r\n\r\n' }; },
  });
  expect(pids).toEqual([123, 456]);
  expect(seen).toBe(path.resolve('C:\\Gliss\\resources\\engine\\vocal-engine') + path.sep);
  expect(await findEngineProcesses('x', { platform: 'linux', exec: async () => { throw new Error('呼ばない'); } })).toEqual([]);
});

test('(R8) 成果物をそろえる: latest.yml の版・SHA-512・余計なインストーラを確かめ、SHA256SUMS を作る', async () => {
  const { prepareAssets } = await import('../../../scripts/release-assets.mjs');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-assets-'));
  try {
    const dist = path.join(dir, 'dist');
    const out = path.join(dir, 'out');
    fs.mkdirSync(dist);
    const exe = 'Gliss-0.1.0-beta.2-win-x64.exe';
    fs.writeFileSync(path.join(dist, exe), 'installer');
    fs.writeFileSync(path.join(dist, `${exe}.blockmap`), 'blockmap');
    const sha = (s) => crypto.createHash('sha512').update(s).digest('base64');
    const latest = { version: '0.1.0-beta.2', files: [{ url: exe, sha512: sha('installer'), size: 9 }], path: exe, sha512: sha('installer') };
    const parseYaml = (t) => JSON.parse(t);
    fs.writeFileSync(path.join(dist, 'latest.yml'), JSON.stringify(latest));
    fs.writeFileSync(path.join(dir, 'NOTICES.txt'), 'n');
    const names = prepareAssets({ dist, out, version: '0.1.0-beta.2', extra: [path.join(dir, 'NOTICES.txt')], parseYaml });
    expect(names).toEqual([exe, `${exe}.blockmap`, 'latest.yml', 'NOTICES.txt', 'SHA256SUMS.txt']);
    const sums = fs.readFileSync(path.join(out, 'SHA256SUMS.txt'), 'utf8').trim().split('\n');
    expect(sums).toHaveLength(4);
    expect(sums.find((l) => l.endsWith(`  ${exe}`))).toBe(`${crypto.createHash('sha256').update('installer').digest('hex')}  ${exe}`);
    expect(() => prepareAssets({ dist, out, version: '0.1.0-beta.3', parseYaml })).toThrow(/版/);
    fs.writeFileSync(path.join(dist, exe), 'tampered!');
    expect(() => prepareAssets({ dist, out, version: '0.1.0-beta.2', parseYaml })).toThrow(/SHA-512/);
    fs.writeFileSync(path.join(dist, exe), 'installer');
    fs.writeFileSync(path.join(dist, 'Gliss-0.1.0-beta.1-win-x64.exe'), 'old');
    expect(() => prepareAssets({ dist, out, version: '0.1.0-beta.2', parseYaml })).toThrow(/別の版/);
    fs.rmSync(path.join(dist, 'Gliss-0.1.0-beta.1-win-x64.exe'));
    // 任意のアドオン: アプリに埋めた目録の大きさ・SHA-256 と合う zip だけを載せる
    const addons = path.join(dir, 'dist-addons');
    fs.mkdirSync(addons);
    const zip = 'Gliss-addon-lyrics-ja-812eeb61609b.zip';
    fs.writeFileSync(path.join(addons, zip), 'zip');
    const catalog = path.join(dir, 'addons-catalog.json');
    const entry = { id: 'lyrics-ja', file: zip, size: 3, sha256: crypto.createHash('sha256').update('zip').digest('hex') };
    fs.writeFileSync(catalog, JSON.stringify({ addons: [entry] }));
    fs.rmSync(out, { recursive: true });
    expect(prepareAssets({ dist, out, version: '0.1.0-beta.2', addons, catalog, parseYaml })).toEqual([exe, `${exe}.blockmap`, 'latest.yml', zip, 'SHA256SUMS.txt']);
    fs.writeFileSync(catalog, JSON.stringify({ addons: [{ ...entry, sha256: '0'.repeat(64) }] }));
    expect(() => prepareAssets({ dist, out, version: '0.1.0-beta.2', addons, catalog, parseYaml })).toThrow(/SHA-256 が目録と違う/);
    fs.writeFileSync(catalog, JSON.stringify({ addons: [] }));
    expect(() => prepareAssets({ dist, out, version: '0.1.0-beta.2', addons, catalog, parseYaml })).toThrow(/アドオンが無い/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('(R9) 公開: 下書きで作ってから公開・beta は prerelease で Latest にしない', async () => {
  const { createArgs, publishArgs } = await import('../../../scripts/release-publish.mjs');
  const beta = createArgs({ tag: 'v0.1.0-beta.2', files: ['a.exe'], notes: 'n.md' });
  expect(beta).toEqual(['release', 'create', 'v0.1.0-beta.2', 'a.exe', '--verify-tag', '--draft', '--title', 'Gliss 0.1.0-beta.2',
    '--notes-file', 'n.md', '--prerelease']);
  expect(publishArgs({ tag: 'v0.1.0-beta.2' })).toEqual(['release', 'edit', 'v0.1.0-beta.2', '--draft=false', '--latest=false']);
  expect(createArgs({ tag: 'v1.0.0', files: [], notes: 'n.md' })).not.toContain('--prerelease');
  expect(publishArgs({ tag: 'v1.0.0' })).toContain('--latest=true');
});

test('(R10) THIRD_PARTY_NOTICES: METADATA の読み方・ライセンス名・copyleft の判定', async () => {
  const { parseMetadata, licenseName, isCopyleft } = await import('../../../scripts/third-party-notices.mjs');
  const meta = parseMetadata('Metadata-Version: 2.4\nName: soxr\nVersion: 1.1.0\nLicense-Expression: LGPL-2.1-or-later\nClassifier: A\n\nbody: x');
  expect(meta.name).toEqual(['soxr']);
  expect(licenseName(meta)).toBe('LGPL-2.1-or-later');
  // License に全文を入れているものは分類を使う
  const long = parseMetadata('Name: x\nLicense: Copyright (c) 2001-2002 Enthought\n        Redistribution and use...\nClassifier: License :: OSI Approved :: BSD License\n');
  expect(licenseName(long)).toBe('BSD License');
  expect(isCopyleft('praat-parselmouth', 'GPLv3')).toBe(true);
  expect(isCopyleft('soxr', 'LGPL-2.1-or-later')).toBe(true);
  expect(isCopyleft('certifi', 'MPL-2.0')).toBe(true);
  expect(isCopyleft('pyinstaller', 'GPLv2-or-later with a special exception')).toBe(false);
  expect(isCopyleft('numpy', 'BSD-3-Clause AND 0BSD AND MIT')).toBe(false);
});
