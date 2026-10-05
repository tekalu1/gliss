import { test, expect } from '@playwright/test';

test('release, blur and host playback cancel an ARA preview even while native start is pending', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const listeners = new Map();
  const calls = [];
  let pendingStart = null;
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
    stopPreview();
    expect(calls.at(-1)).toBe('stop');
    pendingStart({ ok: true }); await settle();
    expect(previewState().sounding).toBeNull();

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
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
  }
});
