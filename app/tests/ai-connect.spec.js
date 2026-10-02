// AI とつなぐ（ヘルプ > AI とつなぐ…）・右クリックの「AI に頼む」・AI の編集の印。
//
//   (A1) ヘルプのメニューに「AI とつなぐ…」、ノートのメニューに「AI に頼む」
//   (A2) bridge.json: 画面で開いている曲（旧形式は作業場所）と編集中のトラック・AI に許可の既定（編集オン・保存オフ）
//   (A3) Claude Code [追加]: `claude mcp add-json gliss <json> --scope user`（偽の claude.cmd で受ける。JSON が壊れずに届く）
//        → 追加済み。設定は cwd を使わず PYTHONPATH・PYTHONIOENCODING・GLISS_BRIDGE
//   (A4) Claude Desktop [追加]: mcpServers に gliss だけ足す（ほかのキーは残す・書く前にバックアップ）→ 再起動の案内
//   (A5) その他 [設定をコピー]: mcpServers の JSON 断片・AI に許可のトグルは bridge.json に書く
//   (A6) 開き直すと登録済みは最初から「追加済み」。claude が無い・Claude Desktop が無いときはボタンを無効に
//   (A7) AI に頼む: 選んだノートの文をクリップボードへ・「3 ノートをコピー」
//   (A8) AI の操作の印: ツールチップ・編集メニューの「元に戻す: AI · ピッチ」、ノートの縁を AI の色に。人が上から編集すると外れる
//
// 本物の `claude` と %APPDATA%\Claude の設定には触らない（GLISS_CLAUDE・GLISS_CLAUDE_DESKTOP_CONFIG で差し替える）。
// クリップボードは使う前の中身に戻す。スクリーンショットは GLISS_SHOT_DIR があればそこへ（追跡しているファイルは書き換えない）。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C', 'C2');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.dirname(HERE);
const REPO = path.dirname(APP);
const ROOT = path.join(REPO, 'projects', '_test-ai-connect');
const MEDIA = path.join(ROOT, 'Media');
const TAKE = path.join(MEDIA, 'take.wav');
const GUIDE = path.join(MEDIA, 'guide.wav');
const PROJECT = path.join(ROOT, 'proj');
const USERDATA = path.join(ROOT, 'userdata');
const BRIDGE = path.join(USERDATA, 'bridge.json');
const FAKE = path.join(ROOT, 'fake-claude');
const CLAUDE_CMD = path.join(FAKE, 'claude.cmd');
const DESKTOP = path.join(ROOT, 'Roaming', 'Claude', 'claude_desktop_config.json');
const SHOTS = process.env.GLISS_SHOT_DIR || null;

let app;
let win;
let clipBefore = null;
const errors = [];

test.describe.configure({ mode: 'serial' });

// 偽の claude（npm の claude.cmd と同じく、中で node を %* で呼ぶ）。受けた引数を calls.json に残し、
// `mcp get gliss` は add-json の後だけ 0 を返す。add-json の JSON はここで読めなければ失敗にする
const FAKE_JS = `import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const dir = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const log = path.join(dir, 'calls.json');
const calls = fs.existsSync(log) ? JSON.parse(fs.readFileSync(log, 'utf8')) : [];
calls.push(args);
fs.writeFileSync(log, JSON.stringify(calls, null, 2));
const reg = path.join(dir, 'registered.json');
if (args[0] === 'mcp' && args[1] === 'get') {
  if (fs.existsSync(reg)) { console.log('gliss:\\n  Scope: User config'); process.exit(0); }
  console.log('No MCP server named "' + args[2] + '".');
  process.exit(1);
}
if (args[0] === 'mcp' && args[1] === 'add-json') {
  JSON.parse(args[3]);
  fs.writeFileSync(reg, args[3]);
  console.log('Added stdio MCP server ' + args[2] + ' to user config');
  process.exit(0);
}
process.exit(2);
`;

