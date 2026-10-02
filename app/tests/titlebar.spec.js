// タイトルバー一体型のウィンドウ（titleBarStyle: 'hidden' + titleBarOverlay）とメニューバー（renderer/titlebar.js）。
//
//   (T1) Windows のメニューバーは出ない。上端のバーの高さ = OS のボタンの高さ。空いた所はドラッグ、ボタンは no-drag
//   (T2) 画面のメニューバーは main の Menu と同じ（名前・有効／無効・チェック・キーの表記・下の段）
//   (T3) マウス: 押して開く・隣へ移る・外を押すと閉じる・項目を押すと main の同じ処理が動く・下の段
//   (T4) キーボード: Alt でフォーカス・←→・↓/Enter で開く・↑↓・Enter で実行・Esc で 1 段ずつ・F10、
//        フォーカスがある間はコマンドのキーが効かない、Alt を修飾に使ったら行かない
//   (T5) 中央にウィンドウのタイトルと同じ名前（未保存の *）
//   (T6) 狭い幅（800px）・最大化で重ならない
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const USERDATA = path.join(REPO, 'projects', '_test-titlebar-userdata');

let app;
let win;
const errors = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  fs.rmSync(USERDATA, { recursive: true, force: true });
  app = await electron.launch({ args: [APP, '--user-data-dir', USERDATA, '--mute'], env });
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.menubar().model.some((m) => m.label === 'ヘルプ'), null, { timeout: 120000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 760));
});

test.afterAll(async () => {
  await app?.close();
});

const mb = () => win.evaluate(() => {
  const m = window.__app.menubar();
  return { active: m.active, top: m.top, open: m.open, focus: document.activeElement?.closest?.('#menubar, .mbd') ? document.activeElement.textContent : null };
});
/** main の Menu（正）を画面と同じ形に: 見える項目の名前・種類・有効・チェック・キー・下の段の名前。 */
const mainMenu = () => app.evaluate(({ Menu }) => {
  const fmt = (a) => (a ? a.split('+').map((p) => (/^(CmdOrCtrl|CommandOrControl|Control|Ctrl)$/i.test(p) ? 'Ctrl' : p)).join('+') : '');
  return Menu.getApplicationMenu().items.map((top) => ({
    label: top.label,
    items: top.submenu.items.filter((x) => x.visible !== false).map((x) => (x.type === 'separator' ? { sep: true } : {
      label: x.label, enabled: x.enabled, checked: (x.type === 'checkbox' || x.type === 'radio') ? x.checked : null,
      key: x.submenu ? '›' : fmt(x.accelerator), sub: x.submenu ? x.submenu.items.map((y) => y.label) : null,
    })),
  }));
});
/** 画面に開いている段（DOM）を同じ形に。 */
const shownLevel = (k = 0) => win.evaluate((k) => {
  const el = document.querySelectorAll('.mbd')[k];
  if (!el) return null;
  return [...el.children].map((c) => (c.classList.contains('sep') ? { sep: true } : {
    label: c.querySelector('.l').textContent,
    enabled: c.getAttribute('aria-disabled') !== 'true',
    checked: c.hasAttribute('aria-checked') ? c.getAttribute('aria-checked') === 'true' : null,
    key: c.querySelector('.k').textContent,
  }));
}, k);
const top = (label) => win.locator('#menubar button', { hasText: label });
async function closeAll() {
  for (let i = 0; i < 4 && (await mb()).active; i++) await win.keyboard.press('Escape');
  expect((await mb()).active).toBe(false);
}

test('(T1) Windows のメニューバーは出ない・上端のバーと OS のボタン・ドラッグの領域', async () => {
  const w = await app.evaluate(({ BrowserWindow }) => {
    const bw = BrowserWindow.getAllWindows()[0];
    return { menuBar: bw.isMenuBarVisible(), bounds: bw.getBounds(), content: bw.getContentBounds() };
  });
  expect(w.menuBar).toBe(false);
  // 中身がウィンドウの上端から（Windows の枠のタイトルバー・メニューバーの分が無い）
  expect(w.content.y).toBe(w.bounds.y);
  const t = await win.evaluate(() => {
    const r = (s) => document.querySelector(s).getBoundingClientRect().toJSON();
    const region = (s) => getComputedStyle(document.querySelector(s)).getPropertyValue('-webkit-app-region').trim();
    return {
      wco: navigator.windowControlsOverlay.visible,
      area: navigator.windowControlsOverlay.getTitlebarAreaRect().toJSON(),
      bar: r('#titlebar'), firstRun: r('#firstRun'),
      regions: {
        bar: region('#titlebar'), logo: region('.titlebar .logo'), title: region('#docTitle'),
        menu: region('#menubar button'), toolbar: region('.tb'), roll: region('#roll'),
      },
      bg: getComputedStyle(document.querySelector('#titlebar')).backgroundColor,
      body: getComputedStyle(document.querySelector('#mock')).backgroundColor,
    };
  });
  expect(t.wco).toBe(true);
  expect(t.bar.y).toBe(0);
  expect(t.bar.height).toBe(32);                 // main の titleBarOverlay.height と同じ
  expect(t.area.height).toBe(32);
  await expect(win.locator('.tb')).toBeHidden();
  expect(t.firstRun.y).toBeGreaterThanOrEqual(t.bar.bottom);
  expect(t.bg).toBe(t.body);                     // バーの色 = 本体の色
  // 空いた所（ロゴ・中央の名前を含む）はドラッグでウィンドウを動かす。押せるものは no-drag
  expect(t.regions).toEqual({ bar: 'drag', logo: 'drag', title: 'drag', menu: 'no-drag', toolbar: 'none', roll: 'none' });
  // 開いた段も no-drag
  await top('ファイル').click();
  expect(await win.evaluate(() => getComputedStyle(document.querySelector('.mbd')).getPropertyValue('-webkit-app-region').trim())).toBe('no-drag');
  await closeAll();
});

