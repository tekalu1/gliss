// 保存・競合の読み直しと順番待ち、複数の呼び出しでできた操作の競合、準備の印のポーリングを検証する（#63）。
//
//   (Q1) 保存（save_project）を待っている間は、順番待ちの次の操作を始めない。保存が終わったら始める
//   (Q2) 保存が競合したら、読み直し（list_tracks・描画データ）が終わるまで次の操作を始めず、保存の後に入った操作も捨てる
//   (Q3) 複数ノートのピッチ・結合で、途中の呼び出しが競合したら「一部だけ当たった可能性がある」と伝える
//        （最初の呼び出しの競合は、今までどおり「今の操作は当てていない」）
//   (Q4) 閉じたセッションの prep_status が遅れて失敗しても、ポーリングを続けない。準備中のある新しいセッションに
//        変わっても、予約は 1 本だけ
//
// エンジンの応答は main の `engine.call` の IPC を差し替えて止める・作る（ほかの呼び出しは本物のエンジンへ）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-conflict-queue');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  env.VOCAL_ENGINE_WORK_DIR = path.join(ROOT, 'work');
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
});

test.afterAll(async () => {
  await app?.close();
});

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 240000 });
}
const status = () => win.evaluate(() => window.__app.status());
const edits = () => win.evaluate(() => window.__app.S.vd.edits);

/** main の engine.call を差し替える。handler(name, args, real) を main のプロセスで評価する文字列で渡す。 */
async function hook(src) {
  await app.evaluate(({ ipcMain }, code) => {
    const handlers = ipcMain._invokeHandlers;
    globalThis.__realCall = globalThis.__realCall || handlers.get('engine.call');
    const real = (event, name, args) => globalThis.__realCall(event, name, args);
    // eslint-disable-next-line no-new-func
    const fn = new Function('real', `return ${code}`)(real);
    handlers.set('engine.call', (event, name, args) => fn(event, name, args));
  }, src);
}
async function unhook() {
  await app.evaluate(({ ipcMain }) => {
    if (globalThis.__realCall) ipcMain._invokeHandlers.set('engine.call', globalThis.__realCall);
    for (const k of Object.keys(globalThis).filter((x) => x.startsWith('__q'))) delete globalThis[k];
  });
}

/** 順番待ちに印の操作を入れる（await しない）。始まったら window.__qStarted が true になる。 */
async function enqueueProbe() {
  await win.evaluate(async () => {
    const { enqueue } = await import('./edits.js');
    window.__qStarted = false;
    window.__qProbe = enqueue(() => { window.__qStarted = true; return true; });
  });
}

test('(Q1) 保存を待っている間は、次の操作を始めない', async () => {
  await settle();
  await hook(`(event, name, args) => name === 'save_project'
    ? new Promise((r) => { globalThis.__qRelease = () => r({ ok: true, saved: 'test.gliss' }); })
    : real(event, name, args)`);
  try {
    await win.evaluate(() => {
      window.__qDoc = window.__app.S.doc;
      window.__app.S.doc = { kind: 'gliss', name: 'test', path: 'test.gliss' };
      window.__qSave = window.__app.saveDoc();
    });
    await expect.poll(() => app.evaluate(() => !!globalThis.__qRelease), { timeout: 5000 }).toBe(true);
    await enqueueProbe();
    await win.waitForTimeout(300);
    expect(await win.evaluate(() => window.__qStarted)).toBe(false);   // 保存の終わりを待っている
    expect(await win.evaluate(() => window.__app.S.busy)).toBe(true);
    await app.evaluate(() => globalThis.__qRelease());
    expect(await win.evaluate(() => window.__qSave)).toBe(true);
    expect(await win.evaluate(() => window.__qProbe)).toBe(true);
    expect(await win.evaluate(() => window.__qStarted)).toBe(true);     // 保存の後で始まった
  } finally {
    await unhook();
    await win.evaluate(() => { window.__app.S.doc = window.__qDoc; });
  }
  await settle();
});

