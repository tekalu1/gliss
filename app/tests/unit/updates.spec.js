// 自動更新の状態機械（updates.mjs）と、beta の版を semver で選び直す provider（update-provider.mjs）。
// Electron は起動しない。updater はフェイク（electron-updater の autoUpdater と同じ形のイベントとメソッド）。
//
//   (U1) allowDowngrade は常に false（チャネルを変えた後・確認の直前にも）。版が beta のアプリは既定で beta
//   (U2) 設定は updates.json に原子的に保存し、次の起動で読む。チャネル → allowPrerelease
//   (U3) 確認 → 見つかる → 自動ダウンロード → downloaded（自動ダウンロードがオフなら available で止まる）
//   (U4) 開発版（enabled: false）は unavailable のまま何もしない
//   (U5) 失敗の文は利用者向けの 3 種類（検証・ネットワーク・その他）。公開された版が無いのは失敗にしない
//   (U6) 再起動して更新: 取り消し（false）なら downloaded に戻る。失敗も downloaded に戻して文を出す
//   (U7) 更新した後の最初の起動だけ updatedFrom が付く（初回のインストール・開発版では付かない）
//   (U8) 自動の確認は間隔を守り、見つけた版を出している間は確かめ直さない
//   (U9) provider: releases.atom の並びによらず semver で一番新しい版の latest.yml を読む
import { test, expect } from '@playwright/test';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

import { MESSAGES, Updates, errorKind } from '../../updates.mjs';
import { newestReleaseProvider, newestTag, readUpdateConfig } from '../../update-provider.mjs';

const require = createRequire(import.meta.url);

class FakeUpdater extends EventEmitter {
  constructor({ latest = null, fail = null, failDownload = null } = {}) {
    super();
    Object.assign(this, { latest, fail, failDownload, checks: 0, downloads: 0, channelSets: 0 });
    this._allowDowngrade = false;
  }
  // electron-updater の AppUpdater と同じく、channel を設定すると allowDowngrade が true になる
  set channel(v) { this.channelSets += 1; this._channel = v; this.allowDowngrade = true; }
  get channel() { return this._channel; }
  async checkForUpdates() {
    this.checks += 1;
    this.seenAllowDowngrade = this.allowDowngrade;
    this.seenAllowPrerelease = this.allowPrerelease;
    if (this.fail) { const e = this.fail; this.emit('error', e); throw e; }
    if (this.latest) this.emit('update-available', { version: this.latest });
    else this.emit('update-not-available', {});
    return {};
  }
  async downloadUpdate() {
    this.downloads += 1;
    if (this.failDownload) { const e = this.failDownload; this.emit('error', e); throw e; }
    for (const percent of [10, 55.5, 100]) this.emit('download-progress', { percent });
    this.emit('update-downloaded', { version: this.latest });
    return [];
  }
}

