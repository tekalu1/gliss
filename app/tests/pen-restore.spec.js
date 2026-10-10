// ペンの右ドラッグ = 元のピッチに戻す（2026-10-10 承認）。音声は合成（素材は要らない）。
//
//   (R1) 手で描いた線の上を右ドラッグ: 離すまでプレビュー（録音のピッチ）が出て、離すと 1 手で戻る。メニューは出ない
//   (R2) 動かさずに右クリック・3 px だけ動かして離す: 今までどおりメニュー。4 px 以上は戻す線（メニューは出ない）
//   (R3) 戻すものが無いところ（編集なし・無声）: 何も積まない
//   (R4) 戻した後に取り消すと、描いた線が戻る
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
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-pen-restore-'));
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
const editsOf = () => win.evaluate(() => (window.__app.S.vd.edits || []).filter((e) => e.kind === 'pitch_draw').map((e) => ({ id: e.id, restore: !!e.params?.restore })));
const menuShown = () => win.evaluate(() => !document.querySelector('#menu').hidden);
const frameAt = (fr, t) => Math.round((t - fr.t0) / fr.hop);

async function dragRight(g, pts) {
  await win.mouse.move(...pts[0]);
  await win.mouse.down({ button: 'right' });
  for (const p of pts.slice(1)) await win.mouse.move(...p, { steps: 1 });
}
const line = (g, t0, t1, m, n = 30) => Array.from({ length: n + 1 }, (_, k) => toClient(g, t0 + (t1 - t0) * k / n, m));

test('R1 手で描いた線の上を右ドラッグすると、録音のピッチに戻る（メニューは出ない）', async () => {
  const g = await geom();
  // まず左ドラッグで 0.8〜1.8 s を +2 半音の高さに描く
  const pts = line(g, 0.8, 1.8, 59, 40);
  await win.mouse.move(...pts[0]); await win.mouse.down();
  for (const p of pts.slice(1)) await win.mouse.move(...p);
  await win.mouse.up();
  await settle();
  const fr = await win.evaluate(() => window.__app.f0Frame());
  const orig = await win.evaluate(() => window.__app.origCurve());
  const k = frameAt(fr, 1.2);
  let eng = await win.evaluate(() => window.__app.engineCurve());
  expect(eng[k] - orig[k]).toBeGreaterThan(1.5);
  const before = await editsOf();

  // 1.0〜1.4 s を右ドラッグ（途中）: プレビュー
  await dragRight(g, line(g, 1.0, 1.4, 58, 20));
  const mid = await win.evaluate(() => ({ restore: !!window.__app.S.stroke?.restore, cur: window.__app.editedCurve() }));
  expect(mid.restore).toBe(true);
  expect(Math.abs(mid.cur[k] - orig[k])).toBeLessThan(0.001);
  expect(await win.locator('[data-restore-band]').count()).toBe(1);
  expect(await win.locator('[data-restore-label]').textContent()).toContain('元のピッチに戻す');
  // 離す
  await win.mouse.up({ button: 'right' });
  await win.waitForTimeout(200);
  expect(await menuShown()).toBe(false);
  await settle();
  eng = await win.evaluate(() => window.__app.engineCurve());
  expect(Math.abs(eng[k] - orig[k])).toBeLessThan(0.005);                        // 区間の中は録音のまま
  const kOut = frameAt(fr, 1.6);
  expect(eng[kOut] - orig[kOut]).toBeGreaterThan(1.5);                           // 区間の外は描いた線のまま
  const after = await editsOf();
  expect(after.length).toBe(before.length + 1);
  expect(after.filter((e) => e.restore).length).toBe(1);
  const hist = await win.evaluate(() => window.__app.hist());
  expect(JSON.stringify(hist)).toContain('元に戻した');
});

