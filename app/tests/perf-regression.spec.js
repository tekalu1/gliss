// 編集の速さが戻っていないか（2026-10-10 承認の画面の改修の後の速さを基準にする）。長い曲（SONG）で計る。
//
//   (P1) 1 回の編集: 離してから、エンジンに当たって画面に採り終わるまで
//   (P2) 3 回続けた編集: 最後に離してから、3 回とも当たって画面に採り終わるまで（ドラッグの間の時間はテストの道具の速さで揺れるので含めない）
//   (P3) 編集の直後の再生: 離した直後に再生を押してから、鳴り始めるまで
//
// それぞれ 3 回計った中央値を、改修の後に計った値（同じテストを 3 回流した中央値）の 1.5 倍と比べる。
// 計った値は「perf …」の 1 行でテストの出力に出す（基準を取り直すときに使う）。
// 裏で再生の音を作る（audio.js の warm）間に次を始めないよう、エンジンへの呼び出しが落ち着くまで待ってから計る。
// 開いた直後の裏の準備と、最初の編集だけにかかる読み込みは計らない（準備が済むのを待ち、1 回当てて戻してから計る）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'SONG');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-perf');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');
const REPS = 3;
const SPAN = 4;

// 改修の後の中央値（ミリ秒。2026-10-11、このテストを 3 回流した中央値の中央値）。上限はこの 1.5 倍
const BASE_MS = { edit1: 124, edit3: 77, play: 351 };
const LIMIT = 1.5;

let app;
let win;
let target;
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
  fs.copyFileSync(M.clip('SONG'), TAKE);
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT, '--user-data-dir', USERDATA, '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready() && window.__app.S.pitched?.length > 0, null, { timeout: 600000 });
  // エンジンへの呼び出しを数える（中身は変えない）: 裏の音作りが終わったかを見る
  await app.evaluate(({ ipcMain }) => {
    const handlers = ipcMain._invokeHandlers;
    const real = handlers.get('engine.call');
    globalThis.__pBusy = 0;
    globalThis.__pLast = Date.now();
    handlers.set('engine.call', async (event, name, args) => {
      globalThis.__pBusy += 1;
      try { return await real(event, name, args); } finally {
        globalThis.__pBusy -= 1;
        globalThis.__pLast = Date.now();
      }
    });
  });
  // 画面の idle の変わり目を、ページの時計で記録する
  await win.evaluate(() => {
    const pr = window.__pr = { ev: [], idle: [], playing: [] };
    for (const t of ['pointerdown', 'pointerup']) window.addEventListener(t, () => pr.ev.push([t, performance.now()]), true);
    let last = null;
    let lastPlaying = false;
    const poll = () => {
      const v = !!(window.__app.idle() && !window.__app.S.opening);
      if (v !== last) { pr.idle.push([performance.now(), v]); last = v; }
      const p = !!window.__app.S.playing;
      if (p !== lastPlaying) { pr.playing.push([performance.now(), p]); lastPlaying = p; }
      setTimeout(poll, 2);
    };
    poll();
  });
  // 曲の中ほど（55% あたり）の、つかみやすい長さのノート
  target = await win.evaluate(() => {
    const ps = window.__app.S.pitched.filter((n) => n.kind === 'note' && n.pitch_editable !== false
      && n.end_sec - n.start_sec > 0.18);
    const want = window.__app.totalSec() * 0.55;
    ps.sort((a, b) => Math.abs((a.start_sec + a.end_sec) / 2 - want) - Math.abs((b.start_sec + b.end_sec) / 2 - want));
    return ps[0]?.id || null;
  });
  expect(target).toBeTruthy();
  await expect.poll(async () => {
    const r = await win.evaluate(() => window.api.call('prep_status', {}));
    return !r.running && (r.tracks || []).every((t) => t.state === 'ready');
  }, { timeout: 600000, intervals: [500] }).toBe(true);
  await show();
  await quiet();
  await drag(-20);
  await idleSince(0);
  await undo(1);
});

test.afterAll(async () => {
  await app?.close();
});

