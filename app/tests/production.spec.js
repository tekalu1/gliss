// 実制作で使うために足したもの（区間ごとの歌詞 / ネイティブメニュー / WAV 書き出し）。
//
//   (1) 歌詞レーンのダブルクリックが「クリックした位置の発声区間」に歌詞を付ける
//   (2) 既に歌詞のある区間をダブルクリックすると、その区間の歌詞が入った状態で開く
//   (3) ファイル > 書き出し先を選んで書き出し… で元と同じ sr / ビット深度 / ch / 長さの WAV が出る
//   (4) タイムスケールのダブルクリックで発声区間へ飛ぶ
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const PROJECT = path.join(REPO, 'projects', '_test-production');
// 設定（最近使ったファイル・前回の表示範囲・ウィンドウの位置）も毎回まっさらにする。人が使った
// ときの表示範囲を引き継ぐと、狙うノートが画面の外に出てドラッグが外れる（人の設定も汚さない）。
const USERDATA = `${PROJECT}-userdata`;
const OUT = path.join(PROJECT, 'export', 'from-menu.wav');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'],
    env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  // S.busy だけだと、離した後に計画の到着や前の編集を待っている間（まだ busy でない）を見落とす
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 240000 });
}

const entries = () => win.evaluate(() => window.__app.lyricsEntries());

/** 発声のかたまりの真ん中の画面 x（歌詞レーンの上）。 */
async function laneXOfUtterance(i) {
  return win.evaluate((k) => {
    const us = window.__app.utterances();
    const u = us[k];
    const S = window.__app.S;
    const svg = document.querySelector('#roll');
    const W = svg.getBoundingClientRect().width;
    const mid = (u[0] + u[1]) / 2;
    return 44 + (mid - S.view.t0) / S.view.span * (W - 44);
  }, i);
}

test('(1) 歌詞レーンのダブルクリックは「その位置の発声区間」に歌詞を付ける', async () => {
  expect(await entries()).toEqual([]);
  const us = await win.evaluate(() => window.__app.utterances());
  expect(us.length).toBeGreaterThan(1);              // 無音で 2 つ以上に割れている

  const svg = await win.locator('#roll').boundingBox();
  const x = await laneXOfUtterance(0);
  await win.mouse.dblclick(svg.x + x, svg.y + svg.height - 12);
  await expect(win.locator('#lyr')).toBeVisible();
  // 歌詞がまだ無いので空。区間はクリックした発声のかたまり
  expect(await win.locator('#lyrIn').inputValue()).toBe('');
  const label = await win.locator('#lyrIn').getAttribute('data-range');
  expect(label).toMatch(/^\d+\.\d+–\d+\.\d+$/);

  await win.locator('#lyrIn').fill(M.text('C.part1'));
  await win.keyboard.press('Enter');
  await settle();

  const es = await entries();
  expect(es.length).toBe(1);
  expect(es[0].text).toBe(M.text('C.part1'));
  expect(es[0].start_sec).not.toBeNull();            // **素材全体ではなく区間**
  expect(es[0].end_sec - es[0].start_sec).toBeLessThan(us[0][1] - us[0][0] + 0.5);

  // 音素は区間の中だけ。外は無音（歌詞なし）
  const ph = await win.evaluate(() => window.__app.phonemes());
  const voiced = ph.filter((p) => p.label !== 'silence');
  expect(voiced.length).toBeGreaterThan(0);
  const lo = es[0].start_sec - 0.25;
  const hi = es[0].end_sec + 0.25;
  expect(voiced.every((p) => p.start >= lo && p.end <= hi)).toBe(true);
});

test('(2) 歌詞のある区間をダブルクリックすると、その区間の歌詞が入っている', async () => {
  const svg = await win.locator('#roll').boundingBox();
  const x = await laneXOfUtterance(0);
  await win.mouse.dblclick(svg.x + x, svg.y + svg.height - 12);
  await expect(win.locator('#lyr')).toBeVisible();
  expect(await win.locator('#lyrIn').inputValue()).toBe(M.text('C.part1'));
  await win.keyboard.press('Escape');
  await expect(win.locator('#lyr')).toBeHidden();

  // 2 つめの発声区間は歌詞なしで開く（区間ごとに独立している）
  const x2 = await laneXOfUtterance(1);
  await win.mouse.dblclick(svg.x + x2, svg.y + svg.height - 12);
  await expect(win.locator('#lyr')).toBeVisible();
  expect(await win.locator('#lyrIn').inputValue()).toBe('');
  await win.keyboard.press('Escape');
});

test('(3) ファイル > 書き出し先を選んで書き出し… で元と同じ形式・長さの WAV が出る', async () => {
  // 編集を 1 つ入れてから書き出す
  await win.evaluate(async () => {
    const n = window.__app.S.pitched[0];
    await window.api.call('shift_pitch', { note_id: n.id, cents: 100, author: 'human' });
    await window.__app.refresh();
  });
  await settle();

  fs.rmSync(OUT, { force: true });
  await app.evaluate(async ({ dialog }, out) => {
    dialog.showSaveDialog = async () => ({ canceled: false, filePath: out });
  }, OUT);

  const r = await win.evaluate(() => window.__app.onMenu({ cmd: 'export-as' }));
  await settle();
  expect(fs.existsSync(OUT)).toBe(true);

  // WAV のヘッダを直接読んで元と突き合わせる（sr / ch / ビット深度 / データ長）
  const head = (p) => {
    const b = fs.readFileSync(p);
    const out = { ch: b.readUInt16LE(22), sr: b.readUInt32LE(24), bits: b.readUInt16LE(34) };
    let i = 12;
    while (i < b.length - 8) {
      const id = b.toString('ascii', i, i + 4);
      const sz = b.readUInt32LE(i + 4);
      if (id === 'data') { out.dataBytes = sz; break; }
      i += 8 + sz + (sz % 2);
    }
    return out;
  };
  const a = head(TAKE);
  const z = head(OUT);
  expect(z).toEqual(a);                              // sr / ch / bits / データ長がすべて同じ

  const st = await win.evaluate(() => window.__app.status());
  expect(st).toContain('書き出した');
});

test('(4) タイムスケールのダブルクリックで発声区間へ飛ぶ', async () => {
  await win.evaluate(() => { window.__app.S.view = { t0: 0, span: window.__app.totalSec() }; });
  const before = await win.evaluate(() => window.__app.view());
  const jumped = await win.evaluate(() => window.__app.jumpToUtterance(
    window.__app.utterances()[1][0]));
  expect(jumped).toBe(true);
  const after = await win.evaluate(() => window.__app.view());
  expect(after.span).toBeLessThan(before.span);      // そのかたまりに寄っている
});

test('実行時のコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