test('(T2) 画面のメニューバーは main の Menu と同じ（名前・有効・チェック・キー・下の段）', async () => {
  const m = await mainMenu();
  expect(m.map((x) => x.label)).toEqual(['ファイル', '編集', 'ノート', '表示', 'ヘルプ']);
  expect(await win.locator('#menubar button').allTextContents()).toEqual(m.map((x) => x.label));
  for (const t of m) {
    await top(t.label).click();
    await expect.poll(() => shownLevel(0)).toEqual(t.items.map(({ sub, ...x }) => x));
    await closeAll();
  }
  // 無効（何も開いていない: 保存）・チェック（表示 > 時間スナップ）・キーの表記（Ctrl+Shift+S）が画面にも
  const file = m.find((x) => x.label === 'ファイル').items;
  expect(file.find((x) => x.label === '保存')).toMatchObject({ enabled: false, key: 'Ctrl+S' });
  expect(file.find((x) => x.label === '名前を付けて保存…').key).toBe('Ctrl+Shift+S');
  expect(m.find((x) => x.label === '表示').items.find((x) => x.label === '時間スナップ')).toMatchObject({ checked: false, key: 'N' });
});

test('(T3) マウス: 開く・隣へ移る・外で閉じる・押した項目が動く・下の段', async () => {
  // 押すと開く、もう一度押すと閉じる
  await top('ファイル').click();
  expect((await mb()).open.length).toBe(1);
  await top('ファイル').click();
  expect((await mb()).active).toBe(false);
  // 開いている間は隣に乗るだけで移る
  await top('ファイル').click();
  await top('表示').hover();
  await expect.poll(async () => (await mb()).top).toBe(3);
  expect((await shownLevel(0)).map((x) => x.label)).toContain('時間スナップ');
  // 外を押すと閉じる（下の画面には何も起きない）
  await win.mouse.click(640, 400);
  expect((await mb()).active).toBe(false);
  expect(await win.locator('.mbd').count()).toBe(0);

  // 表示 > 時間スナップ を押す → main の Menu の click → renderer のコマンド（キー N と同じ）
  const snap0 = await win.evaluate(() => window.__app.grid().snapT);
  await top('表示').click();
  await win.locator('.mbd button', { hasText: '時間スナップ' }).click();
  expect((await mb()).active).toBe(false);
  await expect.poll(() => win.evaluate(() => window.__app.grid().snapT)).toBe(!snap0);
  // チェックの表示も変わる（main の Menu が作り直され、画面も同じに）
  await expect.poll(async () => (await mainMenu()).find((x) => x.label === '表示').items
    .find((x) => x.label === '時間スナップ').checked).toBe(!snap0);
  await top('表示').click();
  await expect.poll(async () => (await shownLevel(0)).find((x) => x.label === '時間スナップ').checked).toBe(!snap0);
  await win.locator('.mbd button', { hasText: '時間スナップ' }).click();
  await expect.poll(() => win.evaluate(() => window.__app.grid().snapT)).toBe(snap0);

  // 無効の項目は押しても閉じない・何も起きない
  await top('ファイル').click();
  await win.locator('.mbd button', { hasText: '名前を付けて保存' }).click({ force: true });
  expect((await mb()).open.length).toBe(1);
  // 下の段（最近使ったプロジェクト）: 乗ると右に開く
  await win.locator('.mbd button', { hasText: '最近使ったプロジェクト' }).hover();
  await expect.poll(async () => (await mb()).open.length).toBe(2);
  expect((await shownLevel(1)).map((x) => x.label)).toEqual((await mainMenu())[0].items
    .find((x) => x.label === '最近使ったプロジェクト').sub);
  const [a, b] = await win.evaluate(() => [...document.querySelectorAll('.mbd')].map((e) => e.getBoundingClientRect().toJSON()));
  expect(b.left).toBeGreaterThanOrEqual(a.right - 4);
  await win.mouse.click(640, 400);

  // 編集 > ショートカット（キー・ホイール）… でキーの設定画面が開く（Ctrl+, と同じ）
  await top('編集').click();
  await win.locator('.mbd button', { hasText: 'ショートカット' }).click();
  await expect.poll(() => win.evaluate(() => window.__app.keysOpen())).toBe(true);
  await win.evaluate(() => window.__app.closeKeys());
});

