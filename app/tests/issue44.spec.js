// Issue #44: 音程のない区間もはさみで分け、幅・補正表示・undo が使える。
import { test, expect, _electron as electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from './materials.js';

M.skipUnlessReady(test, 'A', 'C', 'E');

const APP = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const ROOT = path.dirname(APP);
const SHOTS = path.join(ROOT, 'scratchpad');

for (const [kind, clip] of [
  ['unvoiced', 'A'],
  ['breath', 'C'],
  ['silence', 'E'],
]) {
  test(`${kind}: 分割、端のプレビュー、補正色、元の長さ、undo`, async () => {
    const project = path.join(ROOT, 'projects', `_test-issue44-${kind}`);
    const userData = `${project}-userdata`;
    fs.rmSync(project, { recursive: true, force: true });
    fs.rmSync(userData, { recursive: true, force: true });
    const env = { ...process.env, ...M.RMVPE_ENV, ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
      VOCAL_EDITOR_MUTE: '1', VOCAL_ENGINE_CWD: path.join(ROOT, 'engine') };
    delete env.ELECTRON_RUN_AS_NODE;
    const app = await electron.launch({ args: [APP, '--take', M.clip(clip),
      '--project-dir', project, '--user-data-dir', userData, '--mute'], env });
    try {
      const win = await app.firstWindow();
      const errors = [];
      win.on('pageerror', (e) => errors.push(e.message));
      await win.waitForFunction(() => window.__app?.ready(), null, { timeout: 240000 });
      const target = await win.evaluate((k) => {
        const n = window.__app.S.notes.find((v) => v.kind === k && v.end_sec - v.start_sec > 0.06);
        if (!n) return null;
        window.__app.S.view = { t0: Math.max(0, n.start_sec - 0.2), span: n.end_sec - n.start_sec + 0.4 };
        window.__app.setTool('cut');
        return { id: n.id, start: n.start_sec, end: n.end_sec };
      }, kind);
      expect(target).not.toBeNull();
      const body = win.locator(`#roll rect[data-note="${target.id}"]:not([data-edge])`).first();
      const b = await body.boundingBox();
      expect(b).not.toBeNull();
      await win.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
      await expect.poll(() => win.evaluate(() => window.__app.cutHover()?.id)).toBe(target.id);
      await win.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
      await win.waitForFunction(() => window.__app.idle() && window.__app.S.notes.some((n) => n.id.includes('@')));
      const right = await win.evaluate((id) => window.__app.S.notes.find((n) => n.id.startsWith(`${id}@`))?.id, target.id);
      expect(right).toBeTruthy();
      const pieces = await win.evaluate(([left, rightId]) => {
        const a = window.__app.S.byId.get(left); const b = window.__app.S.byId.get(rightId);
        return [a.kind, b.kind, a.end_sec, b.start_sec];
      }, [target.id, right]);
      expect(pieces.slice(0, 2)).toEqual([kind, kind]);
      expect(pieces[2]).toBeCloseTo(pieces[3], 4);

      await win.evaluate(() => window.__app.setTool('main'));
      const rightBody = win.locator(`#roll rect[data-nop="${right}"]:not([data-nop-edge])`).first();
      await rightBody.click();
      expect(await win.evaluate((id) => window.__app.S.sel.includes(id), right)).toBe(true);
      const edge = win.locator(`#roll rect[data-nop="${right}"][data-nop-edge="end"]`).first();
      await expect(edge).toHaveCount(1);
      const e = await edge.boundingBox();
      await win.mouse.move(e.x + e.width / 2, e.y + e.height / 2);
      await win.mouse.down();
      await win.mouse.move(e.x + e.width / 2 + 24, e.y + e.height / 2, { steps: 8 });
      await win.waitForFunction(() => Math.abs(window.__app.plan()?.x || 0) > 0.001);
      const during = await win.evaluate((id) => ({
        span: window.__app.noPitch().find((n) => n.id === id),
        corr: window.__app.corr().find((n) => n.id === id),
      }), right);
      expect(during.corr.band).toBe('#ffffff');
      await win.mouse.up();
      await win.waitForFunction(() => window.__app.idle());
      const after = await win.evaluate((id) => ({
        span: window.__app.noPitch().find((n) => n.id === id),
        corr: window.__app.corr().find((n) => n.id === id),
        orig: window.__app.drawnColors().orig.find((n) => n.id === id),
        fill: document.querySelector(`#roll path[data-nopitch="${id}"]`)?.getAttribute('fill'),
      }), right);
      expect(after.span.e).toBeCloseTo(during.span.e, 2);
      expect(after.corr.engTiming.manual).toBe(true);
      expect(after.corr.band).toBe('#ffffff');
      expect(after.fill).toBe('#ffffff');
      expect(after.orig?.stroke).toBe('#a4a4aa');
      fs.mkdirSync(SHOTS, { recursive: true });
      await win.screenshot({ path: path.join(SHOTS, `issue44-${kind}.png`) });

      await win.keyboard.press('Control+z');
      await win.waitForFunction(() => window.__app.idle());
      expect((await win.evaluate((id) => window.__app.corr().find((n) => n.id === id)?.timing, right))).toBeNull();
      const ramp = await win.evaluate((id) => {
        const n = window.__app.S.byId.get(id);
        n.timing_corr = { amount_ms: 20, degree: 0.25, manual: false };
        window.__app.render();
        const amber = document.querySelector(`#roll path[data-nopitch="${id}"]`)?.getAttribute('fill');
        n.timing_corr = { amount_ms: 80, degree: 1, manual: false };
        window.__app.render();
        const red = document.querySelector(`#roll path[data-nopitch="${id}"]`)?.getAttribute('fill');
        return { amber, red };
      }, right);
      expect(ramp.amber).not.toBe('#e6d24a');
      expect(ramp.red).toBe('#d23a2a');
      await win.screenshot({ path: path.join(SHOTS, `issue44-auto-${kind}.png`) });
      await win.evaluate((id) => { window.__app.S.byId.get(id).timing_corr = null; window.__app.render(); }, right);
      await win.evaluate(() => window.__app.setTool('cut'));
      const join = win.locator(`#roll rect[data-join="${target.id}|${right}"]`).first();
      await expect(join).toHaveCount(1);
      await join.dblclick({ delay: 100 });
      await win.waitForFunction((id) => window.__app.idle() && !window.__app.S.byId.has(id), right);
      await win.keyboard.press('Control+z');
      await win.waitForFunction((id) => window.__app.idle() && window.__app.S.byId.has(id), right);
      await win.keyboard.press('Control+z');
      await win.waitForFunction(() => window.__app.idle());
      expect(await win.evaluate((id) => window.__app.S.byId.has(id), right)).toBe(false);
      expect(errors).toEqual([]);
    } finally {
      await app.close();
    }
  });
}
