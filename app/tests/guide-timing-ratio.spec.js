// 「ガイドに合わせる」のタイミングの割合をノートごとに後から変える（案 T1。2026-10-11 承認）。
// 素材・解析モデルの重みは要らない（合成の歌）。
//
//   (T1) 開くと選んだノートの今の割合（記録の無いノートは 0%）。違えば「混在」と「ノートごとに lo〜hi%」
//   (T2) つまみを動かすと輪郭がすぐ変わる（ノートの境目が動く）。離すまでエンジンの編集は増えない
//   (T3) 隙間を挟んだ前のノートの尻がゆずる: 前のノートに破線の縁と「接している前のノートの長さも変わります（1 ノート）」
//   (T4) 届かないノート: 右上の印と「1 ノートは届きません」（理由はツールチップ）
//   (T5) 手で直したノートは既定で除く（「手で直した 1 ノートは除いています」）。チェックすると含めて当て直す
//   (T6) ポップアップの中の確定は 1 回の Ctrl+Z で開く前に戻る
//   (T7) 音程のつまみと一緒に動かしても 1 回の Ctrl+Z
//   (T8) 開き直す・アプリを開き直すと今の割合が出る
//   (T9) プラグイン（GLISS_TEST_ARA=1）でも同じ表示と確定
import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** 倍音のある合成の歌。notes: [開始秒, 終わり秒, MIDI]、vib: ビブラートの深さ（半音）・速さ（Hz）。
 * 接して並ぶノートは位相をつないで 1 つの声にする（立ち上がり・終わりは 10 ms）。 */
function writeSong(file, notes, vib, hz, sec = 3.5, sr = 22050) {
  const n = sr * sec;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(buf.length - 8, 4); buf.write('WAVEfmt ', 8);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  let ph = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const k = notes.findIndex(([a, b]) => t >= a && t < b);
    let env = 0;
    let f = 220;
    if (k >= 0) {
      const [a, b, m] = notes[k];
      const joinedPrev = k > 0 && Math.abs(notes[k - 1][1] - a) < 1e-9;
      const joinedNext = k + 1 < notes.length && Math.abs(notes[k + 1][0] - b) < 1e-9;
      env = 0.25 * Math.min(1, joinedPrev ? 1 : (t - a) / 0.01, joinedNext ? 1 : (b - t) / 0.01);
      const depth = vib * Math.min(1, Math.max(0, (t - a - 0.12) / 0.1));
      f = 440 * 2 ** ((m - 69 + depth * Math.sin(2 * Math.PI * hz * (t - a))) / 12);
    }
    ph += 2 * Math.PI * f / sr;
    let x = 0;
    for (let h = 0; h < 8; h++) x += 0.6 ** h * Math.sin((h + 1) * ph);
    buf.writeInt16LE(Math.round(env * x * 0.5 * 32767), 44 + i * 2);
  }
  fs.writeFileSync(file, buf);
}

// 隙間（休み）を挟んで並ぶノート（エンジンの test_timing_ratio.py の GAP と同じ並び）: ガイドは前のノートが早く終わり、
// 後のノートが早く始まる（後のノートだけ寄せると、頭が前のノートの尻を追い越す）
const TAKE_NOTES = [[0.5, 0.80, 60], [0.95, 1.3, 62], [1.3, 1.75, 64], [2.3, 2.8, 67]];
const GUIDE_NOTES = [[0.5, 0.70, 60.5], [0.75, 1.3, 62.5], [1.3, 1.75, 64.5], [2.36, 2.8, 67.5]];

let ROOT;
let TAKE;
let GUIDE;
let app;
let win;
let ara = false;
const errors = [];

