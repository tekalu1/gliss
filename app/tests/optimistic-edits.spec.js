// 離した形を先に描き、エンジンに当たるのを待たない（2026-10-10 承認）。同じノートを続けて直したとき。
//
//   (O1) 同じノートを 3 回続けて動かす: 画面はいつも最後の操作の形。縞は最後の操作が当たって画面に採るまで残る。
//        エンジンへは操作の順に届き、取り消しは操作 1 回ずつ。描画データを読んでいる間に次の編集を送ったら、
//        その描画データは採らない。段階の時間を「screen op=pitch …」の 1 行で残す
//   (O2) 途中の操作が失敗: 失敗だけを知らせ、エンジンの状態を読み直し、後の操作をその上に載せ直す
//   (O3) 単体の「反映中…」は、300 ms を超えて待たされたときだけ出る
//
// エンジンの応答は main の `engine.call` の IPC を差し替えて止める・失敗させる（ほかの呼び出しは本物のエンジンへ）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-optimistic');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');
const TOL_ST = 0.02;

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  env.VOCAL_ENGINE_WORK_DIR = path.join(ROOT, 'work');
  env.VOCAL_ENGINE_LOG_DIR = path.join(ROOT, 'logs');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.evaluate(([t0, t1]) => {
    window.__app.S.view = { t0, span: t1 - t0 };
    window.__app.render();
  }, [2.2, 3.5]);
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
const shown = async (id) => (await win.evaluate(() => window.__app.shapes())).find((n) => n.id === id).pitch;
const shifts = async () => (await win.evaluate(() => window.__app.S.vd.edits)).filter((e) => e.kind === 'pitch_shift');
const status = () => win.evaluate(() => window.__app.status());
const marked = (id) => win.evaluate((nid) => ({
  syncing: window.__app.S.syncing.has(nid),
  drawn: document.querySelectorAll(`#roll [data-sync="${nid}"]`).length,
}), id);

async function dragUp(id, dy) {
  const b = await blob(id).boundingBox();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await win.mouse.down();
  await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - dy, { steps: 6 });
  await win.mouse.up();
}
async function undoAll() {
  await settle();
  for (let i = 0; i < 40; i++) {
    const h = await win.evaluate(() => window.__app.hist());
    if (!h?.can_undo) break;
    await win.evaluate(() => window.__app.undo());
    await settle();
  }
}

/** 見えている範囲でいちばん長いノート（つかみやすい）。 */
async function pickNote() {
  return win.evaluate(() => {
    const { t0, span } = window.__app.view();
    const ns = window.__app.notes().filter((n) => n.start >= t0 && n.end <= t0 + span && !n.muted);
    ns.sort((a, b) => (b.end - b.start) - (a.end - a.start));
    return ns[0]?.id || null;
  });
}

/** shift_pitch を止めておき、release() で 1 本ずつ通す。fail 番目は失敗を返す。描画データの読みも数える。
 * holdView: 次の export_view_data を止めておき、viewRelease() で返す（返す結果は view_rev に印を付ける）。 */
async function hook({ fail = 0 } = {}) {
  await app.evaluate(({ ipcMain }, failAt) => {
    const handlers = ipcMain._invokeHandlers;
    globalThis.__oReal = globalThis.__oReal || handlers.get('engine.call');
    const real = (event, name, args) => globalThis.__oReal(event, name, args);
    globalThis.__oCalls = [];
    globalThis.__oGates = [];
    globalThis.__oViews = 0;
    globalThis.__oHoldView = false;
    globalThis.__oViewGate = null;
    handlers.set('engine.call', (event, name, args) => {
      if (name === 'shift_pitch') {
        globalThis.__oCalls.push((args.notes || []).map((n) => n.cents));
        const nth = globalThis.__oCalls.length;
        return new Promise((r) => {
          let done = false;   // 通すのは 1 回だけ（unhook が残りをまとめて通すときに送り直さない）
          globalThis.__oGates.push(() => {
            if (done) return;
            done = true;
            r(nth === failAt ? { ok: false, error: '試験で失敗させた' } : real(event, name, args));
          });
        });
      }
      if (name === 'export_view_data') {
        globalThis.__oViews += 1;
        if (globalThis.__oHoldView) {
          globalThis.__oHoldView = false;
          return new Promise((r) => {
            globalThis.__oViewGate = () => {
              globalThis.__oViewGate = null;
              return Promise.resolve(real(event, name, args)).then((res) => r({ ...res, view_rev: 'stale-test' }));
            };
          });
        }
      }
      return real(event, name, args);
    });
  }, fail);
}
async function unhook() {
  await app.evaluate(({ ipcMain }) => {
    for (const g of globalThis.__oGates || []) g();
    globalThis.__oViewGate?.();
    if (globalThis.__oReal) ipcMain._invokeHandlers.set('engine.call', globalThis.__oReal);
  });
}
const calls = () => app.evaluate(() => globalThis.__oCalls.length);
/** n 本目の shift_pitch が届くまで待ち、通す。 */
async function release(n) {
  await expect.poll(calls, { timeout: 10000 }).toBeGreaterThanOrEqual(n);
  await app.evaluate((_electron, i) => globalThis.__oGates[i - 1](), n);
}

