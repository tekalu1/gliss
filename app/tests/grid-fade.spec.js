// 時間グリッド・音程グリッド・スナップ・テンポ（issue #18）とノートのフェード（issue #20）。提案 proposal/v3.html §4・§5・§8。
//
//   #18
//     (G1) 既定: スナップはどちらもオフ・テンポ無し（「— BPM」を薄く）・秒のグリッドとルーラー・細かさのプルダウンは秒の刻み。
//          N / Shift+N（とヘッダーのアイコン・右クリック）でオン・オフ
//     (G2) テンポ: 数字をクリックで入力 → 小節・拍のグリッドとルーラー・プルダウンは 1/1〜1/32 と 3 連符。取り消せる
//     (G3) ホイールを続けて回すと取り消し 1 回・上下のドラッグも 1 回（ドラッグ中はヘッダーとグリッドが動く）・拍子
//     (G4) 時間スナップ: ノートの端・移動がグリッドに（Shift で解除）・はさみの位置
//     (G5) 音程スナップ: 帯の高さ（平均の音程）が半音に・ドラッグ中だけ「G4 ±0¢」
//     (G6) トラックビューのクリップの位置ずらしもスナップに従う（細かさは共通のプルダウン）
//     (G7) iXML のテンポ: テンポマップの入った伴奏を足すと自動で読み、ヘッダーに小さく「iXML」。ルーラーの右クリック「テンポと拍子…」
//   #20
//     (F1) ノートに乗ると帯の上の両端につまみ。内側へドラッグ → 帯がその分細くなる（ドラッグ中 = 離した後）→ 1 回で戻る
//     (F2) 右クリック「フェードを消す」（フェードのあるノートのときだけ）
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2', 'E');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const GUIDE = M.clip('C2');
const INST_SRC = M.clip('E');
const PROJECT = path.join(REPO, 'projects', '_test-grid-fade');
const USERDATA = `${PROJECT}-userdata`;
const WORK = `${PROJECT}-files`;
const DOCS = path.join(APP, 'screenshots');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  for (const d of [PROJECT, USERDATA, WORK]) fs.rmSync(d, { recursive: true, force: true });
  fs.mkdirSync(WORK, { recursive: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await showRange(2.2, 3.5);
});

