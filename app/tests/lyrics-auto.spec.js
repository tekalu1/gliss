import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'H');

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO = path.dirname(APP);
const TAKE = M.clip('H');
const PROJECT = path.join(REPO, 'projects', '_test-lyrics-auto');
const USERDATA = `${PROJECT}-userdata`;

test('推定の歌詞を表示し、1音節を直して戻せる', async () => {
  fs.rmSync(PROJECT, { recursive: true, force: true });
  fs.rmSync(USERDATA, { recursive: true, force: true });
  const env = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
    VOCAL_ENGINE_AUTO_LYRICS: '1' };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({
    args: [APP, '--take', TAKE, '--project-dir', PROJECT,
      '--user-data-dir', USERDATA, '--mute'], env,
  });
  try {
    const win = await app.firstWindow();
    await win.waitForFunction(() => window.__app?.ready()
      && window.__app.lyricsEntries().some((e) => e.origin === 'estimated'),
    null, { timeout: 240000 });
    const before = await win.evaluate(() => window.__app.lyricsEntries()[0]);
    expect(before.estimate.reading).toBe(before.text);
    const kana = win.locator('#roll text[data-kana]').first();
    await expect(kana).toBeVisible();
    expect(await kana.getAttribute('fill')).toBe('#85858a');
    await win.screenshot({ path: path.join(APP, 'screenshots', 'screenshot-lyrics-auto.png') });

    const box = await kana.boundingBox();
    await win.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
    await expect(win.locator('#lyr')).toBeVisible();
    expect(await win.locator('#lyrIn').inputValue()).toBe((await kana.textContent()));
    await win.locator('#lyrIn').fill('ま');
    await win.keyboard.press('Enter');
    await win.waitForFunction(() => window.__app.idle()
      && window.__app.lyricsEntries()[0]?.confirmed_syllables?.includes(0),
    null, { timeout: 120000 });
    const after = await win.evaluate(() => window.__app.lyricsEntries()[0]);
    expect(after.text).toContain('ま');
    expect(after.origin).toBe('estimated');
    expect(after.confirmed_syllables).toEqual([0]);
    expect(after.estimate.reading).toBe(before.text);
    expect(await win.locator('#roll text[data-kana="0"]').getAttribute('fill')).toBe('#d6d6d6');
    expect(await win.locator('#roll text[data-kana="1"]').getAttribute('fill')).toBe('#85858a');
    await win.locator('#mock').focus();
    await win.keyboard.press('Control+z');
    await win.waitForFunction((text) => window.__app.idle()
      && window.__app.lyricsEntries()[0]?.text === text,
    before.text, { timeout: 120000 });
    expect((await win.evaluate(() => window.__app.lyricsEntries()[0])).origin).toBe('estimated');

    const target = await win.evaluate(() => {
      const syllables = window.__app.S.ph.syllables;
      const n = window.__app.S.pitched.find((note) => note.end_sec - note.start_sec > 0.06
        && syllables.some((s) => s.end_sec > note.start_sec && s.start_sec < note.end_sec));
      if (!n) return null;
      window.__app.S.view = { t0: Math.max(0, n.start_sec - 0.2), span: n.end_sec - n.start_sec + 0.4 };
      window.__app.setTool('cut');
      return n.id;
    });
    expect(target).toBeTruthy();
    const labelCount = await win.locator('#roll text[data-kana]').count();
    const body = win.locator(`#roll rect[data-note="${target}"]:not([data-edge])`).first();
    const bounds = await body.boundingBox();
    await win.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await win.waitForFunction((id) => window.__app.idle()
      && window.__app.S.notes.some((n) => n.id.startsWith(`${id}@`)), target);
    expect(await win.locator('#roll text[data-kana]').count()).toBeLessThan(labelCount);
    await win.keyboard.press('Control+z');
    await win.waitForFunction((id) => window.__app.idle()
      && !window.__app.S.notes.some((n) => n.id.startsWith(`${id}@`)), target);
    expect(await win.locator('#roll text[data-kana]').count()).toBe(labelCount);
  } finally {
    await app.close();
  }
});