async function launch() {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1', VOCAL_ENGINE_AUTO_LYRICS: '0',
    VOCAL_ENGINE_WORK_DIR: path.join(ROOT, 'work'), VOCAL_ENGINE_PROJECTS: path.join(ROOT, 'projects'),
    VOCAL_ENGINE_LOG_DIR: path.join(ROOT, 'log'), GLISS_BRIDGE: path.join(ROOT, 'bridge.json') };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_F0_ESTIMATOR;
  if (ara) env.GLISS_TEST_ARA = '1';
  app = await electron.launch({
    args: ara ? [APP, '--user-data-dir', path.join(ROOT, 'userdata'), '--mute']
      : [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', path.join(ROOT, 'project'),
        '--user-data-dir', path.join(ROOT, 'userdata'), '--mute'], env,
  });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  if (ara) {
    // DAW の修飾のトラックを 2 つ作る（テイク・ガイド）
    await win.waitForFunction(() => !!window.__app && window.api?.mode === 'ara', null, { timeout: 120000 });
    await win.evaluate(async ([take, guide]) => {
      const c = async (n, a) => { const r = await window.api.call(n, a); if (r.ok === false) throw new Error(`${n}: ${r.error}`); return r; };
      await c('ara_open', { work_key: 'timing-ratio-spec', name: 'timing ratio' });
      await c('ara_set_modification', { ara_id: 'mod-0', source_path: take, source_id: 'src-0', name: 'take', group: 'take', offset_sec: 0 });
      await c('ara_set_modification', { ara_id: 'mod-1', source_path: guide, source_id: 'src-1', name: 'guide', group: 'guide', offset_sec: 0 });
    }, [TAKE, GUIDE]);
    await win.evaluate(() => window.api.__araEmit('session-changed', { dir: '' }));
    await win.waitForFunction(() => window.__app.S.tracks.length === 2 && window.__app.ready(), null, { timeout: 240000 });
    await settle();
    // 共通のガイド = guide、編集するトラック = take（track-guide.spec.js と同じ道具）
    const [take, guide] = await win.evaluate(() => ['take', 'guide']
      .map((nm) => window.__app.S.tracks.find((t) => t.name === nm)?.id));
    await win.evaluate((id) => window.__app.setGuide(id), guide);
    await settle();
    await win.evaluate((id) => window.__app.selectTrack(id), take);
    await settle();
  }
  await win.waitForFunction(() => window.__app?.ready() && !!window.__app.S.vd?.guide && !!window.__app.S.vd?.guide_basis,
    null, { timeout: 240000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 800));
  await win.evaluate(() => { window.__app.S.view = { t0: 0, span: 3.5 }; window.__app.render(); });
  await settle();
}

async function close() {
  const pid = app?.process().pid;
  // 試験のセッションは無題: 閉じるときの「保存しますか」を「保存しない」で答える（ara-mode.spec.js と同じ）
  if (ara) await app?.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 1 }); }).catch(() => {});
  await Promise.race([app?.close(), new Promise((r) => setTimeout(r, 15000))]).catch(() => {});
  try { if (pid) execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* 終わっている */ }
  app = null;
}

async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();

/** 音程ノート（時刻順）。ids[0] と ids[1] の間が隙間、ids[1] と ids[2] は接している。 */
let ids = [];

async function select(...xs) {
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');
  for (const [k, id] of xs.entries()) await blob(id).click(k ? { modifiers: ['Shift'] } : {});
  expect(await win.evaluate(() => [...window.__app.S.sel])).toEqual(xs);
}

/** ポップアップのタイミングの表示。 */
const timeView = () => win.evaluate(() => {
  const q = (s) => document.querySelector(s);
  const shown = (s) => (q(s).hidden ? null : q(s).textContent);
  return { time: q('#popTimeV').textContent, timeSub: shown('#popTimeS'), timeValue: +q('#popTime').value,
    mixed: q('#popTimeV').classList.contains('mix'), disabled: q('#popTime').disabled,
    nb: shown('#popTimeNb'), reach: shown('#popTimeReach'), lock: shown('#popTimeLock'),
    manualN: shown('#popManualN'), manualL: q('#popManualL').textContent, pitch: q('#popPitchV').textContent };
});

/** G でポップアップを開き、計画（ノートごとの今の割合）が届いてからの表示。 */
async function openPop() {
  await win.keyboard.press('g');
  await win.waitForFunction(() => !document.querySelector('#pop').hidden
    && !!window.__app.S.plan?.data?.timing_ratio, null, { timeout: 60000 }).catch(async (e) => {
    const why = await win.evaluate(() => ({ pop: !document.querySelector('#pop').hidden, plan: !!window.__app.S.plan,
      kind: window.__app.S.plan?.data?.kind, tr: window.__app.S.plan?.data?.timing_ratio,
      status: document.querySelector('#status')?.textContent, focus: document.activeElement?.id }));
    throw new Error(`${e.message}\n${JSON.stringify(why)}`);
  });
  return timeView();
}