let tmp;
test.beforeEach(() => { tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-updates-')); });
test.afterEach(() => { fs.rmSync(tmp, { recursive: true, force: true }); });

function make(o = {}) {
  const updater = o.updater || new FakeUpdater(o);
  const u = new Updates({ updater, version: o.version || '0.1.0-beta.1', file: path.join(tmp, 'updates.json'),
    enabled: o.enabled ?? true, install: o.install, log: () => {}, now: o.now });
  return { u, updater };
}

test('(U1) allowDowngrade は常に false・beta の版は既定で beta', async () => {
  const { u, updater } = make();
  updater.allowDowngrade = true;               // 何かが true にしても
  await u.init();
  expect(u.state.channel).toBe('beta');
  expect(updater.allowPrerelease).toBe(true);
  expect(updater.allowDowngrade).toBe(false);
  expect(updater.autoDownload).toBe(false);
  expect(updater.autoInstallOnAppQuit).toBe(false);
  updater.channel = 'beta';                    // electron-updater の set channel は true にする
  expect(updater.allowDowngrade).toBe(true);
  await u.check();
  expect(updater.seenAllowDowngrade).toBe(false);   // 確認の直前に必ず false に戻す
  await u.setPreferences({ channel: 'stable' });
  expect(updater.allowPrerelease).toBe(false);
  expect(updater.allowDowngrade).toBe(false);
  // channel そのものは触らない（触ると electron-updater がダウングレードを許す）
  expect(updater.channelSets).toBe(1);
  const stable = make({ version: '1.0.0' });
  await stable.u.init();
  expect(stable.u.state.channel).toBe('stable');
  expect(stable.updater.allowPrerelease).toBe(false);
});

test('(U2) 設定の保存と読み込み（原子的に書く・壊れていたら既定）', async () => {
  const { u } = make();
  await u.init();
  await u.setPreferences({ channel: 'stable', autoCheck: false, autoDownload: false });
  const saved = JSON.parse(fs.readFileSync(path.join(tmp, 'updates.json'), 'utf8'));
  expect(saved).toEqual({ channel: 'stable', autoCheck: false, autoDownload: false, lastVersion: '0.1.0-beta.1' });
  expect(fs.readdirSync(tmp).filter((f) => f.endsWith('.tmp'))).toEqual([]);
  const again = make();
  await again.u.init();
  expect(again.u.state).toMatchObject({ channel: 'stable', autoCheck: false, autoDownload: false });
  expect(again.updater.allowPrerelease).toBe(false);
  // 変な値は受けない
  await again.u.setPreferences({ channel: 'nightly', autoCheck: 'yes' });
  expect(again.u.state).toMatchObject({ channel: 'stable', autoCheck: false });
  fs.writeFileSync(path.join(tmp, 'updates.json'), '{ 壊れている');
  const broken = make();
  await broken.u.init();
  expect(broken.u.state).toMatchObject({ channel: 'beta', autoCheck: true, autoDownload: true });
});

test('(U3) 確認 → 自動ダウンロード → downloaded。オフなら available で待つ', async () => {
  const { u, updater } = make({ latest: '0.1.0-beta.2' });
  await u.init();
  const phases = [];
  u.on('state', (s) => phases.push(s.phase));
  const s = await u.check();
  expect(s).toMatchObject({ phase: 'downloaded', target: '0.1.0-beta.2', progress: 100, error: null });
  expect(phases).toEqual(expect.arrayContaining(['checking', 'available', 'downloading', 'downloaded']));
  expect(updater.downloads).toBe(1);
  expect(s.lastChecked).toBeTruthy();
  // 取得済みのときに確かめても、取得し直さない
  await u.check();
  expect(updater.checks).toBe(1);

  const manual = make({ latest: '0.1.0-beta.2' });
  await manual.u.init();
  await manual.u.setPreferences({ autoDownload: false });
  expect((await manual.u.check()).phase).toBe('available');
  expect(manual.updater.downloads).toBe(0);
  expect((await manual.u.download()).phase).toBe('downloaded');

  const none = make();
  await none.u.init();
  expect((await none.u.check()).phase).toBe('current');
});

test('(U4) 開発版は unavailable のまま何もしない', async () => {
  const { u, updater } = make({ enabled: false, latest: '9.9.9' });
  await u.init();
  expect(u.state.phase).toBe('unavailable');
  expect((await u.check()).phase).toBe('unavailable');
  expect(await u.auto()).toBe(false);
  expect(updater.checks).toBe(0);
  await u.setPreferences({ channel: 'stable' });
  expect(u.state.phase).toBe('unavailable');
});

test('(U5) 失敗の文は 3 種類にまとめる', async () => {
  const err = (code, message = code, statusCode) => Object.assign(new Error(message), { code, statusCode });
  expect(errorKind(err('ERR_CHECKSUM_MISMATCH', 'sha512 checksum mismatch, expected x, got y'))).toBe('verify');
  expect(errorKind(err('ERR_UPDATER_INVALID_SIGNATURE'))).toBe('verify');
  expect(errorKind(err('ENOTFOUND', 'getaddrinfo ENOTFOUND github.com'))).toBe('network');
  expect(errorKind(new Error('net::ERR_INTERNET_DISCONNECTED'))).toBe('network');
  expect(errorKind(err(undefined, 'HttpError: 503', 503))).toBe('network');
  expect(errorKind(err('ERR_UPDATER_NO_PUBLISHED_VERSIONS'))).toBe('nothing');
  expect(errorKind(new Error('何か'))).toBe('other');

  const net = make({ fail: err('ECONNREFUSED', 'connect ECONNREFUSED 127.0.0.1:443') });
  await net.u.init();
  expect(await net.u.check()).toMatchObject({ phase: 'error', error: MESSAGES.network });
  // 生の文（ホスト名・パス）は画面に出さない
  expect(net.u.state.error).not.toContain('127.0.0.1');

  const bad = make({ latest: '0.1.0-beta.2', failDownload: err('ERR_CHECKSUM_MISMATCH', 'sha512 checksum mismatch') });
  await bad.u.init();
  expect(await bad.u.check()).toMatchObject({ phase: 'error', error: MESSAGES.verify });

  const nothing = make({ fail: err('ERR_UPDATER_LATEST_VERSION_NOT_FOUND') });
  await nothing.u.init();
  expect(await nothing.u.check()).toMatchObject({ phase: 'current', error: null });

  const other = make({ fail: new Error('予想しない') });
  await other.u.init();
  expect((await other.u.check()).error).toBe(MESSAGES.other);
});

test('(U6) 再起動して更新: 取り消し・失敗は downloaded に戻る', async () => {
  let answer = false;
  const calls = [];
  const { u } = make({ latest: '0.1.0-beta.2', install: async () => { calls.push('install'); if (answer === 'throw') throw new Error('エンジンを止められなかった'); return answer; } });
  await u.init();
  await expect(u.apply()).rejects.toThrow();          // まだ取得していない
  await u.check();
  expect(await u.apply()).toBe(false);                // 保存の確認でキャンセル
  expect(u.state.phase).toBe('downloaded');
  answer = 'throw';
  expect(await u.apply()).toBe(false);
  expect(u.state).toMatchObject({ phase: 'downloaded' });
  expect(u.state.error).toContain('エンジンを止められなかった');
  answer = true;
  expect(await u.apply()).toBe(true);
  expect(u.state.phase).toBe('installing');
  expect(calls).toHaveLength(3);
});

test('(U7) 更新した後の最初の起動だけ updatedFrom が付く', async () => {
  const file = path.join(tmp, 'updates.json');
  const first = new Updates({ updater: new FakeUpdater(), version: '0.1.0-beta.1', file, enabled: true, log: () => {} });
  expect((await first.init()).updatedFrom).toBe(null);    // 初回のインストール
  const next = new Updates({ updater: new FakeUpdater(), version: '0.1.0-beta.2', file, enabled: true, log: () => {} });
  expect((await next.init()).updatedFrom).toBe('0.1.0-beta.1');
  expect(next.dismissNotice().updatedFrom).toBe(null);
  const again = new Updates({ updater: new FakeUpdater(), version: '0.1.0-beta.2', file, enabled: true, log: () => {} });
  expect((await again.init()).updatedFrom).toBe(null);    // 2 回目の起動
  // 開発版は覚えた版を書き換えない・出さない
  const dev = new Updates({ updater: new FakeUpdater(), version: '0.2.0', file, enabled: false, trackVersion: false, log: () => {} });
  expect((await dev.init()).updatedFrom).toBe(null);
  expect(JSON.parse(fs.readFileSync(file, 'utf8')).lastVersion).toBe('0.1.0-beta.2');
});

test('(U8) 自動の確認は間隔を守る', async () => {
  let t = 1000;
  const { u, updater } = make({ now: () => t });
  await u.init();
  expect(await u.auto()).toBe(true);
  expect(await u.auto(60000)).toBe(false);            // 前回からまだ間がない
  t += 60000;
  expect(await u.auto(60000)).toBe(true);
  expect(updater.checks).toBe(2);
  await u.setPreferences({ autoCheck: false });
  t += 60000;
  expect(await u.auto(0)).toBe(false);                // 自動確認がオフ
  await u.check();                                    // 手動はできる
  expect(updater.checks).toBe(3);
  const found = make({ latest: '0.1.0-beta.2', now: () => t });
  await found.u.init();
  await found.u.setPreferences({ autoDownload: false });
  await found.u.check();
  t += 1e9;
  expect(await found.u.auto(0)).toBe(false);          // 見つけた版を出している間
});

test('(U9) newestTag は semver で選ぶ（文字列の順・並びの順ではない）', () => {
  const semver = require(require.resolve('semver', { paths: [path.dirname(require.resolve('electron-updater/package.json'))] }));
  expect(newestTag(['v0.1.0-beta.9', 'v0.1.0-beta.11', 'v0.1.0-beta.10'], semver)).toBe('v0.1.0-beta.11');
  expect(newestTag(['v0.1.0-beta.3', 'android-v0.1.0-676', 'v0.1.0'], semver)).toBe('v0.1.0');
  expect(newestTag(['v0.2.0-beta.1', 'v0.1.5'], semver)).toBe('v0.2.0-beta.1');
  expect(newestTag(['latest', null], semver)).toBe(null);
});

test('(U9) provider: beta の人は feed の並びによらず一番新しい版の latest.yml を読む', async () => {
  const Provider = newestReleaseProvider();
  const entry = (tag, title) => `<entry><id>tag:github.com,2008:Repository/1/${tag}</id><updated>2026-10-01T00:00:00Z</updated>`
    + `<link rel="alternate" type="text/html" href="https://github.com/tekalu1/gliss/releases/tag/${tag}"/>`
    + `<title>${title}</title><content type="html">notes of ${tag}</content></entry>`;
  // GitHub の並びが版の順でないとき（beta.9 が先頭）
  const atom = `<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">${
    entry('v0.1.0-beta.9', 'Gliss 0.1.0-beta.9')}${entry('v0.1.0-beta.11', 'Gliss 0.1.0-beta.11')}${entry('v0.1.0-beta.10', 'Gliss 0.1.0-beta.10')}</feed>`;
  const yml = (v) => `version: ${v}\nfiles:\n  - url: Gliss-${v}-win-x64.exe\n    sha512: abc\n    size: 1\npath: Gliss-${v}-win-x64.exe\nsha512: abc\nreleaseDate: '2026-10-01T00:00:00.000Z'\n`;
  const requested = [];
  const updater = { allowPrerelease: true, currentVersion: { raw: '0.1.0-beta.9' }, channel: null };
  const p = new Provider({ provider: 'github', owner: 'tekalu1', repo: 'gliss', channel: 'latest' }, updater,
    { isUseMultipleRangeRequest: false, platform: 'win32', executor: {
      request: async (opts) => {
        const url = `${opts.protocol}//${opts.hostname}${opts.path}`;
        requested.push(url);
        if (url.endsWith('releases.atom')) return atom;
        const m = /download\/v([^/]+)\/latest\.yml$/.exec(url);
        if (m) return yml(m[1]);
        throw new Error(`想定しない URL: ${url}`);
      },
    } });
  const info = await p.getLatestVersion();
  expect(info.version).toBe('0.1.0-beta.11');
  expect(info.tag).toBe('v0.1.0-beta.11');
  expect(info.releaseNotes).toBe('notes of v0.1.0-beta.11');
  expect(requested.some((u) => u.endsWith('/tekalu1/gliss/releases/download/v0.1.0-beta.11/latest.yml'))).toBe(true);
  // ダウンロードの URL も選んだ版の Release を指す
  expect(p.resolveFiles(info)[0].url.href).toBe('https://github.com/tekalu1/gliss/releases/download/v0.1.0-beta.11/Gliss-0.1.0-beta.11-win-x64.exe');
});

test('(U9) app-update.yml を読める', () => {
  expect(readUpdateConfig('owner: tekalu1\nrepo: gliss\nprovider: github\nchannel: latest\nupdaterCacheDirName: gliss-updater\n'))
    .toEqual({ owner: 'tekalu1', repo: 'gliss', provider: 'github', channel: 'latest', updaterCacheDirName: 'gliss-updater' });
});