/** 画面に描いている高さ・縞・採った描画データを数 ms ごとに記録する。 */
async function startSampler(id) {
  await win.evaluate((nid) => {
    window.__oSamples = [];
    window.__oSampler = setInterval(() => {
      const n = window.__app.shapes().find((x) => x.id === nid);
      window.__oSamples.push({ t: performance.now(), pitch: n?.pitch, stale: window.__app.S.vd?.view_rev === 'stale-test' });
    }, 4);
  }, id);
}
async function stopSampler() {
  return win.evaluate(() => { clearInterval(window.__oSampler); return window.__oSamples; });
}

test('(O1) 同じノートを 3 回続けて動かす: いつも最後の形・縞は最後まで・順に届く・取り消しは 1 回ずつ', async () => {
  await undoAll();
  const id = await pickNote();
  expect(id).toBeTruthy();
  const p0 = await shown(id);
  await hook();
  try {
    const after = [];
    for (let i = 0; i < 3; i++) {
      await dragUp(id, 14);
      // 離したその時に、離した形と縞
      after.push(await shown(id));
      expect(await marked(id)).toEqual({ syncing: true, drawn: expect.any(Number) });
      expect((await marked(id)).drawn).toBeGreaterThan(0);
    }
    expect(after[0]).toBeGreaterThan(p0 + 0.05);
    expect(after[1]).toBeGreaterThan(after[0] + 0.05);
    expect(after[2]).toBeGreaterThan(after[1] + 0.05);
    const last = after[2];
    await startSampler(id);
    await release(1);
    await release(2);
    // 3 本目が届いた（前の 2 本は当たった）: 最後の操作が当たって画面に採るまで縞は残る
    await expect.poll(calls).toBe(3);
    expect((await marked(id)).syncing).toBe(true);
    expect(await app.evaluate(() => globalThis.__oViews)).toBe(0);   // 続けた編集の間は描画データを読まない
    await release(3);
    await settle();
    const samples = await stopSampler();
    expect(samples.length).toBeGreaterThan(3);
    for (const s of samples) expect(Math.abs(s.pitch - last)).toBeLessThan(TOL_ST);
    expect(await marked(id)).toEqual({ syncing: false, drawn: 0 });
    expect(Math.abs((await shown(id)) - last)).toBeLessThan(TOL_ST);
    // 操作の順に届いた（それぞれ 1 本の呼び出し・上向き）
    const sent = await app.evaluate(() => globalThis.__oCalls);
    expect(sent.map((c) => c.length)).toEqual([1, 1, 1]);
    const got = (await shifts()).map((e) => e.params.cents);
    expect(got).toEqual(sent.map((c) => c[0]));
    expect(await app.evaluate(() => globalThis.__oViews)).toBe(1);
  } finally {
    await unhook();
  }
  // 段階の時間の 1 行（秒だけ）。曲を開いた場所の screen.log にも書く
  const lines = await win.evaluate(() => window.__app.S.screenTimings.slice(-3));
  expect(lines).toHaveLength(3);
  for (const l of lines) {
    expect(l).toMatch(/^screen op=pitch queued= *\d+\.\d\d apply= *\d+\.\d\d refresh= *\d+\.\d\d total= *\d+\.\d\d$/);
  }
  await expect.poll(() => {
    const found = [];
    const walk = (d) => {
      for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        const p = path.join(d, e.name);
        if (e.isDirectory()) walk(p);
        else if (e.name === 'screen.log') found.push(fs.readFileSync(p, 'utf8'));
      }
    };
    walk(ROOT);
    return found.join('').split('\n').filter((l) => / screen op=pitch /.test(l)).length;
  }, { timeout: 5000 }).toBeGreaterThanOrEqual(3);
  // 取り消しは操作 1 回ずつ
  for (let k = 2; k >= 0; k--) {
    await win.evaluate(() => window.__app.undo());
    await settle();
    expect(await shifts()).toHaveLength(k);
  }
  expect(Math.abs((await shown(id)) - p0)).toBeLessThan(TOL_ST);
});