test('(Q2) 保存が競合したら、読み直しが終わるまで次を始めず、保存の後に入った操作も捨てる', async () => {
  await settle();
  await hook(`(event, name, args) => {
    if (name === 'save_project') {
      return new Promise((r) => { globalThis.__qSaveRelease = () => r({ ok: false, conflict: true, error: 'conflict' }); });
    }
    if (name === 'list_tracks' && globalThis.__qHoldList) {
      globalThis.__qHoldList = false;
      return new Promise((r) => { globalThis.__qListRelease = () => r(real(event, name, args)); });
    }
    return real(event, name, args);
  }`);
  try {
    await app.evaluate(() => { globalThis.__qHoldList = true; });
    await win.evaluate(() => {
      window.__qDoc = window.__app.S.doc;
      window.__app.S.doc = { kind: 'gliss', name: 'test', path: 'test.gliss' };
      window.__qSave = window.__app.saveDoc();
    });
    await expect.poll(() => app.evaluate(() => !!globalThis.__qSaveRelease), { timeout: 5000 }).toBe(true);
    await enqueueProbe();                          // 保存が始まった後に入った操作
    await app.evaluate(() => globalThis.__qSaveRelease());
    await expect.poll(() => app.evaluate(() => !!globalThis.__qListRelease), { timeout: 5000 }).toBe(true);
    await win.waitForTimeout(300);
    expect(await win.evaluate(() => window.__qStarted)).toBe(false);   // 読み直しの途中で始まらない
    expect(await win.evaluate(() => window.__app.S.busy)).toBe(true);
    await app.evaluate(() => globalThis.__qListRelease());
    expect(await win.evaluate(() => window.__qSave)).toBe(false);
    expect(await win.evaluate(() => window.__qProbe)).toBeNull();      // 捨てた
    expect(await win.evaluate(() => window.__qStarted)).toBe(false);
    expect(await status()).toContain('今の操作は当てていない');
  } finally {
    await unhook();
    await win.evaluate(() => { window.__app.S.doc = window.__qDoc; });
  }
  await settle();
  // 競合の後も、次の操作はふつうに当たる
  await enqueueProbe();
  expect(await win.evaluate(() => window.__qProbe)).toBe(true);
});

/** n 回目の呼び出し（name）だけ競合を返す。 */
async function conflictOn(name, nth) {
  await app.evaluate(() => { globalThis.__qCount = 0; });
  await hook(`(event, n, args) => {
    if (n === ${JSON.stringify(name)} && ++globalThis.__qCount === ${nth}) {
      return { ok: false, conflict: true, preparing: false, error: '別のエンジンが project.json を更新した' };
    }
    return real(event, n, args);
  }`);
}

async function undoAll() {
  for (let i = 0; i < 20; i++) {
    const h = await win.evaluate(() => window.__app.hist());
    if (!h?.can_undo) break;
    await win.evaluate(() => window.__app.undo());
    await settle();
  }
}

test('(Q3) 複数ノートのピッチで、途中の呼び出しが競合したら「一部だけ当たった可能性がある」', async () => {
  await settle();
  await undoAll();
  const ids = (await win.evaluate(() => window.__app.notes())).slice(0, 3).map((n) => n.id);
  // 2 つ目の shift_pitch だけ競合: 1 つ目は当たっている
  await conflictOn('shift_pitch', 2);
  try {
    await win.evaluate(async (ns) => {
      const { applyPitch } = await import('./edits.js');
      await applyPitch(new Map(ns.map((id) => [id, 0.5])));
    }, ids);
  } finally {
    await unhook();
  }
  await settle();
  expect(await status()).toContain('今の操作は一部だけ当たった可能性がある。表示を確かめて');
  expect((await edits()).filter((e) => e.kind === 'pitch_shift')).toHaveLength(1);
  // 最初の呼び出しの競合は、今までどおり「当てていない」
  await undoAll();
  await conflictOn('shift_pitch', 1);
  try {
    await win.evaluate(async (ns) => {
      const { applyPitch } = await import('./edits.js');
      await applyPitch(new Map(ns.map((id) => [id, 0.5])));
    }, ids);
  } finally {
    await unhook();
  }
  await settle();
  expect(await status()).toContain('今の操作は当てていない');
  expect((await edits()).filter((e) => e.kind === 'pitch_shift')).toHaveLength(0);
});