test('(T4) キーボード: Alt・矢印・Enter・Esc・F10（Windows のメニューバーと同じ）', async () => {
  await win.locator('#mock').focus();
  // Alt だけを押して離す → メニューバーの最初（開かない）
  await win.keyboard.press('Alt');
  expect(await mb()).toMatchObject({ active: true, top: 0, open: [], focus: 'ファイル' });
  expect(await win.evaluate(() => window.__app.S.alt)).toBe(false);   // 離したことは画面にも届く
  await win.keyboard.press('ArrowRight');
  expect(await mb()).toMatchObject({ top: 1, focus: '編集' });
  await win.keyboard.press('ArrowLeft');
  await win.keyboard.press('ArrowLeft');
  expect(await mb()).toMatchObject({ top: 4, focus: 'ヘルプ' });   // 端で回る
  await win.keyboard.press('ArrowRight');
  // ↓ で開いて最初の項目
  await win.keyboard.press('ArrowDown');
  let s = await mb();
  expect(s.open.length).toBe(1);
  expect(s.open[0].labels[s.open[0].sel]).toBe('新規プロジェクト');
  // ↑ で端から最後（区切りは飛ばす）
  await win.keyboard.press('ArrowUp');
  s = await mb();
  expect(s.open[0].labels[s.open[0].sel]).toBe('終了');
  // 開いたまま → で隣のメニューが開く
  await win.keyboard.press('ArrowRight');
  s = await mb();
  expect(s.top).toBe(1);
  expect(s.open[0].labels[s.open[0].sel]).toMatch(/^元に戻す/);
  // Esc: 段を閉じる → メニューバーに残る → もう一度で抜けて、前のフォーカス（画面）に戻る
  await win.keyboard.press('Escape');
  expect(await mb()).toMatchObject({ active: true, top: 1, open: [] });
  await win.keyboard.press('Escape');
  expect((await mb()).active).toBe(false);
  expect(await win.evaluate(() => document.activeElement?.id)).toBe('mock');

  // Enter で実行: 表示 > 時間スナップ（キー N と同じ）
  const snap0 = await win.evaluate(() => window.__app.grid().snapT);
  await win.keyboard.press('Alt');
  for (let i = 0; i < 3; i++) await win.keyboard.press('ArrowRight');
  await win.keyboard.press('Enter');
  s = await mb();
  expect(s.top).toBe(3);
  while ((s = await mb()).open[0].labels[s.open[0].sel] !== '時間スナップ') await win.keyboard.press('ArrowDown');
  await win.keyboard.press('Enter');
  expect((await mb()).active).toBe(false);
  await expect.poll(() => win.evaluate(() => window.__app.grid().snapT)).toBe(!snap0);
  await win.keyboard.press('n');           // メニューを閉じたらコマンドのキーは効く
  await expect.poll(() => win.evaluate(() => window.__app.grid().snapT)).toBe(snap0);

  // フォーカスがある間は、コマンドのキー（N）は効かない
  await win.keyboard.press('Alt');
  await win.keyboard.press('n');
  expect(await win.evaluate(() => window.__app.grid().snapT)).toBe(snap0);
  // Alt をもう一度で抜ける
  await win.keyboard.press('Alt');
  expect((await mb()).active).toBe(false);

  // 下の段: → で開いて最初、← で閉じる
  await win.keyboard.press('Alt');
  await win.keyboard.press('Enter');
  await win.keyboard.press('ArrowDown');
  await win.keyboard.press('ArrowDown');
  s = await mb();
  expect(s.open[0].labels[s.open[0].sel]).toBe('最近使ったプロジェクト');
  await win.keyboard.press('ArrowRight');
  expect((await mb()).open.length).toBe(2);
  await win.keyboard.press('ArrowLeft');
  expect((await mb()).open.length).toBe(1);
  expect((await mb()).top).toBe(0);
  await closeAll();

  // F10 でも
  await win.keyboard.press('F10');
  expect(await mb()).toMatchObject({ active: true, top: 0 });
  await win.keyboard.press('F10');
  expect((await mb()).active).toBe(false);

  // Alt を修飾に使った（Alt+X・Alt を押したままクリック）ときは行かない
  await win.keyboard.press('Alt+x');
  expect((await mb()).active).toBe(false);
  await win.keyboard.down('Alt');
  await win.mouse.click(640, 400);
  await win.keyboard.up('Alt');
  expect((await mb()).active).toBe(false);
  // 画面が Alt を使った（はさみの吸着の切り替えなど。consumeAlt）ときも行かない
  await win.keyboard.down('Alt');
  await win.evaluate(() => window.api.consumeAlt());
  await win.keyboard.up('Alt');
  expect((await mb()).active).toBe(false);
  // キーの設定画面の間は行かない（Alt+… を割り当てる途中）
  await win.evaluate(() => window.__app.openKeys());
  await win.keyboard.press('Alt');
  expect((await mb()).active).toBe(false);
  await win.evaluate(() => window.__app.closeKeys());
});

