import { test, expect } from '@playwright/test';

test('release, blur and host playback cancel an ARA preview even while native start is pending', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldCustomEvent = globalThis.CustomEvent;
  const listeners = new Map();
  const calls = [];
  const startArgs = [];
  const phases = [];
  let pendingStart = null;
  let holdRender = false;
  let renderCalls = 0;
  const pendingRenders = [];
  const rendered = (path, overrides = {}) => ({ ok: true, path, view_rev: 'view-1',
    rev: 'audio-1', track_id: 'track-1', ara_id: 'mod-1', note_id: 'n1', cents: 0,
    source_id: 'source-1', ...overrides });
  globalThis.CustomEvent = class {
    constructor(type, init) { this.type = type; this.detail = init.detail; }
  };
  globalThis.window = {
    api: {
      mode: 'ara',
      call: async (tool) => {
        if (tool !== 'render_audition') return { ok: true };
        renderCalls++;
        if (holdRender) return new Promise((resolve) => { pendingRenders.push(resolve); });
        return rendered('preview.wav');
      },
      preview: (op, arg) => {
        calls.push(op);
        if (op === 'start') startArgs.push(arg);
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
    const { preparePreview, startPreview, stopPreview, previewState } = await import('../../renderer/audio.js');
    S.playing = false; S.vd = { view_rev: 'view-1' }; S.local.pitch = new Map();
    S.session = { current: 'track-1' }; S.tracks = [{ id: 'track-1', ara_id: 'mod-1' }];
    S.projectDir = 'synthetic-project'; S.busy = false; S.queued = 0; S.pendingPlan = false;
    S.byId = new Map([
      ['n1', { id: 'n1', kind: 'note', start_sec: 0.1, end_sec: 0.5 }],
      ['n2', { id: 'n2', kind: 'note', start_sec: 0.6, end_sec: 0.9 }],
    ]);
    const settle = async () => { for (let i = 0; i < 8; ++i) await Promise.resolve(); };

    startPreview('n1'); await settle();
    expect(calls).toContain('start');
    // 試聴を足す EditorRenderer を選ぶために、ノートの修飾の ara_id を native へ渡す
    expect(startArgs.at(-1)).toMatchObject({ note: 'n1', ara_id: 'mod-1' });
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

    for (const selection of [{ track_id: 'track-2', ara_id: 'mod-2' }, null]) {
      startPreview('n1'); await settle();
      expect(calls.at(-1)).toBe('start');
      listeners.get('gliss-ara-selection')({ detail: selection });
      expect(calls.at(-1)).toBe('stop');
      pendingStart({ ok: true }); await settle();
      expect(previewState()).toMatchObject({ phase: 'idle', sounding: null });
    }

    startPreview('n1'); await settle();
    S.queued = 1;
    pendingStart({ ok: true }); await settle();
    expect(calls.at(-1)).toBe('stop');
    expect(previewState()).toMatchObject({ phase: 'idle', sounding: null });
    S.queued = 0;

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

    // Pointerdown prepares corrected PCM without granting sound. A short click cancels it.
    holdRender = true;
    const nativeStarts = calls.filter((x) => x === 'start').length;
    preparePreview('n1');
    expect(renderCalls).toBeGreaterThan(0);
    expect(previewState().phase).toBe('idle');
    stopPreview();
    pendingRenders.shift()(rendered('old.wav')); await settle();
    expect(calls.filter((x) => x === 'start')).toHaveLength(nativeStarts);

    const beforeBurst = renderCalls;
    for (let i = 0; i < 12; i++) { preparePreview(i % 2 ? 'n1' : 'n2'); stopPreview(); }
    expect(renderCalls).toBe(beforeBurst + 1);
    pendingRenders.shift()(rendered('burst.wav')); await settle();
    expect(calls.filter((x) => x === 'start')).toHaveLength(nativeStarts);

    // The held gesture reuses its one render; an old view generation cannot be reused.
    preparePreview('n1');
    const beforeHeld = renderCalls;
    holdRender = false;
    pendingRenders.shift()(rendered('fresh.wav')); await settle();
    startPreview('n1'); await settle();
    expect(renderCalls).toBe(beforeHeld);
    expect(calls.filter((x) => x === 'start')).toHaveLength(nativeStarts + 1);
    pendingStart({ ok: true }); await settle();
    stopPreview();

    holdRender = true;
    preparePreview('n1');
    const oldView = S.vd;
    S.vd = { view_rev: 'view-2' };
    holdRender = false;
    const beforeChangedView = renderCalls;
    const beforeChangedViewStarts = calls.filter((x) => x === 'start').length;
    startPreview('n1'); await settle();
    expect(renderCalls).toBe(beforeChangedView + 1);
    pendingRenders.shift()(rendered('stale.wav')); await settle();
    expect(calls.filter((x) => x === 'start')).toHaveLength(beforeChangedViewStarts);
    expect(previewState().phase).toBe('idle');
    S.vd = oldView;

    // An optional-field check would silently accept an older engine response.
    for (const stale of [
      rendered('missing-revision.wav', { view_rev: undefined }),
      rendered('missing-audio-revision.wav', { rev: undefined }),
      rendered('wrong-track.wav', { track_id: 'other-track' }),
      rendered('wrong-modification.wav', { ara_id: 'other-mod' }),
    ]) {
      holdRender = true;
      startPreview('n1'); await settle();
      const starts = calls.filter((x) => x === 'start').length;
      pendingRenders.shift()(stale); await settle();
      expect(calls.filter((x) => x === 'start')).toHaveLength(starts);
      expect(previewState().phase).toBe('idle');
    }

    // Refresh, pending edits and a host selection change can occur during render.
    for (const change of [
      () => { S.vd = { view_rev: 'view-2' }; },
      () => { S.queued = 1; },
      () => { S.session.current = 'other-track'; },
    ]) {
      holdRender = true;
      startPreview('n1'); await settle();
      const starts = calls.filter((x) => x === 'start').length;
      change();
      pendingRenders.shift()(rendered('late.wav')); await settle();
      expect(calls.filter((x) => x === 'start')).toHaveLength(starts);
      expect(previewState().phase).toBe('idle');
      S.vd = oldView; S.queued = 0; S.session.current = 'track-1';
    }

    const { viewData } = await import('../../renderer/engine.js');
    const originalCall = window.api.call;
    window.api.call = async (tool) => tool === 'export_view_data'
      ? { ok: true, path: 'cache/view/range-specific-name.json', view_rev: 'unqualified-view-revision' }
      : originalCall(tool);
    window.api.readJson = async () => ({});
    expect((await viewData({ start_sec: 0.1, end_sec: 0.5 })).view_rev)
      .toBe('unqualified-view-revision');
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.CustomEvent = oldCustomEvent;
  }
});

// 長押し（250 ms）の判定の後すぐ鳴らす: 押した時点で render_audition を始め（preparePreview）、判定の時点でその結果を
// 使う（startPreview）ので、native への開始の要求は max(判定の時間, render_audition の時間) の後に 1 回だけ出る。
// 判定の後にもう一度 render_audition を呼ぶ（待ちが 2 倍になる）ことがない。
test('ARA preview starts right after the hold using the render prepared at pointerdown', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldCustomEvent = globalThis.CustomEvent;
  const natives = [];
  let renderCalls = 0;
  let renderMs = 0;
  let t0 = 0;
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
  globalThis.window = {
    api: {
      mode: 'ara',
      call: async (tool) => {
        if (tool !== 'render_audition') return { ok: true };
        renderCalls++;
        await new Promise((r) => setTimeout(r, renderMs));
        return { ok: true, path: 'preview.wav', view_rev: 'view-1', rev: 'audio-1', track_id: 'track-1',
          ara_id: 'mod-1', note_id: 'n1', cents: 0, source_id: 'source-1' };
      },
      preview: async (op) => {
        if (op === 'start') natives.push(performance.now() - t0);
        return { ok: true };
      },
    },
    addEventListener: () => {},
    dispatchEvent: () => {},
  };
  globalThis.document = { documentElement: { dataset: {} }, hidden: false, querySelector: () => null, addEventListener: () => {} };
  try {
    const { S } = await import('../../renderer/state.js');
    const { preparePreview, startPreview, stopPreview } = await import('../../renderer/audio.js');
    S.playing = false; S.vd = { view_rev: 'view-1' }; S.local.pitch = new Map();
    S.session = { current: 'track-1' }; S.tracks = [{ id: 'track-1', ara_id: 'mod-1' }];
    S.projectDir = 'synthetic-project'; S.busy = false; S.queued = 0; S.pendingPlan = false;
    S.byId = new Map([['n1', { id: 'n1', kind: 'note', start_sec: 0.1, end_sec: 0.5 }]]);
    const HOLD = 250;
    const press = async (ms) => {
      stopPreview();
      natives.length = 0; renderCalls = 0; renderMs = ms;
      t0 = performance.now();
      preparePreview('n1');                                   // pointerdown
      await new Promise((r) => setTimeout(r, HOLD));
      startPreview('n1');                                     // 長押しの判定
      await new Promise((r) => setTimeout(r, Math.max(0, ms - HOLD) + 120));
    };

    await press(60);                                          // 速いエンジン: 判定の直後に native へ
    expect(renderCalls).toBe(1);
    expect(natives).toHaveLength(1);
    expect(natives[0]).toBeGreaterThanOrEqual(HOLD - 10);
    expect(natives[0]).toBeLessThan(HOLD + 60);

    await press(400);                                         // 遅いエンジン: 押してから 400 ms で（判定 + 400 ms ではない）
    expect(renderCalls).toBe(1);
    expect(natives).toHaveLength(1);
    expect(natives[0]).toBeGreaterThanOrEqual(400 - 10);
    expect(natives[0]).toBeLessThan(400 + 60);
    stopPreview();
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.CustomEvent = oldCustomEvent;
  }
});
