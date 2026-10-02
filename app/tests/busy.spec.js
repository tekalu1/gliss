import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const USERDATA = path.join(APP, '..', 'projects', '_test-busy-userdata');
let app;
let win;

test.beforeAll(async () => {
  fs.rmSync(USERDATA, { recursive: true, force: true });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  app = await electron.launch({ args: [APP, '--user-data-dir', USERDATA, '--mute'], env });
  win = await app.firstWindow();
  await win.waitForFunction(() => !!window.__app);
  await expect(win.locator('#firstRun')).toBeVisible();
});

test.afterAll(async () => { await app?.close(); });

// どのテストからも busy.js を触れるように（前のテストが落ちてページが作り直されても）
test.beforeEach(async () => {
  await win.evaluate(async () => { window.testBusy ??= await import('../renderer/busy.js'); });
});

test('開く処理中の二重起動を断る', async () => {
  const state = await win.evaluate(async () => {
    const { S, onMenu } = window.__app;
    S.opening = 1;
    try {
      const result = await window.__app.newProject();
      const menuResult = await onMenu({ cmd: 'open-take' });
      return { result, menuResult, opening: S.opening };
    } finally {
      S.opening = 0;
    }
  });
  expect(state).toEqual({ result: false, menuResult: false, opening: 1 });
});

test('解析を取り消した現在トラックは再選択経路に入る', async () => {
  const result = await win.evaluate(async () => {
    const { S, selectTrack } = window.__app;
    const previous = { session: S.session, tracks: S.tracks, vd: S.vd, view: S.view };
    S.session = { current: 'missing-test-track' };
    S.tracks = [{ id: 'missing-test-track', kind: 'vocal', name: 'test' }];
    S.vd = null;
    S.view = { t0: 0, span: 1 };
    try {
      return await selectTrack('missing-test-track');
    } finally {
      Object.assign(S, previous);
    }
  });
  // The fake track is absent from the engine, so an actual retry fails. The old same-track shortcut returned true.
  expect(result).toBe(false);
});

test('短い処理は隠し、長い処理だけヘッダーの進捗とカーソルを出す', async () => {
  const previous = await win.locator('#status').textContent();
  await win.evaluate(async () => {
    window.testBusy = await import('../renderer/busy.js');
    const { status } = await import('../renderer/engine.js');
    status('解析している…');
    window.testTask = window.testBusy.beginBusy({ label: '解析している…' });
  });
  await expect(win.locator('#busyLine')).toBeHidden();
  expect(await win.locator('#status').textContent()).toBe(previous);
  await win.evaluate(async () => { (await import('../renderer/engine.js')).status('完了'); });
  await win.waitForTimeout(350);
  await expect(win.locator('#busyLine')).toBeVisible();
  await expect(win.locator('#mock')).toHaveClass(/is-busy/);
  await win.evaluate(() => { window.testTask.update(0.6); });
  await expect(win.locator('#busyFill')).toHaveCSS('width', /./);
  expect(await win.locator('#busyFill').evaluate((el) => el.style.width)).toBe('60%');
  await win.evaluate(() => window.testTask.finish());
  await expect(win.locator('#busyLine')).toBeHidden();
});

// 止める処理（issue #63 の 4）の試しに使う: 覆いの後ろに置くボタン（押された回数と、届いたキーを数える）
async function addBehind() {
  await win.evaluate(() => {
    window.hits = { click: 0, keys: [] };
    const b = document.createElement('button');
    b.id = 'behind';
    b.textContent = 'behind';
    b.style.cssText = 'position:fixed;left:40px;top:120px;width:120px;height:40px;z-index:1';
    b.addEventListener('click', () => { window.hits.click += 1; });
    document.body.appendChild(b);
    window.onTestKey = (e) => { window.hits.keys.push(e.key); };
    document.addEventListener('keydown', window.onTestKey);
  });
}
async function removeBehind() {
  await win.evaluate(() => {
    document.querySelector('#behind')?.remove();
    document.removeEventListener('keydown', window.onTestKey);
  });
}
/** ページの中で、ポップアップを出した・隠した時刻を記録し始める（hidden の変化を MutationObserver で見る。
 * テストのウィンドウは透明で、requestAnimationFrame は間引かれることがあるので使わない）。 */