test('(T5) 中央にウィンドウのタイトルと同じ名前（未保存の *）', async () => {
  const title = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getTitle());
  expect(await title()).toBe('Gliss');
  expect(await win.locator('#docTitle').textContent()).toBe('');
  await win.evaluate(() => window.api.setDoc({ name: 'うた', dirty: true }));
  await expect.poll(title).toBe('うた* — Gliss');
  await expect(win.locator('#docTitle')).toHaveText('うた*');
  await win.evaluate(() => window.api.setDoc({ name: 'うた', dirty: false }));
  await expect(win.locator('#docTitle')).toHaveText('うた');
  // 中央（ウィンドウの幅の真ん中）に出る
  const r = await win.evaluate(() => {
    const e = document.querySelector('#docTitle');
    const rg = document.createRange();
    rg.selectNodeContents(e);
    const t = rg.getBoundingClientRect();
    return { mid: (t.left + t.right) / 2, w: innerWidth };
  });
  expect(Math.abs(r.mid - r.w / 2)).toBeLessThan(4);
  await win.evaluate(() => window.api.setDoc(null));
  await expect(win.locator('#docTitle')).toHaveText('');
});

/** バーの中身が重なっていない: ロゴ・メニュー < 中央の名前 < OS のボタン（titlebar-area の右端）。 */
async function noOverlap() {
  return win.evaluate(() => {
    const r = (s) => document.querySelector(s).getBoundingClientRect();
    const area = navigator.windowControlsOverlay.getTitlebarAreaRect();
    const menus = [...document.querySelectorAll('#menubar button')].map((b) => b.getBoundingClientRect());
    const ttl = r('#docTitle');
    const text = document.createRange();
    text.selectNodeContents(document.querySelector('#docTitle'));
    const tr = text.getBoundingClientRect();
    const visibleText = Math.min(tr.right, ttl.right) - Math.max(tr.left, ttl.left);
    return {
      menusRight: Math.max(...menus.map((m) => m.right)),
      menusInside: menus.every((m) => m.left >= 0 && m.right <= area.right),
      ttlLeft: ttl.left, ttlRight: ttl.right, areaRight: area.right, width: innerWidth,
      clipped: document.querySelector('#docTitle').scrollWidth > document.querySelector('#docTitle').clientWidth,
      visibleText,
    };
  });
}

test('(T6) 狭い幅（800px）・最大化でも重ならない', async () => {
  await win.evaluate(() => window.api.setDoc({ name: 'とても長いプロジェクトの名前のテスト用のファイル名_最終版_v12', dirty: true }));
  for (const size of [[1280, 760], [800, 600], [640, 480]]) {
    await app.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setSize(w, h), size);
    await expect.poll(() => win.evaluate(() => innerWidth)).toBeLessThanOrEqual(size[0]);
    const o = await noOverlap();
    expect(o.menusInside, `${size}`).toBe(true);
    expect(o.ttlLeft + 0.5, `${size}`).toBeGreaterThanOrEqual(o.menusRight);
    expect(o.ttlRight, `${size}`).toBeLessThanOrEqual(o.areaRight + 0.5);
    // 開いた段も画面の中
    await top('ヘルプ').click();
    const d = await win.evaluate(() => document.querySelector('.mbd').getBoundingClientRect().toJSON());
    expect(d.right).toBeLessThanOrEqual(size[0]);
    await closeAll();
  }
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].maximize());
  await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMaximized())).toBe(true);
  await win.waitForTimeout(300);
  const o = await noOverlap();
  expect(o.menusInside).toBe(true);
  expect(o.ttlLeft + 0.5).toBeGreaterThanOrEqual(o.menusRight);
  expect(o.ttlRight).toBeLessThanOrEqual(o.areaRight + 0.5);
  expect(await win.evaluate(() => document.querySelector('#titlebar').getBoundingClientRect().top)).toBe(0);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].unmaximize());
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 760));
  await win.evaluate(() => window.api.setDoc(null));
});

test('エラーが出ていない', async () => {
  expect(errors).toEqual([]);
});