test.afterAll(async () => {
  await app?.close();
  for (const d of [WORK]) fs.rmSync(d, { recursive: true, force: true });
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
async function showRange(a, b) {
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [a, b]);
}
const hist = () => win.evaluate(() => window.__app.hist());
const grid = () => win.evaluate(() => window.__app.grid());
const tempoText = () => win.evaluate(() => window.__app.tempoText());
const items = () => win.evaluate(() => window.__app.menuItems());
const notes = () => win.evaluate(() => window.__app.notes());
async function escape() {
  await win.keyboard.press('Escape');
  await expect(win.locator('#menu')).toBeHidden();
}
/** いちばん長い音程ノート（画面に入っているもの）。 */
async function longNote() {
  const ns = await win.evaluate(() => {
    const S = window.__app.S;
    return S.pitched.map((n) => ({ id: n.id, len: n.edited_end_sec - n.edited_start_sec, s: n.edited_start_sec, e: n.edited_end_sec }))
      .filter((n) => n.s >= S.view.t0 && n.e <= S.view.t0 + S.view.span);
  });
  return ns.sort((a, b) => b.len - a.len)[0];
}
async function rollBox() { return win.locator('#roll').boundingBox(); }
/** ピアノロールの秒 → ページの x。 */
async function pageX(t) {
  const r = await rollBox();
  return win.evaluate(([tt, left]) => {
    const S = window.__app.S; const W = document.querySelector('#roll').getBoundingClientRect().width;
    return left + 44 + (tt - S.view.t0) / S.view.span * (W - 44);
  }, [t, r.x]);
}

// ---------------------------------------------------------------- #18
test('(G1) 既定はスナップ オフ・テンポ無し・秒のグリッド。N / Shift+N で切り替え', async () => {
  let g = await grid();
  expect([g.snapT, g.snapP, g.bars, g.tempo]).toEqual([false, false, false, null]);
  expect(g.options).toEqual(['auto', '1', '0.5', '0.25', '0.1', '0.05', '0.01']);
  expect(g.sel).toBe('auto');
  const tt = await tempoText();
  expect(tt).toMatchObject({ bpm: '—', sig: '4/4', src: false, none: true });
  // 秒のグリッド（線）とルーラー（分:秒）
  const lines = await win.evaluate(() => window.__app.gridLines());
  expect(lines.length).toBeGreaterThan(3);
  const labs = await win.evaluate(() => window.__app.rulerLabels());
  expect(labs.length).toBeGreaterThan(1);
  for (const l of labs) expect(l).toMatch(/^\d+:\d\d(\.\d+)?$/);
  // スナップのアイコン（オフ）と N
  await expect(win.locator('#bSnapT')).toHaveAttribute('aria-pressed', 'false');
  await expect(win.locator('#bSnapT')).toHaveAttribute('title', '時間スナップ（N）。ドラッグ中 Shift で解除');
  await win.locator('#mock').focus();
  await win.keyboard.press('n');
  expect((await grid()).snapT).toBe(true);
  await expect(win.locator('#bSnapT')).toHaveAttribute('aria-pressed', 'true');
  expect(await win.evaluate(() => window.__app.status())).toContain('時間スナップ: オン');
  await win.keyboard.press('Shift+N');
  expect((await grid()).snapP).toBe(true);
  // 右クリック（空白）にチェックが付く
  const r = await rollBox();
  await win.evaluate(() => { window.__app.S.pv = { top: window.__app.S.pv.top + 8, span: window.__app.S.pv.span }; window.__app.render(); });
  await win.mouse.click(r.x + r.width - 30, r.y + 40, { button: 'right' });
  const it = await items();
  expect(it.filter((x) => /スナップ/.test(x.label)).map((x) => [x.label, x.checked, x.disabled]))
    .toEqual([['時間スナップ', true, false], ['音程スナップ（半音）', true, false]]);
  await escape();
  await win.evaluate(() => { window.__app.S.pv = { top: window.__app.S.pv.top - 8, span: window.__app.S.pv.span }; window.__app.render(); });
  // ヘッダーのアイコンでも切り替わる。取り消しの履歴には入らない
  await win.locator('#bSnapT').click();
  await win.locator('#bSnapP').click();
  g = await grid();
  expect([g.snapT, g.snapP]).toEqual([false, false]);
  expect((await hist())?.undo ?? null).toBeNull();
});

test('(G2) テンポを入力すると小節・拍のグリッドとルーラー。取り消せる', async () => {
  await win.locator('#tBpm').click();
  await expect(win.locator('#tIn')).toBeVisible();
  await win.keyboard.type('120');
  await win.keyboard.press('Enter');
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('120');
  const g = await grid();
  expect(g.bars).toBe(true);
  expect(g.tempo).toMatchObject({ bpm: 120, num: 4, den: 4, start_sec: 0, source: 'manual' });
  expect(g.options).toEqual(['auto', '1/1', '1/2', '1/4', '1/8', '1/16', '1/32', '1/2T', '1/4T', '1/8T', '1/16T']);
  expect((await hist()).undo.label).toBe('テンポ');
  // ルーラーは小節・拍（2.2〜3.5 秒 = 2 小節目の 2〜4 拍。1 小節 = 2 秒）
  const labs = await win.evaluate(() => window.__app.rulerLabels());
  expect(labs).toEqual(['2.2', '2.3', '2.4']);
  // 上のトラックビューのルーラーも小節
  const tv = await win.evaluate(() => window.__app.rulerLabels('#tvRuler'));
  expect(tv).toContain('1');
  // グリッドの線は拍の位置（0.5 秒ごと）を含む
  const t = await win.evaluate(() => window.__app.ticks(2.2, 3.5, 400, 22).out.filter((x) => x.l <= 1).map((x) => x.t));
  expect(t.map((v) => Math.round(v * 1000) / 1000)).toEqual([2.5, 3, 3.5]);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-grid.png') });
  // 取り消す → テンポ無し（秒）
  await win.keyboard.press('Control+z');
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('—');
  expect((await grid()).bars).toBe(false);
  await win.keyboard.press('Control+Shift+z');
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('120');
});

test('(G3) ホイールは続けて回すと取り消し 1 回・ドラッグも 1 回・拍子', async () => {
  const size0 = (await hist()).size;
  const b = await win.locator('#tBpm').boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  for (let i = 0; i < 4; i++) { await win.mouse.wheel(0, -100); await win.waitForTimeout(60); }
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('124');
  let h = await hist();
  expect(h.size).toBe(size0 + 1);
  expect(h.undo.label).toBe('テンポ');
  // Shift で 0.1 刻み
  await win.waitForTimeout(1000);
  await win.keyboard.down('Shift');
  await win.mouse.wheel(0, 100);
  await win.keyboard.up('Shift');
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('123.9');
  expect((await hist()).size).toBe(size0 + 2);
  // 上下のドラッグ: ドラッグ中はヘッダーとグリッドが動き、離すと 1 回
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  for (let k = 1; k <= 5; k++) await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - k * 8);
  await expect.poll(async () => (await tempoText()).bpm).toBe('134');
  expect((await grid()).tempo.bpm).toBe(134);
  await win.mouse.up();
  await settle();
  h = await hist();
  expect(h.size).toBe(size0 + 3);
  expect((await grid()).tempo.bpm).toBe(134);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await grid()).tempo.bpm).toBe(123.9);
  // 拍子: クリックで入力
  await win.locator('#tSig').click();
  await win.keyboard.press('Control+a');
  await win.keyboard.type('3/4');
  await win.keyboard.press('Enter');
  await settle();
  await expect.poll(async () => (await tempoText()).sig).toBe('3/4');
  expect((await hist()).undo.label).toBe('拍子');
  // 数字でないものは断る（履歴も増えない）
  const s1 = (await hist()).size;
  await win.locator('#tSig').click();
  await win.keyboard.press('Control+a');
  await win.keyboard.type('3/5');
  await win.keyboard.press('Enter');
  expect(await win.evaluate(() => window.__app.status())).toContain('拍子は');
  expect((await hist()).size).toBe(s1);
  // 以下のテストのため 120 BPM 4/4・1 小節目 0 秒
  await win.evaluate(() => window.__app.openTempoPop(200, 100));
  await win.locator('#tpB').fill('120');
  await win.locator('#tpN').selectOption('4');
  await win.locator('#tpD').selectOption('4');
  await win.locator('#tpS').fill('0');
  await win.locator('#tpOk').click();
  await settle();
  expect((await grid()).tempo).toMatchObject({ bpm: 120, num: 4, den: 4, start_sec: 0 });
  expect((await hist()).undo.label).toBe('テンポと拍子');
});

