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
        if (op === 'start' && arg?.local) { calls.push('local'); return Promise.resolve({ ok: false, reason: 'not-cached' }); }
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
    // 古い応答・編集の最中の応答は、捨てずに待って取り直す（押している間）。この試験では取り直しを止めて、残りの応答も流す
    const drain = async () => { stopPreview(); while (pendingRenders.length) pendingRenders.shift()(rendered('drained.wav')); await settle(); };

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

    {
      startPreview('n1'); await settle();
      expect(calls.at(-1)).toBe('start');
      listeners.get('gliss-ara-selection')({ detail: { track_id: 'track-2', ara_id: 'mod-2' } });   // 別のトラックへ変わった: 止める
      expect(calls.at(-1)).toBe('stop');
      pendingStart({ ok: true }); await settle();
      expect(previewState()).toMatchObject({ phase: 'idle', sounding: null });
      // DAW の選択が空になっただけ（何も選んでいない）: 止めない
      startPreview('n1'); await settle();
      expect(calls.at(-1)).toBe('start');
      listeners.get('gliss-ara-selection')({ detail: null });
      expect(calls.at(-1)).toBe('start');
      pendingStart({ ok: true }); await settle();
      expect(previewState().phase).toBe('sounding');
      stopPreview();
    }

    startPreview('n1'); await settle();
    S.queued = 1;                                            // 渡している間に編集が順番待ちに入った
    pendingStart({ ok: true }); await settle();
    expect(calls.at(-1)).toBe('stop');                       // 古い音は止める
    expect(previewState()).toMatchObject({ phase: 'preparing', sounding: null });   // 押している間は捨てず、待って取り直す
    S.queued = 0;
    await new Promise((r) => setTimeout(r, 120));
    expect(calls.at(-1)).toBe('start');                      // 編集が済んだので、今の状態で作り直して渡した
    stopPreview();
    pendingStart({ ok: true }); await settle();

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
    expect(renderCalls).toBeGreaterThanOrEqual(beforeChangedView + 1);
    pendingRenders.shift()(rendered('stale.wav')); await settle();
    expect(calls.filter((x) => x === 'start')).toHaveLength(beforeChangedViewStarts);
    expect(previewState().phase).toBe('preparing');          // 捨てずに、待って取り直す
    await drain();
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
      expect(previewState().phase).toBe('preparing');
      await drain();
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
      expect(previewState().phase).toBe('preparing');          // 捨てずに待つ
      if (S.session.current !== 'track-1') {                   // 別のトラックへ移った: 取り直さずに止める
        await new Promise((r) => setTimeout(r, 150));
        expect(previewState().phase).toBe('idle');
      }
      await drain();
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

