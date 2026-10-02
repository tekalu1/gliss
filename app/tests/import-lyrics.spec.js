import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'C');

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO = path.dirname(APP);
const TAKE = M.clip('C');
const PROJECT = path.join(REPO, 'projects', '_test-score-import');
const USERDATA = `${PROJECT}-userdata`;
const SCORE = path.join(REPO, 'projects', '_test-score-import.svp');
const BLICK = 705600000;

test('ファイルメニューから SVP を読み込み、歌詞を戻せる', async () => {
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({ args: [APP, '--take', TAKE, '--project-dir', PROJECT,
    '--user-data-dir', USERDATA, '--mute'], env });
  try {
    const win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
    const notes = await win.evaluate(() => window.__app.S.pitched.map((n) => ({
      start_sec: n.start_sec, end_sec: n.end_sec, pitch_midi: n.pitch_midi,
    })).filter((n) => n.pitch_midi != null));
    expect(notes.length).toBeGreaterThanOrEqual(8);
    const score = { time: { tempo: [{ position: 0, bpm: 120 }] }, tracks: [{
      name: '自作テスト', mainGroup: { notes: notes.map((n) => ({
        onset: Math.round(n.start_sec * BLICK * 2),
        duration: Math.max(1, Math.round((n.end_sec - n.start_sec) * BLICK * 2)),
        pitch: Math.round(n.pitch_midi), lyrics: 'あ',
      })) },
    }] };
    fs.writeFileSync(SCORE, JSON.stringify(score), 'utf8');
    const menu = await win.evaluate(() => window.__app.appMenuTemplate()[0].submenu);
    expect(menu.some((item) => item.cmd === 'import-lyrics' && item.enabled)).toBe(true);
    const dry = await win.evaluate((p) => window.api.call('import_lyrics', { path: p, dry_run: true }), SCORE);
    expect(dry.ok).toBe(true);
    expect(dry.entries.length).toBeGreaterThan(0);
    await app.evaluate(({ dialog }, p) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [p] });
    }, SCORE);
    await win.evaluate(() => window.__app.onMenu({ cmd: 'import-lyrics' }));
    await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
    const imported = await win.evaluate(() => window.__app.lyricsEntries());
    expect(imported.length).toBeGreaterThan(0);
    expect(imported[0].text).toContain('あ');
    await win.evaluate(() => window.__app.undo());
    await win.waitForFunction(() => window.__app.idle(), null, { timeout: 120000 });
    expect(await win.evaluate(() => window.__app.lyricsEntries())).toEqual([]);
  } finally {
    await app.close();
    fs.rmSync(SCORE, { force: true });
  }
});