test('(G4) 時間スナップ: ノートの端・移動がグリッドに（Shift で解除）・はさみ', async () => {
  await win.evaluate(() => window.__app.setGrid({ snapT: true, divBars: '1/16' }));
  expect((await grid()).step).toBeCloseTo(0.125, 6);
  await showRange(2.2, 3.5);
  const n = await longNote();
  const off = await win.evaluate(() => window.__app.S.off);
  // 端（尻）を 20 px ほど右へ
  const info = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  const r = await rollBox();
  const x0 = r.x + info.end.x + info.end.w / 2; const y0 = r.y + info.yc;
  await win.mouse.move(x0, y0);
  await win.mouse.down();
  for (let k = 1; k <= 6; k++) await win.mouse.move(x0 - k * 4, y0);
  await win.mouse.up();
  await settle();
  const after = (await notes()).find((m) => m.id === n.id);
  const endT = after.editedEnd + off;
  expect(Math.abs(endT / 0.125 - Math.round(endT / 0.125))).toBeLessThan(0.01);
  expect(Math.abs(after.editedEnd - n.e)).toBeGreaterThan(1e-3);
  expect((await hist()).undo.label).toMatch(/ノートの長さ/);
  await win.keyboard.press('Control+z');
  await settle();
  // Shift を押しながら: グリッドに寄らない（動いた量そのまま）
  await win.mouse.move(x0, y0);
  await win.mouse.down();
  await win.keyboard.down('Shift');
  for (let k = 1; k <= 6; k++) await win.mouse.move(x0 - k * 3.3, y0);
  await win.mouse.up();
  await win.keyboard.up('Shift');
  await settle();
  const free = (await notes()).find((m) => m.id === n.id);
  const ft = free.editedEnd + off;
  expect(Math.abs(ft / 0.125 - Math.round(ft / 0.125))).toBeGreaterThan(0.02);
  await win.keyboard.press('Control+z');
  await settle();
  // 移動: ノートの頭がグリッドに
  const b = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  const cx = r.x + (b.x0 + b.x1) / 2; const cy = r.y + b.yc;
  await win.mouse.move(cx, cy);
  await win.mouse.down();
  for (let k = 1; k <= 8; k++) await win.mouse.move(cx + k * 3, cy);
  await win.mouse.up();
  await settle();
  const mv = (await notes()).find((m) => m.id === n.id);
  const st = mv.editedStart + off;
  if (Math.abs(mv.editedStart - n.s) > 1e-3) {                       // 動けた（隣にぶつかっていない）とき
    expect(Math.abs(st / 0.125 - Math.round(st / 0.125))).toBeLessThan(0.01);
    await win.keyboard.press('Control+z');
    await settle();
  }
  // はさみ: 切る位置の線がグリッドに
  await win.keyboard.press('3');
  const info2 = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  await win.mouse.move(r.x + info2.x0 + (info2.x1 - info2.x0) * 0.43, r.y + info2.yc);
  const ch = await win.evaluate(() => window.__app.cutHover());
  if (ch) expect(Math.abs((ch.t + off) / 0.125 - Math.round((ch.t + off) / 0.125))).toBeLessThan(0.01);
  await win.keyboard.press('1');
  await win.evaluate(() => window.__app.setGrid({ snapT: false }));
});