async function watchPopup() {
  await win.evaluate(() => {
    window.popLog = { shown: 0, first: null, last: null };
    const el = document.querySelector('#busyCenter');
    window.popObs?.disconnect();
    window.popObs = new MutationObserver(() => {
      if (!el.hidden) { window.popLog.shown += 1; window.popLog.first ??= performance.now(); }
      else if (window.popLog.first !== null && window.popLog.last === null) window.popLog.last = performance.now();
    });
    window.popObs.observe(el, { attributes: true, attributeFilter: ['hidden'] });
  });
}
const popLog = () => win.evaluate(() => { window.popObs.disconnect(); return window.popLog; });

test('止める処理は始まった瞬間から後ろの入力を止め、0.2 秒後にポップアップ、Esc で取り消しを頼む', async () => {
  await addBehind();
  await win.evaluate(() => {
    window.cancelCount = 0;
    window.testTask = window.testBusy.beginBusy({ label: 'WAV に書き出している', target: 'take_ve.wav', modal: true });
    window.testTask.cancelWith(() => { window.cancelCount += 1; });
  });
  // すぐ: 覆いは掛かっている（透明）。ポップアップはまだ
  await expect(win.locator('#busyScrim')).toBeVisible();
  await expect(win.locator('#busyCenter')).toBeHidden();
  expect(await win.locator('#busyScrim').evaluate((el) => el.classList.contains('shown'))).toBe(false);
  await win.mouse.click(100, 140);
  await win.keyboard.press('x');
  expect(await win.evaluate(() => window.hits)).toEqual({ click: 0, keys: [] });
  // 覆いの上をクリックしたので、0.2 秒を待たずにポップアップを出した（止まっている理由を見せる）
  await expect(win.locator('#busyCenter')).toBeVisible();
  await win.evaluate(() => window.testTask.update(0.4, 'テイクの音程'));
  const pop = await win.evaluate(() => window.__app.busy().popup);
  expect(pop).toMatchObject({ label: 'WAV に書き出している', percent: '40%', target: 'take_ve.wav',
    note: '終わるまで編集できません', cancel: true, done: false });
  expect(pop.stage).toMatch(/^テイクの音程 · \d+ 秒$/);
  // ダイアログとして読み上げ、フォーカスを移す
  expect(await win.locator('#busyCenter').getAttribute('role')).toBe('dialog');
  expect(await win.locator('#busyCenter').getAttribute('aria-modal')).toBe('true');
  expect(await win.evaluate(() => document.activeElement?.id)).toBe('busyCenter');
  expect(await win.locator('#busyCancel').evaluate((el) => getComputedStyle(el).cursor)).toBe('pointer');
  await win.evaluate(() => window.testTask.cancelWith(null));
  await expect(win.locator('#busyCancel')).toBeHidden();
  await win.evaluate(() => window.testTask.cancelWith(() => { window.cancelCount += 1; }));
  await expect(win.locator('#busyCancel')).toBeVisible();
  await win.screenshot({ path: path.join(APP, 'test-results', 'busy-visible.png') });
  await win.keyboard.press('Escape');
  expect(await win.evaluate(() => window.cancelCount)).toBe(1);
  await expect(win.locator('#busyNote')).toHaveText('取り消している…');
  await win.evaluate(() => window.testTask.finish());
  await expect(win.locator('#busyCenter')).toBeHidden();
  await expect(win.locator('#busyScrim')).toBeHidden();
  // 終わったら入力は届く
  await win.mouse.click(100, 140);
  expect(await win.evaluate(() => window.hits.click)).toBe(1);
  await removeBehind();
});

test('0.2 秒より短い止める処理ではポップアップを出さない（入力はその間だけ止める）', async () => {
  await watchPopup();
  await win.evaluate(async () => {
    const t = window.testBusy.beginBusy({ label: 'テイク 2 を準備している', modal: true });
    await new Promise((r) => setTimeout(r, 120));
    t.finish();
    await new Promise((r) => setTimeout(r, 400));
  });
  expect((await popLog()).shown).toBe(0);
  await expect(win.locator('#busyScrim')).toBeHidden();
});

test('一度出したポップアップは 0.6 秒は残し、終わった後の残りは入力を通す', async () => {
  await addBehind();
  await watchPopup();
  await win.evaluate(() => {
    window.testTask = window.testBusy.beginBusy({ label: 'プロジェクトを開いている', target: 'song.gliss', modal: true });
  });
  await expect(win.locator('#busyCenter')).toBeVisible();
  await win.evaluate(() => { window.finishedAt = performance.now(); window.testTask.finish(); });
  // 終わった: 「終わった」の状態で残る。覆いは暗くせず、入力は通す
  await expect(win.locator('#busyNote')).toHaveText('終わった');
  expect(await win.evaluate(() => window.__app.busy().blocking)).toBe(false);
  await win.mouse.click(100, 140);
  expect(await win.evaluate(() => window.hits.click)).toBe(1);
  await expect(win.locator('#busyCenter')).toBeHidden();
  const log = await popLog();
  // 出してから消えるまで 0.6 秒以上
  expect(log.last - log.first).toBeGreaterThan(580);
  expect(log.last - log.first).toBeLessThan(1500);
  await removeBehind();
});