test.beforeAll(async () => {
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.GLISS_BRIDGE;
  env.VOCAL_ENGINE_PROJECTS = path.join(ROOT, 'projects');
  fs.rmSync(ROOT, { recursive: true, force: true });
  fs.mkdirSync(MEDIA, { recursive: true });
  fs.copyFileSync(M.clip('C'), TAKE);
  fs.copyFileSync(M.clip('C2'), GUIDE);
  fs.mkdirSync(FAKE, { recursive: true });
  fs.writeFileSync(path.join(FAKE, 'fake-claude.mjs'), FAKE_JS);
  fs.writeFileSync(CLAUDE_CMD, `@echo off\r\n"${process.execPath}" "%~dp0fake-claude.mjs" %*\r\n`);
  // Claude Desktop の設定（ほかのキー・ほかのサーバーがある）
  fs.mkdirSync(path.dirname(DESKTOP), { recursive: true });
  fs.writeFileSync(DESKTOP, JSON.stringify({ globalShortcut: 'Ctrl+Space', mcpServers: { other: { command: 'x.exe', args: ['a'] } } }, null, 2));
  env.GLISS_CLAUDE = CLAUDE_CMD;
  env.GLISS_CLAUDE_DESKTOP_CONFIG = DESKTOP;
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  app = await electron.launch({
    args: [APP, '--take', TAKE, '--guide', GUIDE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  clipBefore = await app.evaluate(({ clipboard }) => clipboard.readText());
  win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  win.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
  await win.evaluate(() => { window.__app.S.view = { t0: 2.2, span: 1.3 }; window.__app.render(); });
});

test.afterAll(async () => {
  if (app && clipBefore !== null) await app.evaluate(({ clipboard }, t) => clipboard.writeText(t), clipBefore).catch(() => {});
  await app?.close();
});

const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const clip = () => app.evaluate(({ clipboard }) => clipboard.readText());
const row = (id) => win.locator(`#ai [data-cl="${id}"]`);
const blob = (id) => win.locator(`#roll rect[data-note="${id}"]:not([data-edge])`).first();
async function settle() {
  await win.waitForFunction(() => window.__app.idle() && !window.__app.S.opening, null, { timeout: 120000 });
}
async function openDialog() {
  await win.evaluate(() => window.__app.runCommand('ai-connect'));
  await expect(win.locator('#ai')).toBeVisible();
  // 状態を読み終わる（Claude Code の行のボタンか「追加済み」が決まる）まで
  await win.waitForFunction(() => {
    const r = document.querySelector('#ai [data-cl="cc"]');
    const b = r.querySelector('button');
    return r.querySelector('.st').textContent || !b.disabled || r.classList.contains('off');
  }, null, { timeout: 60000 });
}
async function closeDialog() {
  await win.keyboard.press('Escape');
  await expect(win.locator('#ai')).toBeHidden();
}
/** 画面が起動したエンジンに入れている command / args（.mcp.json の gliss）と、エンジンのディレクトリ。 */
function engineSetup() {
  const g = readJson(path.join(REPO, '.mcp.json')).mcpServers.gliss;
  return { command: g.command, args: g.args, dir: process.env.VOCAL_ENGINE_CWD || g.cwd };
}

test('(A1) ヘルプに「AI とつなぐ…」、ノートの右クリックに「AI に頼む」', async () => {
  const tpl = await win.evaluate(() => window.__app.appMenuTemplate());
  const help = tpl.find((m) => m.label === 'ヘルプ');
  expect(help.submenu[0]).toMatchObject({ cmd: 'ai-connect', label: 'AI とつなぐ…', enabled: true });
  const note = tpl.find((m) => m.label === 'ノート');
  expect(note.submenu.map((x) => x.label)).toContain('AI に頼む');
  const bar = await app.evaluate(({ Menu }) => Menu.getApplicationMenu().items.find((m) => m.label === 'ヘルプ')
    .submenu.items.map((x) => x.label));
  expect(bar[0]).toBe('AI とつなぐ…');
});

test('(A2) bridge.json: 画面で開いている曲・編集中のトラック・AI に許可の既定', async () => {
  const s = await win.evaluate(() => ({ cur: window.__app.S.session.current, tracks: window.__app.tracks(), doc: window.__app.doc() }));
  const b = readJson(BRIDGE);
  expect(b.allow).toEqual({ edit: true, save: false });
  expect(b.project).toMatchObject({ kind: 'legacy', track: s.cur, track_name: s.tracks.find((t) => t.id === s.cur).name });
  expect(path.resolve(b.project.path)).toBe(path.resolve(s.doc.work_dir));
  // 編集するトラックを切り替えると追いかける（戻すと戻る）
  const other = s.tracks.find((t) => t.id !== s.cur && t.kind === 'vocal');
  await win.evaluate((id) => window.__app.selectTrack(id), other.id);
  await settle();
  await expect.poll(() => readJson(BRIDGE).project.track, { timeout: 60000 }).toBe(other.id);
  expect(readJson(BRIDGE).project.track_name).toBe(other.name);
  await win.evaluate((id) => window.__app.selectTrack(id), s.cur);
  await settle();
  await expect.poll(() => readJson(BRIDGE).project.track, { timeout: 60000 }).toBe(s.cur);
  await win.waitForFunction(() => window.__app.ready(), null, { timeout: 120000 });
  await win.evaluate(() => { window.__app.S.view = { t0: 2.2, span: 1.3 }; window.__app.render(); });
});

test('(A3) Claude Code [追加]: claude mcp add-json gliss … --scope user → 追加済み', async () => {
  await openDialog();
  await expect(row('cc').locator('button')).toBeEnabled();
  await expect(row('cc').locator('button')).toHaveText('追加');
  await expect(row('cd').locator('button')).toBeEnabled();
  await expect(win.locator('#ai [data-allow="edit"]')).toBeChecked();
  await expect(win.locator('#ai [data-allow="save"]')).not.toBeChecked();
  if (SHOTS) await win.screenshot({ path: path.join(SHOTS, 'ai-connect-dialog.png') });
  await row('cc').locator('button').click();
  await expect(row('cc').locator('.st')).toHaveText('追加済み');
  await expect(row('cc').locator('button')).toBeHidden();
  const calls = readJson(path.join(FAKE, 'calls.json'));
  expect(calls[0]).toEqual(['mcp', 'get', 'gliss']);
  const add = calls.find((c) => c[1] === 'add-json');
  expect(add.slice(0, 3)).toEqual(['mcp', 'add-json', 'gliss']);
  expect(add.slice(4)).toEqual(['--scope', 'user']);
  const cfg = JSON.parse(add[3]);          // cmd.exe を通っても JSON が壊れていない（\ と " と日本語）
  const e = engineSetup();
  expect(cfg).toMatchObject({ type: 'stdio', command: e.command, args: e.args });
  expect(cfg.cwd).toBeUndefined();
  expect(cfg.env.PYTHONPATH).toBe(e.dir);
  expect(cfg.env.PYTHONIOENCODING).toBe('utf-8');
  expect(path.resolve(cfg.env.GLISS_BRIDGE)).toBe(path.resolve(BRIDGE));
});

test('(A4) Claude Desktop [追加]: mcpServers に gliss だけ足す・バックアップ・再起動の案内', async () => {
  const before = fs.readFileSync(DESKTOP, 'utf8');
  await row('cd').locator('button').click();
  await expect(row('cd').locator('.st')).toHaveText('Claude Desktop を再起動');
  const d = readJson(DESKTOP);
  expect(d.globalShortcut).toBe('Ctrl+Space');
  expect(d.mcpServers.other).toEqual({ command: 'x.exe', args: ['a'] });
  const e = engineSetup();
  expect(d.mcpServers.gliss).toMatchObject({ command: e.command, args: e.args,
    env: { PYTHONPATH: e.dir, PYTHONIOENCODING: 'utf-8' } });
  expect(Object.keys(d.mcpServers)).toEqual(['other', 'gliss']);
  const bak = fs.readdirSync(path.dirname(DESKTOP)).filter((f) => f.startsWith('claude_desktop_config.json.gliss-backup-'));
  expect(bak.length).toBe(1);
  expect(fs.readFileSync(path.join(path.dirname(DESKTOP), bak[0]), 'utf8')).toBe(before);
});

test('(A5) その他 [設定をコピー]・AI に許可のトグルは bridge.json に', async () => {
  await row('ot').locator('button').click();
  await expect(row('ot').locator('.st')).toHaveText('コピーした');
  const j = JSON.parse(await clip());
  expect(Object.keys(j)).toEqual(['mcpServers']);
  expect(j.mcpServers.gliss).toEqual(readJson(DESKTOP).mcpServers.gliss);
  await win.locator('#ai label.sw', { hasText: '保存・書き出し' }).click();
  await expect.poll(() => readJson(BRIDGE).allow).toEqual({ edit: true, save: true });
  await win.locator('#ai label.sw', { hasText: '編集' }).first().click();
  await expect.poll(() => readJson(BRIDGE).allow).toEqual({ edit: false, save: true });
  expect(readJson(BRIDGE).project).toBeTruthy();                      // 曲の情報は消さない
  await win.locator('#ai label.sw', { hasText: '編集' }).first().click();
  await win.locator('#ai label.sw', { hasText: '保存・書き出し' }).click();
  await expect.poll(() => readJson(BRIDGE).allow).toEqual({ edit: true, save: false });
  await closeDialog();
});

test('(A6) 開き直すと追加済み・claude と Claude Desktop が無ければ無効', async () => {
  await openDialog();
  await expect(row('cc').locator('.st')).toHaveText('追加済み');
  await expect(row('cd').locator('.st')).toHaveText('追加済み');
  await closeDialog();
  await app.evaluate((_e, p) => { process.env.GLISS_CLAUDE = ''; process.env.GLISS_CLAUDE_DESKTOP_CONFIG = p; },
    path.join(ROOT, 'no-such', 'Claude', 'claude_desktop_config.json'));
  try {
    await openDialog();
    await expect(row('cc').locator('button')).toBeDisabled();
    await expect(row('cc')).toHaveAttribute('title', /claude コマンドが見つからない/);
    await expect(row('cd').locator('button')).toBeDisabled();
    await expect(row('cd')).toHaveClass(/off/);
    await expect(row('ot').locator('button')).toBeEnabled();
    await closeDialog();
  } finally {
    await app.evaluate((_e, [c, d]) => { process.env.GLISS_CLAUDE = c; process.env.GLISS_CLAUDE_DESKTOP_CONFIG = d; },
      [CLAUDE_CMD, DESKTOP]);
  }
});

test('(A7) AI に頼む: 選んだノートの文をクリップボードへ・「3 ノートをコピー」', async () => {
  const ids = await win.evaluate(() => window.__app.S.pitched.slice(3, 6).map((n) => n.id));
  await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, ids);
  await win.evaluate((id) => {
    const n = window.__app.S.byId.get(id);
    window.__app.S.view = { t0: Math.max(0, n.edited_start_sec - 0.3), span: 1.4 };
    window.__app.render();
  }, ids[1]);
  await blob(ids[1]).click({ button: 'right' });
  await expect(win.locator('#menu')).toBeVisible();
  const it = await win.evaluate(() => window.__app.menuItems());
  expect(it[it.length - 1]).toMatchObject({ label: 'AI に頼む', cmd: 'ask-ai', disabled: false });
  if (SHOTS) await win.screenshot({ path: path.join(SHOTS, 'ai-connect-menu.png') });
  await win.locator('#menu [data-cmd="ask-ai"]').click();
  await expect(win.locator('#toast')).toHaveText('3 ノートをコピー');
  await expect(win.locator('#toast')).toHaveClass(/on/);
  await expect(win.locator('#status')).toHaveText('3 ノートをコピー');
  const text = await clip();
  const cur = await win.evaluate(() => window.__app.tracks().find((t) => t.current).name);
  expect(text).toMatch(new RegExp(`^Gliss（gliss）で、今開いている曲の ${cur} トラックの ${ids[0]}〜${ids[2]}`
    + '（\\d+:\\d\\d\\.\\d\\d〜\\d+:\\d\\d\\.\\d\\d、3 ノート）について:$'));
  // 飛び飛びのノートは 1 つずつ並べる
  const far = await win.evaluate(() => { const P = window.__app.S.pitched; return [P[1].id, P[4].id]; });
  await win.evaluate((s) => { window.__app.S.sel = s; window.__app.render(); }, far);
  const t2 = await win.evaluate(() => window.__app.askText().text);
  expect(t2).toContain(`${far[0]}・${far[1]}（`);
  await win.evaluate(() => { window.__app.S.sel = []; window.__app.render(); });
});

test('(A8) AI の操作の印: 「元に戻す: AI · ピッチ」・ノートの縁', async () => {
  const id = await win.evaluate(() => window.__app.S.pitched[4].id);
  await win.evaluate((nid) => {
    const n = window.__app.S.byId.get(nid);
    window.__app.S.view = { t0: Math.max(0, n.edited_start_sec - 0.3), span: 1.4 };
    window.__app.render();
  }, id);
  await win.evaluate(async (nid) => {
    await window.api.call('shift_pitch', { cents: 40, note_id: nid, author: 'ai' });
    await window.__app.refresh();
  }, id);
  expect(await win.evaluate(() => window.__app.undoTitle())).toContain('元に戻す: AI · ピッチ');
  const tpl = await win.evaluate(() => window.__app.appMenuTemplate());
  expect(tpl.find((m) => m.label === '編集').submenu.find((x) => x.cmd === 'undo').label).toBe('元に戻す: AI · ピッチ');
  expect(await win.evaluate(() => window.__app.aiNotes())).toEqual([id]);
  await expect(win.locator(`#roll path[data-ai="${id}"]`)).toHaveCount(1);
  if (SHOTS) await win.screenshot({ path: path.join(SHOTS, 'ai-connect-ai-edit.png') });
  // 人が上から編集すると AI の印は外れる（履歴の表示も人の操作）
  await win.evaluate(async (nid) => {
    await window.api.call('shift_pitch', { cents: -40, note_id: nid, author: 'human' });
    await window.__app.refresh();
  }, id);
  expect(await win.evaluate(() => window.__app.aiNotes())).toEqual([]);
  await expect(win.locator('#roll path[data-ai]')).toHaveCount(0);
  expect(await win.evaluate(() => window.__app.undoTitle())).toMatch(/元に戻す: ピッチ/);
  // 取り消しても（AI の操作が最後に戻る）印は戻る。ステータスにも AI · が付く
  await win.locator('#mock').focus();
  await win.keyboard.press('Control+z');
  await settle();
  expect(await win.evaluate(() => window.__app.aiNotes())).toEqual([id]);
  await win.keyboard.press('Control+z');
  await settle();
  expect(await win.evaluate(() => window.__app.status())).toContain('元に戻した: AI · ピッチ');
  expect(await win.evaluate(() => window.__app.aiNotes())).toEqual([]);
  expect(errors).toEqual([]);
});