test('(G5) 音程スナップ: 帯の高さが半音に・ドラッグ中だけ「G4 ±0¢」', async () => {
  await win.evaluate(() => window.__app.setGrid({ snapP: true }));
  const n = await longNote();
  const band0 = (await win.evaluate(() => window.__app.shapes())).find((m) => m.id === n.id).band;
  const info = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  const r = await rollBox();
  const rowH = await win.evaluate(() => { const S = window.__app.S; const h = document.querySelector('#roll').getBoundingClientRect().height; return (h - 20 - 46) / S.pv.span; });
  const cx = r.x + (info.x0 + info.x1) / 2; const cy = r.y + info.yc;
  await win.mouse.move(cx, cy);
  await win.mouse.down();
  for (let k = 1; k <= 6; k++) await win.mouse.move(cx, cy - k * rowH * 0.12);
  // ドラッグ中: 帯は半音ちょうど、ツールチップは「音名 ±0¢」
  const mid = (await win.evaluate(() => window.__app.shapes())).find((m) => m.id === n.id).band;
  expect(Math.abs(mid - Math.round(mid))).toBeLessThan(0.002);
  const tips = await win.evaluate(() => [...document.querySelectorAll('#roll text')].map((t) => t.textContent).filter((t) => /¢$/.test(t)));
  expect(tips.some((t) => /^[A-G]#?-?\d+ ±0¢$/.test(t))).toBe(true);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-pitch-snap.png') });
  await win.mouse.up();
  await settle();
  const after = (await win.evaluate(() => window.__app.bands())).find((m) => m.id === n.id).band;
  expect(Math.abs(after - Math.round(after))).toBeLessThan(0.01);
  expect(Math.abs(after - band0)).toBeLessThan(1.01);
  // 離した後はツールチップが消える
  expect(await win.evaluate(() => [...document.querySelectorAll('#roll text')].filter((t) => /¢$/.test(t.textContent)).length)).toBe(0);
  await win.keyboard.press('Control+z');
  await settle();
  await win.evaluate(() => window.__app.setGrid({ snapP: false }));
});

test('(G6) トラックビューのクリップの位置ずらしもスナップに従う', async () => {
  await win.evaluate(() => window.__app.setGrid({ snapT: true, divBars: '1/4' }));
  const ts = await win.evaluate(() => window.__app.tracks());
  const g = ts.find((t) => t.guide) || ts[1];
  const row = ts.indexOf(ts.find((t) => t.id === g.id));
  const lanes = await win.locator('#lanes').boundingBox();
  const th = await win.evaluate(() => window.__app.trackHeight());
  const x0 = lanes.x + await win.evaluate(() => window.__app.tvX(1.0));
  const y0 = lanes.y + row * th + th * 0.78;
  await win.mouse.move(x0, y0);
  await win.mouse.down();
  const dx = (await win.evaluate(() => window.__app.tvX(1.62))) - (await win.evaluate(() => window.__app.tvX(1.0)));
  for (let k = 1; k <= 10; k++) await win.mouse.move(x0 + k * dx / 10, y0);
  // ドラッグ中（離す前）からグリッドに乗っている（ドラッグ中 = 離した後）
  const mid = await win.evaluate((id) => window.__app.S.trackOff.get(id), g.id);
  expect(Math.abs(mid / 0.5 - Math.round(mid / 0.5))).toBeLessThan(1e-6);
  await win.mouse.up();
  await settle();
  const off = (await win.evaluate(() => window.__app.tracks())).find((t) => t.id === g.id).offset_sec;
  expect(Math.abs(off)).toBeGreaterThan(0.1);
  expect(Math.abs(off / 0.5 - Math.round(off / 0.5))).toBeLessThan(1e-6);           // 120 BPM の 4 分音符 = 0.5 秒
  expect((await hist()).undo.label).toBe('トラックの位置');
  await win.keyboard.press('Control+z');
  await settle();
  await win.evaluate(() => window.__app.setGrid({ snapT: false, divBars: 'auto' }));
});

/** WAV に iXML（PreSonus のテンポマップ）のチャンクを足した写しを作る。 */
function wavWithTempo(src, dst, bpm) {
  const b = fs.readFileSync(src);
  const v = (60 / bpm).toFixed(20);
  const x = Buffer.from(`﻿<?xml version="1.0" encoding="UTF-8"?>\n<BWFXML>\n\t<PRESONUS>\n\t\t<TEMPO_MAP>`
    + `<TEMPO_SEGMENT><TEMPO_SEGMENT_OFFSET>0</TEMPO_SEGMENT_OFFSET><TEMPO_SEGMENT_VALUE>${v}</TEMPO_SEGMENT_VALUE></TEMPO_SEGMENT>`
    + `<TEMPO_SEGMENT><TEMPO_SEGMENT_OFFSET>12000</TEMPO_SEGMENT_OFFSET><TEMPO_SEGMENT_VALUE>${v}</TEMPO_SEGMENT_VALUE></TEMPO_SEGMENT>`
    + '</TEMPO_MAP>\n\t</PRESONUS>\n</BWFXML>', 'utf8');
  const pad = x.length & 1 ? Buffer.alloc(1) : Buffer.alloc(0);
  const head = Buffer.alloc(8);
  head.write('iXML', 0, 'ascii');
  head.writeUInt32LE(x.length, 4);
  const out = Buffer.concat([b, head, x, pad]);
  out.writeUInt32LE(out.length - 8, 4);
  fs.writeFileSync(dst, out);
}

test('(G7) iXML のテンポを自動で読む（「iXML」）・ルーラーの右クリック', async () => {
  // テンポを消してから（set_tempo(clear)）、テンポマップの入った伴奏を足す
  await win.evaluate(() => window.api.call('set_tempo', { clear: true, author: 'human' }));
  await win.evaluate(() => window.__app.loadSession());
  expect((await grid()).tempo).toBeNull();
  const inst = path.join(WORK, 'Inst_mix.wav');
  wavWithTempo(INST_SRC, inst, 171);
  await win.evaluate((p) => window.__app.addTrackFile(p), inst);
  await settle();
  await expect.poll(async () => (await tempoText()).bpm).toBe('171');
  const tt = await tempoText();
  expect(tt.src).toBe(true);
  expect(tt.srcTitle).toContain('Inst_mix.wav の iXML');
  expect((await grid()).tempo.source).toBe('ixml');
  await win.screenshot({ path: path.join(DOCS, 'screenshot-tempo-ixml.png'), clip: { x: 0, y: 0, width: 1400, height: 40 } });
  // 手で変えると「iXML」は消える
  await win.locator('#tBpm').click();
  await win.keyboard.press('Control+a');
  await win.keyboard.type('172');
  await win.keyboard.press('Enter');
  await settle();
  await expect.poll(async () => (await tempoText()).src).toBe(false);
  await win.keyboard.press('Control+z');
  await settle();
  await expect.poll(async () => (await tempoText()).src).toBe(true);
  // ルーラーの右クリック: テンポと拍子…・表示の切り替え
  const r = await rollBox();
  await win.mouse.click(r.x + r.width / 2, r.y + 8, { button: 'right' });
  const it = await items();
  expect(it.map((x) => [x.label, x.disabled, x.checked])).toEqual([
    ['ループを解除', true, false], ['テンポと拍子…', false, false], ['表示: 小節・拍', false, true], ['表示: 分:秒', false, false],
  ]);
  await win.locator('#menu button[data-item="fmt-sec"]').click();
  expect((await grid()).bars).toBe(false);
  for (const l of await win.evaluate(() => window.__app.rulerLabels())) expect(l).toMatch(/^\d+:\d\d/);
  await win.mouse.click(r.x + r.width / 2, r.y + 8, { button: 'right' });
  await win.locator('#menu button[data-item="fmt-bars"]').click();
  expect((await grid()).bars).toBe(true);
  await win.mouse.click(r.x + r.width / 2, r.y + 8, { button: 'right' });
  await win.locator('#menu button[data-item="tempo"]').click();
  await expect(win.locator('#popTempo')).toBeVisible();
  expect(await win.locator('#tpB').inputValue()).toBe('171');
  await win.keyboard.press('Escape');
  await expect(win.locator('#popTempo')).toBeHidden();
});

// ---------------------------------------------------------------- #20
test('(F1) フェード: つまみを内側へドラッグ → 帯がその分細く → 1 回で戻る', async () => {
  await showRange(2.2, 3.5);
  const n = await longNote();
  const info = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  const r = await rollBox();
  // ノートに乗るとつまみが 2 つ（帯の上の両端）
  await win.mouse.move(r.x + (info.x0 + info.x1) / 2, r.y + info.yc);
  let f = await win.evaluate((id) => window.__app.fadeInfo(id), n.id);
  expect(f.map((x) => x.side)).toEqual(['in', 'out']);
  expect(Math.abs(f[0].x - info.x0)).toBeLessThan(0.2);
  expect(Math.abs(f[1].x - info.x1)).toBeLessThan(0.2);
  expect(f[0].y).toBeLessThan(info.yc - 7);
  // 左のつまみを右へ 30 px
  const pxs = await win.evaluate(() => { const S = window.__app.S; return (document.querySelector('#roll').getBoundingClientRect().width - 44) / S.view.span; });
  const hx = r.x + f[0].x; const hy = r.y + f[0].y;
  await win.mouse.move(hx, hy);
  await win.mouse.down();
  for (let k = 1; k <= 6; k++) await win.mouse.move(hx + k * 5, hy);
  // ドラッグ中: 帯の頭が細い（最初の点の太さ ≈ 0）、ツールチップに ms
  const wide = await blobWidthAt(n.id, info.x0 + 2);
  const later = await blobWidthAt(n.id, info.x0 + 45);
  expect(wide).toBeLessThan(later * 0.4);
  const dragShape = await win.evaluate((id) => document.querySelector(`#roll path[data-blob="${id}"]`).getAttribute('d'), n.id);
  expect(await win.evaluate(() => [...document.querySelectorAll('#roll text')].some((t) => / ms$/.test(t.textContent)))).toBe(true);
  await win.mouse.up();
  await settle();
  const after = (await notes()).find((m) => m.id === n.id);
  expect(after.fadeIn).toBeCloseTo(30 / pxs, 2);
  expect(after.fadeOut).toBe(0);
  expect((await hist()).undo.label).toBe('フェード');
  // 離した後の帯 = ドラッグ中の帯（ドラッグ中 = 離した後）
  const afterShape = await win.evaluate((id) => document.querySelector(`#roll path[data-blob="${id}"]`).getAttribute('d'), n.id);
  expect(afterShape).toBe(dragShape);
  // 右のつまみも（ホバーし直す）
  await win.mouse.move(r.x + (info.x0 + info.x1) / 2, r.y + info.yc);
  f = await win.evaluate((id) => window.__app.fadeInfo(id), n.id);
  await win.mouse.move(r.x + f[1].x, r.y + f[1].y);
  await win.mouse.down();
  for (let k = 1; k <= 4; k++) await win.mouse.move(r.x + f[1].x - k * 5, r.y + f[1].y);
  await win.screenshot({ path: path.join(DOCS, 'screenshot-fade.png') });
  await win.mouse.up();
  await settle();
  const both = (await notes()).find((m) => m.id === n.id);
  expect(both.fadeOut).toBeCloseTo(20 / pxs, 2);
  expect(both.fadeIn).toBeCloseTo(after.fadeIn, 4);
  // 隣のノートは変わらない
  expect((await notes()).filter((m) => m.id !== n.id).every((m) => m.fadeIn === 0 && m.fadeOut === 0)).toBe(true);
  await win.keyboard.press('Control+z');
  await settle();
  expect((await notes()).find((m) => m.id === n.id).fadeOut).toBe(0);
});

async function blobWidthAt(id, x) {
  return win.evaluate(([nid, xx]) => {
    const d = document.querySelector(`#roll path[data-blob="${nid}"]`).getAttribute('d');
    const pts = [...d.split('Z')[0].matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => [+m[1], +m[2]]);
    const half = pts.length / 2;
    let best = 0; let bd = Infinity;
    for (let k = 0; k < half; k++) {
      const dd = Math.abs(pts[k][0] - xx);
      if (dd < bd) { bd = dd; best = Math.abs(pts[pts.length - 1 - k][1] - pts[k][1]); }
    }
    return best;
  }, [id, x]);
}

test('(F2) 右クリック「フェードを消す」はフェードのあるノートのときだけ', async () => {
  const n = await longNote();
  const other = (await notes()).find((m) => m.id !== n.id && m.fadeIn === 0);
  const r = await rollBox();
  const info = await win.evaluate((id) => window.__app.edgeInfo(id), n.id);
  await win.mouse.click(r.x + (info.x0 + info.x1) / 2, r.y + info.yc, { button: 'right' });
  let it = await items();
  expect(it.map((x) => x.label)).toContain('フェードを消す');
  await win.locator('#menu button[data-cmd="clear-fade"]').click();
  await settle();
  expect((await notes()).find((m) => m.id === n.id).fadeIn).toBe(0);
  expect((await hist()).undo.label).toBe('フェードを消す');
  await win.keyboard.press('Control+z');
  await settle();
  expect((await notes()).find((m) => m.id === n.id).fadeIn).toBeGreaterThan(0);
  // フェードの無いノートには出さない
  const oi = await win.evaluate((id) => window.__app.edgeInfo(id), other.id);
  if (oi.start) {
    await win.mouse.click(r.x + (oi.x0 + oi.x1) / 2, r.y + oi.yc, { button: 'right' });
    it = await items();
    expect(it.map((x) => x.label)).not.toContain('フェードを消す');
    await escape();
  }
  expect(errors).toEqual([]);
});