/** 画面が idle で、エンジンへの呼び出しも 800 ms 来ていない（裏の音作りも済んだ）。 */
async function quiet() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening && !window.__app.S.playing,
    null, { timeout: 300000 });
  await expect.poll(() => app.evaluate(() => globalThis.__pBusy === 0 && Date.now() - globalThis.__pLast > 800),
    { timeout: 300000, intervals: [100] }).toBe(true);
}
const now = () => win.evaluate(() => performance.now());
/** t より後で、最後に idle になった時刻（その後ずっと idle）。 */
async function idleSince(t) {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 300000, polling: 5 });
  return win.evaluate((m) => {
    const xs = window.__pr.idle.filter(([at, v]) => at >= m && v);
    return xs.length ? xs[xs.length - 1][0] : null;
  }, t);
}
/** t より後の、最後の kind（pointerup など）の時刻。 */
const lastEvent = (t, kind) => win.evaluate(([m, k]) => {
  const xs = window.__pr.ev.filter(([e, at]) => e === k && at >= m);
  return xs.length ? xs[xs.length - 1][1] : null;
}, [t, kind]);

async function show() {
  await win.evaluate(([nid, sp]) => {
    const n = window.__app.S.pitched.find((q) => q.id === nid);
    const mid = (n.start_sec + n.end_sec) / 2;
    window.__app.S.view = { t0: Math.max(0, mid - sp / 2), span: sp };
    window.__app.render();
  }, [target, SPAN]);
}
async function drag(dy) {
  const b = await win.locator(`#roll rect[data-note="${target}"]:not([data-edge])`).first().boundingBox();
  const x = b.x + b.width / 2; const y = b.y + b.height / 2;
  await win.mouse.move(x, y);
  await win.mouse.down();
  await win.mouse.move(x, y + dy, { steps: 4 });
  await win.mouse.up();
}
async function undo(n) {
  for (let i = 0; i < n; i++) {
    await win.evaluate(() => window.__app.undo());
    await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 300000 });
  }
}
const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
function judge(key, xs) {
  const m = median(xs);
  console.log(`perf ${key} median=${m.toFixed(0)} runs=${xs.map((x) => x.toFixed(0)).join(',')} base=${BASE_MS[key]}`);
  expect(m).toBeLessThan(BASE_MS[key] * LIMIT);
}

test('(P1) 1 回の編集が当たって画面に採るまで', async () => {
  const xs = [];
  for (let i = 0; i < REPS; i++) {
    await show();
    await quiet();
    const m = await now();
    await drag(-(18 + 3 * i));
    const up = await lastEvent(m, 'pointerup');
    const done = await idleSince(m);
    xs.push(done - up);
    await undo(1);
  }
  judge('edit1', xs);
});

test('(P2) 3 回続けた編集の最後を離してから、全部当たって画面に採るまで', async () => {
  const xs = [];
  for (let i = 0; i < REPS; i++) {
    await show();
    await quiet();
    const m = await now();
    for (let k = 0; k < 3; k++) await drag((k % 2 ? 1 : -1) * (12 + 2 * i));
    const up = await lastEvent(m, 'pointerup');
    const done = await idleSince(m);
    xs.push(done - up);
    await undo(3);
  }
  judge('edit3', xs);
});

test('(P3) 編集の直後に再生を押してから鳴り始めるまで', async () => {
  const xs = [];
  for (let i = 0; i < REPS; i++) {
    await show();
    await quiet();
    await drag(-(18 + 3 * i));
    const at = await win.evaluate(() => {
      window.__app.S.head = window.__app.S.view.t0;
      const t = performance.now();
      window.__app.runCommand('play');
      return t;
    });
    await win.waitForFunction(() => window.__app.S.playing, null, { timeout: 300000, polling: 5 });
    const began = await win.evaluate((m) => window.__pr.playing.find(([t, v]) => v && t >= m)?.[0], at);
    xs.push(began - at);
    await win.evaluate(() => { if (window.__app.S.playing) window.__app.runCommand('play'); });
    await win.waitForFunction(() => !window.__app.S.playing, null, { timeout: 20000 });
    await undo(1);
  }
  judge('play', xs);
});

test('コンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});