// 押した瞬間から試聴を始める（長押しの判定の 250 ms を待たない）。ずらさない試聴は、プラグインが持つ編集済みの音から
// エンジンを呼ばずに鳴らす（local）。編集が続いていて作れなかったときは、押している間、待って取り直す（黙って捨てない）。
// 離したときは、鳴り始めてから最短（200 ms）だけ鳴らして止める。子音（アタック）の頭から鳴らす。
test('ARA preview: starts at pointerdown, serves cents=0 locally, retries while busy, keeps a minimum sound, includes the attack', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldCustomEvent = globalThis.CustomEvent;
  const log = [];
  let renderCalls = 0;
  let renderMs = 0;
  let localAnswer = { ok: false, reason: 'not-cached' };
  let renderArgs = [];
  let t0 = 0;
  const since = () => performance.now() - t0;
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
  globalThis.window = {
    api: {
      mode: 'ara',
      call: async (tool, args) => {
        if (tool !== 'render_audition') return { ok: true };
        renderCalls++;
        renderArgs.push(args);
        await new Promise((r) => setTimeout(r, renderMs));
        return { ok: true, path: 'preview.wav', view_rev: 'view-1', rev: 'audio-1', track_id: 'track-1',
          ara_id: 'mod-1', note_id: args.note_id, cents: args.cents, source_id: 'source-1' };
      },
      preview: async (op, arg) => {
        if (op === 'start' && arg?.local) { log.push(['local', since(), arg]); return localAnswer; }
        log.push([op, since(), arg]);
        return { ok: true };
      },
    },
    addEventListener: () => {},
    dispatchEvent: () => {},
  };
  globalThis.document = { documentElement: { dataset: {} }, hidden: false, querySelector: () => null, addEventListener: () => {} };
  try {
    const { S } = await import('../../renderer/state.js');
    const { startPreview, stopPreview, releasePreview, previewState, markNoteEdited, clearEditMarks } = await import('../../renderer/audio.js');
    const note = { id: 'n1', kind: 'note', start_sec: 1.0, end_sec: 1.5, pitch_editable: true };
    const attack = { id: 'u1', kind: 'unvoiced', start_sec: 0.9, end_sec: 1.0 };
    const breath = { id: 'b1', kind: 'breath', start_sec: 2.0, end_sec: 2.3 };
    S.playing = false; S.vd = { view_rev: 'view-1' }; S.local.pitch = new Map();
    S.session = { current: 'track-1' }; S.tracks = [{ id: 'track-1', ara_id: 'mod-1' }];
    S.projectDir = 'synthetic-project'; S.busy = false; S.queued = 0; S.pendingPlan = false;
    S.notes = [attack, note, breath];
    S.byId = new Map(S.notes.map((n) => [n.id, n]));
    const reset = (ms = 0) => { stopPreview(); clearEditMarks(); log.length = 0; renderCalls = 0; renderArgs = []; renderMs = ms; localAnswer = { ok: false, reason: 'not-cached' }; t0 = performance.now(); };
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const names = () => log.map((x) => x[0]);

    // 押した瞬間にエンジンへ（長押しの判定を待たない）。子音（アタック）の頭から
    reset(60);
    startPreview('n1');
    await wait(160);
    expect(renderCalls).toBe(1);
    expect(renderArgs[0].start_sec).toBeCloseTo(0.9, 6);        // アタックの子音から鳴らす
    expect(renderArgs[0].end_sec).toBeCloseTo(1.5, 6);
    const start = log.find((x) => x[0] === 'start');
    expect(start[1]).toBeGreaterThanOrEqual(50);
    expect(start[1]).toBeLessThan(140);                          // 押してから render_audition の時間 + ごく少し
    expect(previewState().phase).toBe('sounding');

    // ずらさない試聴は、プラグインの編集済みの音から（エンジンを呼ばない）
    reset(500);
    localAnswer = { ok: true };
    startPreview('n1');
    await wait(40);
    expect(renderCalls).toBe(0);
    expect(names()).toEqual(['local']);
    expect(log[0][2]).toMatchObject({ local: true, ara_id: 'mod-1', cents: 0, allow_stale: true });
    expect(log[0][1]).toBeLessThan(30);
    expect(previewState().phase).toBe('sounding');
    // 編集したばかりのノートは、キャッシュが追いついていない（stale）ことがあるので厳密に（allow_stale: false）
    reset(0);
    markNoteEdited(['n1']);
    localAnswer = { ok: false, reason: 'stale' };
    startPreview('n1');
    await wait(80);
    expect(log.find((x) => x[0] === 'local')[2].allow_stale).toBe(false);
    expect(renderCalls).toBe(1);                                  // 追いついていなければエンジンで作る
    // 子音・息も鳴らせる（cents 0）
    reset(0);
    startPreview('b1');
    await wait(60);
    expect(renderArgs[0]).toMatchObject({ note_id: 'b1', cents: 0, start_sec: 2.0, end_sec: 2.3 });

    // 編集が続いている（S.busy）間に押しても、黙って捨てず、編集が終わったら鳴らす
    reset(0);
    S.busy = true;
    startPreview('n1');
    await wait(100);
    expect(names()).not.toContain('start');
    expect(previewState().phase).toBe('preparing');
    S.busy = false;
    await wait(150);
    expect(names()).toContain('start');
    expect(previewState().phase).toBe('sounding');
    // render_audition の応答を待っている間に編集が始まった（描画データの版が変わった）: 古い音を渡さず、取り直す
    reset(80);
    startPreview('n1');
    await wait(30);
    S.vd = { view_rev: 'view-2' };
    await wait(120);
    expect(names()).not.toContain('start');
    expect(renderCalls).toBeGreaterThanOrEqual(1);
    S.vd = { view_rev: 'view-1' };
    await wait(250);
    expect(names()).toContain('start');
    expect(previewState().phase).toBe('sounding');
    stopPreview();

    // 離しても、鳴り始めてから最短（200 ms）は鳴らす
    reset(0);
    localAnswer = { ok: true };
    startPreview('n1');
    await wait(30);
    expect(previewState().phase).toBe('sounding');
    releasePreview();
    await wait(80);
    expect(names()).not.toContain('stop');
    await wait(200);
    expect(names()).toContain('stop');
    const stopAt = log.find((x) => x[0] === 'stop')[1];
    expect(stopAt).toBeGreaterThanOrEqual(190);
    // まだ鳴り始める前に離しても、鳴り始めたら最短だけ鳴らして止める
    reset(100);
    startPreview('n1');
    await wait(20);
    releasePreview();
    await wait(60);
    expect(names()).not.toContain('stop');
    await wait(400);
    expect(names()).toContain('start');
    expect(names()).toContain('stop');
    expect(log.find((x) => x[0] === 'stop')[1]).toBeGreaterThan(log.find((x) => x[0] === 'start')[1] + 150);
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.CustomEvent = oldCustomEvent;
  }
});
