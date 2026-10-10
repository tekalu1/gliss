// ペンの Shift = 半音に沿って書く（音程スナップ XOR Shift。2026-10-10 承認）。音声は合成（素材は要らない）。
//
//   (S1) Shift を押して書くと、線が半音の行の中心に乗る。押さなければ自由
//   (S2) 隣の半音へは 50 ms の余弦でつなぐ（段にならない）。境目では余裕を持つ（行き来しない）
//   (S3) 書いている途中に Shift を押す・離すと、自由 ⇄ 半音を 50 ms でつなぐ
//   (S4) 音程スナップをオンにすると反転: 押さなければ半音、押すと自由
//   (S5) 先端の目印: 行の中心の線・鍵盤の行・音名・札。OS のカーソルは隠す
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SR = 44100;
const NOTE = [0.3, 2.3, 220];       // 2 秒の長い音（A3）
const KW = 44; const ST = 20; const LH = 46;

function writeTone(file) {
  const n = Math.round(SR * 2.6);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    const t = i / SR;
    let v = 0;
    if (t >= NOTE[0] && t < NOTE[1]) {
      const env = Math.min(1, (t - NOTE[0]) / 0.03, (NOTE[1] - t) / 0.03);
      for (let k = 1; k <= 6; k++) v += Math.sin(2 * Math.PI * NOTE[2] * k * t) / k;
      v *= 0.25 * env;
    }
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

let app;
let win;
let dir;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-pen-semitone-'));
  const wav = path.join(dir, 'tone.wav');
  writeTone(wav);
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_EDITOR_MUTE: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '0', VOCAL_ENGINE_WORK_DIR: path.join(dir, 'work'),
    VOCAL_ENGINE_PROJECTS: path.join(dir, 'projects') };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({
    args: [APP, wav, '--project-dir', path.join(dir, 'project'), '--user-data-dir', path.join(dir, 'userdata'), '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.waitForFunction(() => window.__app.notes().length >= 1, null, { timeout: 120000 });
  await win.evaluate(() => window.__app.setTool('draw'));
  await win.evaluate(() => {
    const S = window.__app.S;
    S.view = { t0: 0.4, span: 2.0 };
    S.pv = { top: 57 + 6, span: 12 };
    window.__app.render();
  });
  await win.waitForTimeout(150);
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

const settle = () => win.waitForFunction(() => window.__app.idle() && !window.__app.S.stroke, null, { timeout: 120000 }).catch(() => {});
const geom = () => win.evaluate(() => {
  const r = document.querySelector('#roll').getBoundingClientRect();
  const S = window.__app.S;
  return { left: r.left, top: r.top, W: r.width, H: r.height, t0: S.view.t0, span: S.view.span, ptop: S.pv.top, pspan: S.pv.span };
});
const rowHOf = (g) => (g.H - LH - ST) / g.pspan;
const toClient = (g, t, m) => [g.left + KW + (t - g.t0) / g.span * (g.W - KW), g.top + ST + (g.ptop - m) * rowHOf(g)];
const frameAt = (fr, t) => Math.round((t - fr.t0) / fr.hop);
const valsOf = () => win.evaluate(() => Object.fromEntries([...window.__app.S.stroke.vals]));
const hudOf = () => win.evaluate(() => ({
  cls: document.querySelector('#roll').getAttribute('class'),
  hud: document.querySelector('[data-pen-hud]')?.getAttribute('data-pen-hud') ?? null,
  label: document.querySelector('[data-pen-label] text')?.textContent ?? null,
  name: document.querySelector('[data-pen-name] text')?.textContent ?? null,
  glyph: !!document.querySelector('[data-pen-glyph]'),
}));

/** 点列 [[t, m], ...] を左ドラッグで送る。途中で呼びたいことは hooks[添字] で。最後は離さない。 */
async function stroke(g, pts, hooks = {}) {
  await win.mouse.move(...toClient(g, ...pts[0]));
  await win.mouse.down();
  for (let i = 1; i < pts.length; i++) {
    if (hooks[i]) await hooks[i]();
    await win.mouse.move(...toClient(g, ...pts[i]));
  }
}
const flat = (t0, t1, m, step = 0.01) => {
  const o = [];
  for (let k = 0; t0 + k * step <= t1 + 1e-9; k++) o.push([+(t0 + k * step).toFixed(4), m]);
  return o;
};
async function finish() {
  await win.mouse.up();
  await settle();
  await win.evaluate(() => window.__app.undo());
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
}
const near = (v, w, eps = 1e-6) => Math.abs(v - w) < eps;

test('S1 Shift を押して書くと半音の行の中心に乗る（押さなければ自由）', async () => {
  const g = await geom();
  const fr = await win.evaluate(() => window.__app.f0Frame());
  // 押さない: 自由
  await stroke(g, flat(0.8, 1.4, 58.3));
  let v = await valsOf();
  expect(near(v[frameAt(fr, 1.1)], 58.3, 0.02)).toBe(true);
  await finish();
  // Shift: 58.3 → 58（行の中心）
  await win.keyboard.down('Shift');
  await stroke(g, flat(0.8, 1.4, 58.3));
  v = await valsOf();
  for (let t = 0.85; t <= 1.35; t += 0.05) expect(near(v[frameAt(fr, t)], 58), `t=${t}`).toBe(true);
  await finish();
  await win.keyboard.up('Shift');
  // 傾き 57.2 → 59.8 を Shift で: 大半は整数の行の中心（残りは隣へ移る途中）
  await win.keyboard.down('Shift');
  const pts = [];
  for (let i = 0; i <= 60; i++) pts.push([0.8 + i * 0.01, 57.2 + 2.6 * i / 60]);
  await stroke(g, pts);
  v = await valsOf();
  const ks = Object.keys(v).map(Number);
  const onRow = ks.filter((k) => near(v[k], Math.round(v[k]))).length;
  expect(onRow / ks.length).toBeGreaterThan(0.6);
  await finish();
  await win.keyboard.up('Shift');
});

test('S2 隣の半音へは 50 ms の余弦でつなぐ。境目では行き来しない', async () => {
  const g = await geom();
  const fr = await win.evaluate(() => window.__app.f0Frame());
  await win.keyboard.down('Shift');
  // 58.2 → 1.00〜1.02 s で 59.2 へ跳ぶ
  const pts = [...flat(0.8, 1.0, 58.2), [1.02, 59.2], ...flat(1.03, 1.4, 59.2)];
  await stroke(g, pts);
  const v = await valsOf();
  expect(near(v[frameAt(fr, 0.95)], 58)).toBe(true);
  expect(near(v[frameAt(fr, 1.1)], 59)).toBe(true);
  const mid = [];
  for (let k = 0; k <= 7; k++) mid.push(v[frameAt(fr, 1.0) + k]);
  expect(mid.filter((x) => x > 58.02 && x < 58.98).length).toBeGreaterThanOrEqual(3);   // 段ではなく、数フレームかけて動く
  for (let i = 1; i < mid.length; i++) expect(mid[i]).toBeGreaterThanOrEqual(mid[i - 1] - 1e-9);
  await finish();
  // 境目の手前（0.6 半音）では動かない。戻っても動かない（行き来しない）
  const pts2 = [...flat(0.8, 0.9, 58.2), ...flat(0.9, 1.0, 58.6), ...flat(1.0, 1.1, 58.4), ...flat(1.1, 1.2, 58.6),
    ...flat(1.2, 1.3, 59.0), ...flat(1.3, 1.4, 58.4)];
  await stroke(g, pts2);
  const w = await valsOf();
  for (const t of [0.95, 1.05, 1.15]) expect(near(w[frameAt(fr, t)], 58), `t=${t}`).toBe(true);   // 58.6 でも 58（四捨五入なら 59）
  expect(near(w[frameAt(fr, 1.28)], 59)).toBe(true);                                              // 59.0 まで来たら移る
  expect(near(w[frameAt(fr, 1.38)], 59)).toBe(true);                                              // 58.4 に戻っても 59 のまま
  await finish();
  await win.keyboard.up('Shift');
});

test('S3 書いている途中に Shift を押す・離すと、自由 ⇄ 半音を 50 ms でつなぐ', async () => {
  const g = await geom();
  const fr = await win.evaluate(() => window.__app.f0Frame());
  const pts = flat(0.8, 1.6, 58.4);
  const idx = (t) => pts.findIndex((p) => Math.abs(p[0] - t) < 1e-6);
  await stroke(g, pts, {
    [idx(1.0)]: () => win.keyboard.down('Shift'),
    [idx(1.3)]: () => win.keyboard.up('Shift'),
  });
  const v = await valsOf();
  expect(near(v[frameAt(fr, 0.95)], 58.4, 0.02)).toBe(true);             // 押す前は自由
  expect(near(v[frameAt(fr, 1.12)], 58)).toBe(true);                     // 押している間は行の中心
  expect(near(v[frameAt(fr, 1.45)], 58.4, 0.02)).toBe(true);             // 離した後は自由
  const down = [];
  for (let k = 0; k <= 6; k++) down.push(v[frameAt(fr, 1.0) + k]);
  expect(down.filter((x) => x < 58.38 && x > 58.02).length).toBeGreaterThanOrEqual(3);   // 押した直後は段にならない
  const up = [];
  for (let k = 0; k <= 6; k++) up.push(v[frameAt(fr, 1.3) + k]);
  expect(up.filter((x) => x > 58.02 && x < 58.38).length).toBeGreaterThanOrEqual(3);
  await finish();
});

test('S4 音程スナップをオンにすると反転: 押さなければ半音、押すと自由', async () => {
  const g = await geom();
  const fr = await win.evaluate(() => window.__app.f0Frame());
  await win.locator('#bSnapP').click();
  await expect(win.locator('#bSnapP')).toHaveAttribute('aria-pressed', 'true');
  await stroke(g, flat(0.8, 1.4, 58.3));
  let v = await valsOf();
  expect(near(v[frameAt(fr, 1.1)], 58)).toBe(true);
  await finish();
  await win.keyboard.down('Shift');
  await stroke(g, flat(0.8, 1.4, 58.3));
  v = await valsOf();
  expect(near(v[frameAt(fr, 1.1)], 58.3, 0.02)).toBe(true);
  await finish();
  await win.keyboard.up('Shift');
});

test('S5 先端の目印: 行の中心の線・鍵盤の行・音名・札。カーソルは隠す', async () => {
  // いまは音程スナップがオン（S4 の続き）。ポインタをピアノロールに置く
  const g = await geom();
  await win.mouse.move(...toClient(g, 1.2, 58.3));
  await win.waitForTimeout(100);
  let h = await hudOf();
  expect(h.hud).toBe('58');
  expect(h.name).toBe('A#3');
  expect(h.label).toBe('音程スナップ: オン');
  expect(h.glyph).toBe(true);
  expect(h.cls).toContain('pen-snap');
  // Shift を押すと反転（半音に沿わない）: 目印は消え、札だけ
  await win.keyboard.down('Shift');
  await win.waitForTimeout(100);
  h = await hudOf();
  expect(h.hud).toBeNull();
  expect(h.label).toBe('Shift: 半音に沿わない');
  expect(h.cls).not.toContain('pen-snap');
  await win.keyboard.up('Shift');
  // オフに戻す。押すと出る
  await win.locator('#bSnapP').click();
  await expect(win.locator('#bSnapP')).toHaveAttribute('aria-pressed', 'false');
  await win.mouse.move(...toClient(g, 1.2, 58.7));
  await win.waitForTimeout(100);
  h = await hudOf();
  expect(h.hud).toBeNull();
  expect(h.label).toBeNull();
  await win.keyboard.down('Shift');
  await win.waitForTimeout(100);
  h = await hudOf();
  expect(h.hud).toBe('59');                                              // 58.7 → 59 の行
  expect(h.name).toBe('B3');
  expect(h.label).toBe('Shift: 半音に沿う');
  expect(h.cls).toContain('pen-snap');
  // 鍵盤の上では出さない（矢印のまま）
  await win.mouse.move(g.left + 20, g.top + ST + 100);
  await win.waitForTimeout(100);
  h = await hudOf();
  expect(h.hud).toBeNull();
  expect(h.cls).not.toContain('pen-snap');
  await win.keyboard.up('Shift');
});

test('エラーが出ていない', () => {
  expect(errors).toEqual([]);
});