test('(Q3) 3 つのノートの結合で、2 回目の merge_notes が競合したら「一部だけ当たった可能性がある」', async () => {
  await settle();
  await undoAll();
  // 接して並ぶ 3 つ（結合できる並び）
  const trio = await win.evaluate(() => {
    const ns = window.__app.notes();
    for (let i = 0; i + 2 < ns.length; i++) {
      if (Math.abs(ns[i].end - ns[i + 1].start) < 1e-3 && Math.abs(ns[i + 1].end - ns[i + 2].start) < 1e-3) {
        return [ns[i].id, ns[i + 1].id, ns[i + 2].id];
      }
    }
    return null;
  });
  test.skip(!trio, '接して並ぶ 3 つのノートが無い');
  await conflictOn('merge_notes', 2);
  try {
    await win.evaluate(async (ids) => {
      const { mergeMany } = await import('./edits.js');
      await mergeMany(ids);
    }, trio);
  } finally {
    await unhook();
  }
  await settle();
  expect(await status()).toContain('今の操作は一部だけ当たった可能性がある');
  expect((await edits()).filter((e) => e.kind === 'merge')).toHaveLength(1);
  await undoAll();
});

test('ここまでにコンソールエラーが無い', async () => {
  expect(errors).toEqual([]);
});

test('(Q4) 閉じたセッションの prep_status が遅れて失敗しても、ポーリングを続けない（予約は 1 本）', async () => {
  await settle();
  const original = await win.evaluate(() => window.__app.S.session);
  // 1 回目の prep_status は止めておき、後で失敗させる。2 回目からはすぐ失敗する
  await app.evaluate(() => { globalThis.__qPolls = 0; });
  await hook(`(event, name, args) => {
    if (name !== 'prep_status') return real(event, name, args);
    globalThis.__qPolls += 1;
    if (globalThis.__qPolls === 1) {
      return new Promise((_, reject) => { globalThis.__qPollRelease = () => reject(new Error('offline')); });
    }
    throw new Error('offline');
  }`);
  const polls = () => app.evaluate(() => globalThis.__qPolls);
  const adopt = (sess) => win.evaluate(async (s) => {
    const { adoptSession } = await import('./session.js');
    adoptSession(s);
  }, sess);
  const queued = (dir) => ({ dir, current: null, tracks: [{ id: 'fake', name: 'fake', kind: 'vocal',
    prep: { state: 'queued' } }] });
  try {
    // 閉じた後（トラック 0 本の新しいセッション）: 古い呼び出しが失敗しても、もう読まない
    await adopt(queued('D:/fake-session-a'));
    await expect.poll(() => app.evaluate(() => !!globalThis.__qPollRelease), { timeout: 5000 }).toBe(true);
    await adopt({ dir: 'D:/fake-session-b', current: null, tracks: [] });
    await app.evaluate(() => globalThis.__qPollRelease());
    await win.waitForTimeout(3500);
    expect(await polls()).toBe(1);

    // 準備中のある新しいセッションに変わった: 古い呼び出しの失敗で 2 本目の予約を作らない
    await app.evaluate(() => { globalThis.__qPolls = 0; delete globalThis.__qPollRelease; });
    await adopt(queued('D:/fake-session-c'));
    await expect.poll(() => app.evaluate(() => !!globalThis.__qPollRelease), { timeout: 5000 }).toBe(true);
    await adopt(queued('D:/fake-session-d'));
    await app.evaluate(() => globalThis.__qPollRelease());
    const n0 = await polls();                      // 1（止めていた呼び出し）
    await win.waitForTimeout(3500);
    // 1 本なら: 1 秒後に 1 回（失敗）→ 次は 3 秒後。2 本あると 3 秒後にもう 1 回増える
    expect((await polls()) - n0).toBe(1);
  } finally {
    await unhook();
    await adopt({ dir: 'D:/fake-session-e', current: null, tracks: [] });
    if (original) await adopt(original);
  }
});
