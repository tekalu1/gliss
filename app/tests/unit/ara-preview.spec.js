import { test, expect } from '@playwright/test';

test('release, blur and host playback cancel an ARA preview even while native start is pending', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldCustomEvent = globalThis.CustomEvent;
  const listeners = new Map();
  const calls = [];
  const phases = [];
  let pendingStart = null;
  globalThis.CustomEvent = class {
    constructor(type, init) { this.type = type; this.detail = init.detail; }
  };
  globalThis.window = {
    api: {
      mode: 'ara',
      call: async (tool) => ({ ok: true, path: tool === 'render_audition' ? 'preview.wav' : '' }),
      preview: (op) => {
        calls.push(op);
        if (op === 'stop') return Promise.resolve({ ok: true });
        return new Promise((resolve) => { pendingStart = resolve; });
      },
    },
    addEventListener: (name, fn) => listeners.set(name, fn),
    dispatchEvent: (event) => { if (event.type === 'gliss-preview-state') phases.push(event.detail); },
  };
  globalThis.document = {
    documentElement: { dataset: {} }, hidden: false,
    querySelector: () => null,
    addEventListener: (name, fn) => listeners.set(name, fn),
  };
  try {
    const { S } = await import('../../renderer/state.js');
    const { startPreview, stopPreview, previewState } = await import('../../renderer/audio.js');
    S.playing = false; S.vd = {}; S.local.pitch = new Map();
    S.byId = new Map([['n1', { id: 'n1', kind: 'note', start_sec: 0.1, end_sec: 0.5 }]]);
    const settle = async () => { for (let i = 0; i < 8; ++i) await Promise.resolve(); };

    startPreview('n1'); await settle();
    expect(calls).toContain('start');
    expect(previewState().phase).toBe('preparing');
    expect(phases.map((x) => x.phase)).toEqual(['preparing']);
    stopPreview();
    expect(calls.at(-1)).toBe('stop');
    expect(phases.at(-1).phase).toBe('idle');
    pendingStart({ ok: true }); await settle();
    expect(previewState().sounding).toBeNull();
    expect(phases.at(-1).phase).toBe('idle');

    startPreview('n1'); await settle();
    listeners.get('blur')();
    expect(calls.at(-1)).toBe('stop');
    pendingStart({ ok: true }); await settle();
    expect(previewState().phase).toBe('idle');

    startPreview('n1'); await settle();
    listeners.get('gliss-host-play')();
    expect(calls.at(-1)).toBe('stop');
    pendingStart({ ok: true }); await settle();
    S.playing = true;
    expect(previewState().sounding).toBeNull();
    S.playing = false;
    expect(previewState().sounding).toBeNull();

    startPreview('n1'); await settle();
    pendingStart({ ok: true }); await settle();
    expect(phases.at(-1)).toMatchObject({ phase: 'sounding', sounding: { cents: 0 } });
    stopPreview();
    expect(phases.at(-1)).toMatchObject({ phase: 'idle', sounding: null });
    const eventCount = phases.length;
    stopPreview();
    expect(phases).toHaveLength(eventCount);

    for (const [reason, message] of [
      ['no-editor-renderer', 'この DAW では試聴出力を使えません'],
      ['host-playing', 'DAW の再生中は試聴できません'],
      ['invalid-audio', '試聴用の音を読み込めません'],
      ['invalid-path', '試聴用の音を読み込めません'],
      ['unexpected-code', 'DAW で試聴を開始できません'],
    ]) {
      startPreview('n1'); await settle();
      pendingStart({ ok: false, reason }); await settle();
      expect(previewState()).toMatchObject({ phase: 'error', error: message, sounding: null });
      stopPreview();
    }

    const beforeCancel = phases.length;
    startPreview('n1'); await settle();
    pendingStart({ ok: false, reason: 'cancelled' }); await settle();
    expect(previewState()).toMatchObject({ phase: 'idle', error: null, sounding: null });
    expect(phases.slice(beforeCancel).map((x) => x.phase)).toEqual(['preparing', 'idle']);
    expect(calls.at(-1)).toBe('stop');
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.CustomEvent = oldCustomEvent;
  }
});