test('R4 取り消すと描いた線が戻る（1 手）', async () => {
  const fr = await win.evaluate(() => window.__app.f0Frame());
  const orig = await win.evaluate(() => window.__app.origCurve());
  const k = frameAt(fr, 1.2);
  await win.evaluate(() => window.__app.undo());
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
  const eng = await win.evaluate(() => window.__app.engineCurve());
  expect(eng[k] - orig[k]).toBeGreaterThan(1.5);
  expect((await editsOf()).some((e) => e.restore)).toBe(false);
});

test('R2 動かさない右クリック・3 px はメニュー、4 px 以上は戻す線', async () => {
  const g = await geom();
  const [x, y] = toClient(g, 1.2, 58);
  const before = (await editsOf()).length;
  for (const dx of [0, 3]) {
    await win.mouse.move(x, y);
    await win.mouse.down({ button: 'right' });
    if (dx) await win.mouse.move(x + dx, y);
    await win.mouse.up({ button: 'right' });
    await win.waitForTimeout(150);
    expect(await menuShown(), `dx=${dx}`).toBe(true);
    await win.keyboard.press('Escape');
    await win.waitForTimeout(100);
  }
  expect((await editsOf()).length).toBe(before);
  // 4 px: 戻す線（描いた線の上）。メニューは出ない
  await win.mouse.move(x, y);
  await win.mouse.down({ button: 'right' });
  await win.mouse.move(x + 4, y);
  await win.mouse.move(x + 30, y);
  await win.mouse.up({ button: 'right' });
  await win.waitForTimeout(200);
  expect(await menuShown()).toBe(false);
  await settle();
  expect((await editsOf()).length).toBe(before + 1);
  // 続けて右クリック（動かさない）は、また普通にメニュー
  await win.mouse.move(x, y);
  await win.mouse.click(x, y, { button: 'right' });
  await win.waitForTimeout(200);
  expect(await menuShown()).toBe(true);
  await win.keyboard.press('Escape');
  await win.evaluate(() => window.__app.undo());
  await win.waitForFunction(() => window.__app.idle(), null, { timeout: 60000 });
});

test('R3 戻すものが無いところ（録音のまま・無声）は何も積まない', async () => {
  const g = await geom();
  const before = (await editsOf()).length;
  const histN = await win.evaluate(() => (window.__app.hist()?.changesets || []).length);
  await dragRight(g, line(g, 2.33, 2.39, 57, 10));                 // 音が終わった後（無声）
  await win.mouse.up({ button: 'right' });
  await win.waitForTimeout(250);
  expect(await win.evaluate(() => window.__app.status())).toContain('無声のところは録音のまま');
  expect(await win.evaluate(() => !!window.__app.S.stroke)).toBe(false);
  await dragRight(g, line(g, 0.45, 0.7, 57, 10));                  // 有声だが、編集していない
  await win.mouse.up({ button: 'right' });
  await win.waitForTimeout(250);
  expect(await win.evaluate(() => window.__app.status())).toContain('録音のピッチのまま');
  expect(await win.evaluate(() => !!window.__app.S.stroke)).toBe(false);
  expect((await editsOf()).length).toBe(before);
  expect(await win.evaluate(() => (window.__app.hist()?.changesets || []).length)).toBe(histN);
  // 戻した区間をもう一度なぞる = 変化なし（描いた線は 0.8〜1.8 s にある）
  await dragRight(g, line(g, 1.2, 1.5, 57, 10));
  await win.mouse.up({ button: 'right' });
  await settle();
  const once = (await editsOf()).length;
  const onceHist = await win.evaluate(() => (window.__app.hist()?.changesets || []).length);
  expect(once).toBe(before + 1);
  await dragRight(g, line(g, 1.2, 1.5, 57, 10));
  await win.mouse.up({ button: 'right' });
  await win.waitForTimeout(250);
  expect(await win.evaluate(() => window.__app.status())).toContain('録音のピッチのまま');
  expect((await editsOf()).length).toBe(once);
  expect(await win.evaluate(() => (window.__app.hist()?.changesets || []).length)).toBe(onceHist);
});

test('エラーが出ていない', () => {
  expect(errors).toEqual([]);
});