test('重なったら後から始まったものを出し、前のものは後で出す。覆いは両方終わるまで外さない', async () => {
  await win.evaluate(() => {
    window.taskA = window.testBusy.beginBusy({ label: 'プロジェクトを開いている', modal: true });
    window.taskB = window.testBusy.beginBusy({ label: 'テイク 2 を準備している', modal: true });
  });
  await expect(win.locator('#busyLabel')).toHaveText('テイク 2 を準備している');
  await win.evaluate(() => window.taskB.finish());
  await expect(win.locator('#busyLabel')).toHaveText('プロジェクトを開いている');
  expect(await win.evaluate(() => window.__app.busy().blocking)).toBe(true);
  await win.evaluate(() => window.taskA.finish());
  await expect(win.locator('#busyScrim')).toBeHidden({ timeout: 2000 });
});

test('覆いの上のクリックでポップアップを揺らし、終わったらフォーカスを戻す', async () => {
  await win.evaluate(() => {
    document.querySelector('#mock').focus();
    window.testTask = window.testBusy.beginBusy({ label: 'WAV に書き出している', target: 'take_ve.wav', modal: true });
    window.testTask.update(null, '音を作っている');
  });
  await expect(win.locator('#busyCenter')).toBeVisible();
  await win.waitForTimeout(700);
  await win.mouse.click(60, 400);
  expect(await win.locator('#busyCenter').evaluate((el) => el.getAnimations().length)).toBeGreaterThan(0);
  expect(await win.locator('#busyCenter .rail').evaluate((el) => el.classList.contains('unknown'))).toBe(true);
  await win.screenshot({ path: path.join(APP, 'test-results', 'busy-nudge.png') });
  await win.evaluate(() => window.testTask.finish());
  await expect(win.locator('#busyScrim')).toBeHidden();
  expect(await win.evaluate(() => document.activeElement?.id)).toBe('mock');
});

test('裏方の呼び出し（描画データ・一覧・準備の状態）は線を出さず、編集の反映は具体的なラベルで出す', async () => {
  // 呼び出している間の処理の一覧（線は 0.3 秒を超えたら、この一覧の最後のものを出す）
  const seen = await win.evaluate(async () => {
    const { call } = await import('../renderer/engine.js');
    const during = async (p) => { const t = window.__app.busy().tasks.map((x) => x.label); await p.catch(() => {}); return t; };
    return {
      prep: await during(call('prep_status', {})),
      list: await during(call('list_tracks', {})),
      view: await during(call('export_view_data', {})),
      named: await during(call('engine_info', {}, { busy: 'ピッチを反映している…' })),
      edit: await during(call('set_tempo', { bpm: 120 })),   // 開いていないので失敗する。線のラベルだけ見る
    };
  });
  expect(seen).toEqual({ prep: [], list: [], view: [], named: ['ピッチを反映している…'], edit: ['テンポを反映している…'] });
});

test('トラック解析の進捗を対象クリップの上に出す', async () => {
  await win.evaluate(() => {
    document.querySelector('#mock').classList.remove('first-run');
    document.querySelector('#tv').hidden = false;
    const lanes = document.querySelector('#lanes');
    lanes.innerHTML = '<rect data-clip="busy-test" x="20" y="20" width="120" height="30" />';
    window.testTask = window.testBusy.beginBusy({ label: '解析している…', clipId: 'busy-test' });
    window.testTask.update(0.4);
  });
  await win.waitForTimeout(350);
  await expect(win.locator('#busyClip')).toBeVisible();
  expect(await win.locator('#busyClip span').evaluate((el) => el.style.width)).toBe('40%');
  await win.screenshot({ path: path.join(APP, 'test-results', 'clip-progress.png') });
  await win.evaluate(() => {
    window.testTask.finish();
    document.querySelector('#tv').hidden = true;
    document.querySelector('#lanes').innerHTML = '';
    document.querySelector('#mock').classList.add('first-run');
  });
});