/** つまみを動かす（離さない）。 */
async function slide(sel, v) {
  await win.evaluate(([s, val]) => {
    const el = document.querySelector(s);
    el.value = String(val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, [sel, v]);
}
/** つまみを動かして離す（離したら当たる）。 */
async function setSlider(sel, v) {
  await slide(sel, v);
  await win.evaluate((s) => document.querySelector(s).dispatchEvent(new Event('change', { bubbles: true })), sel);
  await settle();
}

const live = () => win.evaluate(() => window.__app.S.vd.history.changesets.filter((c) => !c.undone).length);
/** 描いている頭・尻（編集後の秒。プレビューを含む）。 */
const shapes = () => win.evaluate(() => Object.fromEntries(window.__app.shapes().map((s) => [s.id, [s.s, s.e]])));
/** エンジンに記録されたノートごとのタイミングの割合。 */
const ratios = () => win.evaluate(async (xs) => {
  const r = await window.api.call('get_correction_strength', { note_ids: xs });
  return Object.fromEntries(r.notes.map((n) => [n.note, n.timing_state === 'none' ? 0 : n.timing]));
}, ids);
const near = (a, b, tol = 0.002) => Math.abs(a - b) < tol;

/** 全体（選択なし）をタイミング v% にそろえる。 */
async function setAll(v) {
  await win.keyboard.press('Escape');
  await win.keyboard.press('Escape');
  await openPop();
  await setSlider('#popTime', v);
  await win.keyboard.press('Escape');
}

function suite(name) {
  test.describe(name, () => {
    test.describe.configure({ mode: 'serial' });
    test.beforeAll(async () => {
      ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'gliss-timing-ratio-'));
      TAKE = path.join(ROOT, 'take.wav');
      GUIDE = path.join(ROOT, 'guide.wav');
      writeSong(TAKE, TAKE_NOTES, 0.3, 5.5);
      writeSong(GUIDE, GUIDE_NOTES, 0.6, 4.5);
      errors.length = 0;
      await launch();
    });
    test.afterAll(async () => {
      await close();
      if (ROOT) fs.rmSync(ROOT, { recursive: true, force: true, maxRetries: 5 });
    });

    test('(T1) 開くと今の割合（記録が無ければ 0%）・違えば混在', async () => {
      await win.keyboard.press('Escape');
      expect(await openPop()).toMatchObject({ time: '0%', timeSub: null, timeValue: 0, mixed: false, disabled: false,
        nb: null, reach: null });
      ids = await win.evaluate(() => window.__app.notes().sort((a, b) => a.start - b.start).map((n) => n.id));
      expect(ids.length).toBe(4);
      const sh = await shapes();
      expect(sh[ids[1]][0] - sh[ids[0]][1]).toBeGreaterThan(0.05);          // 隙間
      expect(near(sh[ids[1]][1], sh[ids[2]][0])).toBe(true);               // 接している
      await setSlider('#popTime', 100);
      await win.keyboard.press('Escape');
      expect(await openPop()).toMatchObject({ time: '100%', timeValue: 100, mixed: false });
      await win.keyboard.press('Escape');

      await select(ids[1]);
      expect(await openPop()).toMatchObject({ time: '100%', timeValue: 100 });
      await setSlider('#popTime', 30);
      await win.keyboard.press('Escape');
      const r = await ratios();
      expect(r[ids[1]]).toBeCloseTo(0.3, 5);
      expect(r[ids[0]]).toBeCloseTo(1, 5);

      await select(ids[0], ids[1]);
      expect(await openPop()).toMatchObject({ time: '混在', timeSub: 'ノートごとに 30〜100%', timeValue: 65, mixed: true });
      await win.keyboard.press('Escape');
    });

    test('(T2) つまみを動かすと輪郭がすぐ変わる（離すまで編集は増えない）', async () => {
      await select(ids[1]);
      await openPop();
      const n0 = await live();
      const before = await shapes();
      await slide('#popTime', 100);
      const at100 = await shapes();
      expect(await win.evaluate(() => window.__app.plan().t)).toBe(1);
      // 頭（隙間の後）が動く。ガイドは 0.2 秒早く始まる: 30% → 100% で 0.7 × 0.2 ≒ 0.14 秒早まる
      expect(before[ids[1]][0] - at100[ids[1]][0]).toBeGreaterThan(0.05);
      expect(near(at100[ids[3]][0], before[ids[3]][0])).toBe(true);        // 選んでいないノートは動かない
      await slide('#popTime', 30);
      const back = await shapes();
      expect(near(back[ids[1]][0], before[ids[1]][0], 0.001)).toBe(true);  // 今の割合に戻すと元の輪郭
      expect(await live()).toBe(n0);
      await win.keyboard.press('Escape');
      expect(near((await shapes())[ids[1]][0], before[ids[1]][0], 0.001)).toBe(true);
    });

    test('(T3) 前のノートの尻がゆずる: 破線の縁と説明の行', async () => {
      await setAll(0);
      await select(ids[1]);
      expect(await openPop()).toMatchObject({ time: '0%', nb: null });
      await slide('#popTime', 100);
      const v = await timeView();
      expect(v.nb).toBe('接している前のノートの長さも変わります（1 ノート）');
      expect(v.reach).toBeNull();
      expect(await win.evaluate(() => window.__app.plan().timingMarks.nb)).toEqual([ids[0]]);
      await expect(win.locator(`#roll rect[data-nb="${ids[0]}"]`)).toHaveCount(1);
      expect(await win.locator(`#roll rect[data-nb="${ids[0]}"]`).getAttribute('stroke-dasharray')).toBe('4 3');
      await slide('#popTime', 0);                                         // 0% では動かない: 行も縁も消える
      expect((await timeView()).nb).toBeNull();
      await expect(win.locator('#roll rect[data-nb]')).toHaveCount(0);
      await slide('#popTime', 100);
      const n0 = await live();
      await win.evaluate(() => document.querySelector('#popTime').dispatchEvent(new Event('change', { bubbles: true })));
      await settle();
      expect(await live()).toBe(n0 + 1);
      const sh = await shapes();
      expect(sh[ids[0]][1]).toBeLessThan(0.8 - 0.02);                      // 前のノートが短くなった
      await win.keyboard.press('Escape');
      await expect(win.locator('#roll rect[data-nb]')).toHaveCount(0);     // 閉じたら縁は消える
    });

    test('(T4) 届かないノート: 右上の印と「1 ノートは届きません」', async () => {
      await setAll(100);
      await select(ids[0]);
      expect(await openPop()).toMatchObject({ time: '100%', reach: null });
      await slide('#popTime', 0);
      const v = await timeView();
      expect(v.reach).toBe('1 ノートは届きません');
      expect(await win.evaluate(() => document.querySelector('#popTimeReach').title)).not.toBe('');
      await expect(win.locator(`#roll [data-unreached="${ids[0]}"]`)).toHaveCount(1);
      await expect(win.locator(`#roll [data-unreached="${ids[0]}"] text`)).toHaveText('!');
      await slide('#popTime', 100);
      await expect(win.locator('#roll [data-unreached]')).toHaveCount(0);
      await win.keyboard.press('Escape');
    });

    test('(T5)(T6) 手で直したノートは既定で除く・含めて当て直す・1 回の Ctrl+Z で開く前に戻る', async () => {
      // 4 つ目のノートを手で 20 ms 遅らせる（ドラッグと同じ計画）
      await win.evaluate(async (id) => {
        const r = await window.api.call('plan_edit', { op: 'move', note_ids: [id] });
        await window.api.call('apply_plan', { plan_id: r.plan_id, x: 0.02, author: 'human' });
        await window.__app.refresh();
      }, ids[3]);
      await settle();
      await select(ids[2], ids[3]);
      const v = await openPop();
      expect(v).toMatchObject({ manualN: '手で直した 1 ノートは除いています', manualL: '手で直したノートも含める（1）',
        time: '100%', mixed: false });
      const n0 = await live();
      const pre = await shapes();
      const r0 = await ratios();
      await setSlider('#popTime', 40);
      let sh = await shapes();
      expect(near(sh[ids[3]][0], pre[ids[3]][0], 0.001)).toBe(true);      // 手で直したノートは動かない
      expect((await ratios())[ids[2]]).toBeCloseTo(0.4, 5);
      // 含めると、手で直したノートもこの割合に当て直す（前回ぶんを取り消して 1 つの項目のまま）
      await win.locator('#popManual').check();
      await settle();
      expect((await timeView()).manualN).toBeNull();
      sh = await shapes();
      expect(pre[ids[3]][0] - sh[ids[3]][0]).toBeGreaterThan(0.01);       // ガイドは 60 ms 遅いので、40% で手前へ戻る
      const r1 = await ratios();
      expect(r1[ids[2]]).toBeCloseTo(0.4, 5);
      expect(r1[ids[3]]).toBeCloseTo(0.4, 5);
      expect(await live()).toBe(n0 + 1);
      // 外すと除いて当て直す
      await win.locator('#popManual').uncheck();
      await settle();
      expect(near((await shapes())[ids[3]][0], pre[ids[3]][0], 0.001)).toBe(true);
      expect(await live()).toBe(n0 + 1);
      // 作り直した計画も開く前の状態から: 手で直したノートは手で直したまま数え、動かしても動かない
      expect(await timeView()).toMatchObject({ manualN: '手で直した 1 ノートは除いています',
        manualL: '手で直したノートも含める（1）' });
      await slide('#popTime', 80);
      expect(near((await shapes())[ids[3]][0], pre[ids[3]][0], 0.001)).toBe(true);
      await slide('#popTime', 40);
      // キーボード: つまみは矢印で動いて当たり、チェックボックスは Space で切り替わる（再生は始まらない）
      await win.locator('#popTime').focus();
      await win.keyboard.press('ArrowRight');
      await settle();
      expect((await timeView()).time).toBe('41%');
      expect((await ratios())[ids[2]]).toBeCloseTo(0.41, 5);
      await win.locator('#popManual').focus();
      await win.keyboard.press('Space');
      await settle();
      expect(await win.locator('#popManual').isChecked()).toBe(true);
      expect(await win.evaluate(() => !!window.__app.S.playing)).toBe(false);
      expect((await ratios())[ids[3]]).toBeCloseTo(0.41, 5);
      expect(await live()).toBe(n0 + 1);
      await win.keyboard.press('Escape');
      await win.keyboard.press('Escape');
      await win.keyboard.press('Control+z');
      await settle();
      expect(await live()).toBe(n0);
      const back = await shapes();
      for (const id of ids) {
        expect(near(back[id][0], pre[id][0], 0.001), `頭 ${id}`).toBe(true);
        expect(near(back[id][1], pre[id][1], 0.001), `尻 ${id}`).toBe(true);
      }
      expect(await ratios()).toEqual(r0);
    });

    test('(T7) 音程のつまみと一緒に動かしても 1 回の Ctrl+Z', async () => {
      await select(ids[0], ids[1]);
      const v0 = await openPop();
      const n0 = await live();
      const pre = await shapes();
      const r0 = await ratios();
      await setSlider('#popPitch', 40);
      await setSlider('#popTime', 60);
      await setSlider('#popPitch', 70);
      expect(await live()).toBe(n0 + 1);
      await win.keyboard.press('Escape');
      expect(await openPop()).toMatchObject({ pitch: '70%', time: '60%', mixed: false });
      await win.keyboard.press('Escape');
      await win.keyboard.press('Escape');
      await win.keyboard.press('Control+z');
      await settle();
      expect(await live()).toBe(n0);
      expect(await ratios()).toEqual(r0);
      const back = await shapes();
      for (const id of ids) expect(near(back[id][0], pre[id][0], 0.001), `頭 ${id}`).toBe(true);
      await select(ids[0], ids[1]);
      expect(await openPop()).toMatchObject({ pitch: v0.pitch, time: v0.time });
      await win.keyboard.press('Escape');
    });

    test('(T8) 開き直す・アプリを開き直すと今の割合が出る', async () => {
      await select(ids[1]);
      await openPop();
      await setSlider('#popTime', 25);
      await win.keyboard.press('Escape');
      await select(ids[0], ids[1]);
      const v = await openPop();
      expect(v).toMatchObject({ time: '混在', mixed: true });
      await win.keyboard.press('Escape');
      if (ara) return;                       // プラグインは DAW が開き直す（エンジンの記録は単体と同じ）
      await close();
      await launch();
      await select(ids[0], ids[1]);
      expect(await openPop()).toMatchObject({ time: v.time, timeSub: v.timeSub, timeValue: v.timeValue });
      await win.keyboard.press('Escape');
    });

    test('実行時のコンソールエラーが無い', () => {
      expect(errors).toEqual([]);
    });
  });
}

suite('単体');
test.describe('プラグイン', () => {
  test.beforeAll(() => { ara = true; });
  test.afterAll(() => { ara = false; });
  suite('ARA');
});