test('(O1) 描画データを読んでいる間に次の編集を送ったら、その描画データは採らない', async () => {
  await undoAll();
  const id = await pickNote();
  await hook();
  try {
    await app.evaluate(() => { globalThis.__oHoldView = true; });
    await dragUp(id, 14);
    await startSampler(id);
    await release(1);
    // 1 本目が当たり、列が空いて描画データを読み始めた（止めてある）
    await expect.poll(() => app.evaluate(() => !!globalThis.__oViewGate), { timeout: 10000 }).toBe(true);
    await dragUp(id, 14);
    const last = await shown(id);
    await release(2);
    await app.evaluate(() => globalThis.__oViewGate());
    await settle();
    const samples = await stopSampler();
    expect(samples.some((s) => s.stale)).toBe(false);       // 古い描画データは一度も採っていない
    expect(await win.evaluate(() => window.__app.S.vd.view_rev)).not.toBe('stale-test');
    expect(Math.abs((await shown(id)) - last)).toBeLessThan(TOL_ST);
    expect(await shifts()).toHaveLength(2);
  } finally {
    await unhook();
  }
  await undoAll();
});

test('(O2) 途中の操作が失敗: 失敗だけを知らせ、読み直した上に後の操作を載せ直す', async () => {
  await undoAll();
  const id = await pickNote();
  const p0 = await shown(id);
  await hook({ fail: 2 });
  try {
    const after = [];
    for (let i = 0; i < 3; i++) {
      await dragUp(id, 14);
      after.push(await shown(id));
    }
    const d1 = after[0] - p0;
    const d3 = after[2] - after[1];
    await release(1);
    await release(2);                                   // 2 本目は失敗する
    await expect.poll(status, { timeout: 10000 }).toContain('反映できなかった');
    expect(await status()).toContain('試験で失敗させた');
    // 失敗した操作だけを外した形（1 本目と 3 本目）
    await expect.poll(() => shown(id), { timeout: 5000 }).toBeCloseTo(p0 + d1 + d3, 1);
    await startSampler(id);
    await release(3);                                   // 読み直した後で、3 本目をその上に当てる
    await settle();
    const samples = await stopSampler();
    for (const s of samples) expect(Math.abs(s.pitch - (p0 + d1 + d3))).toBeLessThan(TOL_ST);
    const sent = await app.evaluate(() => globalThis.__oCalls);
    expect(sent).toHaveLength(3);
    expect((await shifts()).map((e) => e.params.cents)).toEqual([sent[0][0], sent[2][0]]);
    expect(Math.abs((await shown(id)) - (p0 + d1 + d3))).toBeLessThan(TOL_ST);
    expect(await marked(id)).toEqual({ syncing: false, drawn: 0 });
    // 知らせたのは失敗の 1 つだけ（成功した操作は黙って差し替える）
    expect(await status()).toMatch(/^反映できなかった（[^）]*）: [^\n]*試験で失敗させた/);
  } finally {
    await unhook();
  }
  // 取り消しは当たった 2 つの操作の分だけ
  for (let k = 1; k >= 0; k--) {
    await win.evaluate(() => window.__app.undo());
    await settle();
    expect(await shifts()).toHaveLength(k);
  }
  expect(Math.abs((await shown(id)) - p0)).toBeLessThan(TOL_ST);
});

test('(O3) 単体の「反映中…」は 300 ms を超えて待つときだけ出る', async () => {
  await undoAll();
  const id = await pickNote();
  const chip = win.locator('#syncChip');
  await hook();
  try {
    await dragUp(id, 14);
    await expect(chip).toBeHidden();
    await win.waitForTimeout(150);
    await expect(chip).toBeHidden();
    await expect(chip).toBeVisible({ timeout: 2000 });
    await expect(chip).toHaveText('反映中…');
    await release(1);
    await settle();
    await expect(chip).toBeHidden();
  } finally {
    await unhook();
  }
  // 待たされなかった編集では出さない
  await dragUp(id, 14);
  await settle();
  await expect(chip).toBeHidden();
  await undoAll();
});

test('コンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
