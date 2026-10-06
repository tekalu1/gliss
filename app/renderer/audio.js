// 再生: **全トラックを混ぜて鳴らす**（issue #7。`docs/track-view.md` §2）。
//
//  - エンジンの `render_tracks` がトラックごとの音のファイルを返す（編集のあるボーカルは編集を当てた音、
//    他は元のファイル）。main 経由で読み、Web Audio でタイムライン上の位置（start_sec）に置いて鳴らす。
//  - **ミュート／ソロ・音量はトラックごとの GainNode、パンはその次の StereoPannerNode**（再生中に動かしても、
//    その場で聞こえ方が変わる）。**再生だけに効く**（書き出し・render_tracks の音には入らない）。
//    ガイドのトラックも他と同じく鳴る（テイクと重ねる・ソロで切り替える）。
//  - **クリップで消した区間（トラックビューのミュートツール）は鳴らさない**（伴奏にも効く）。ミュート／ソロの GainNode とは
//    別に、トラックごとにもう 1 つ GainNode を挟み、消した区間だけ 0 にする（区間の外側 5 ms でフェード。書き出しと同じ）。
//  - 再生位置 S.head とループ S.loop はタイムラインの秒（上下で共通）。ループは区間の終わりの少し前に
//    次の周回を AudioContext の時刻で予約する（どのトラックも同じ時刻に頭へ戻る）。
//  - 再生ヘッドは AudioContext の時刻で動かす（requestAnimationFrame の誤差を持ち込まない）。
//  - **つかんだノートのプレビュー音**（issue #27。Melodyne と同じ）: ノート（端）をつかんでいる間、そのノートを
//    ループで鳴らす。ピッチのドラッグ中は今の高さで作り直して（エンジンの render_audition。1 ノート 10 ms 前後）、
//    鳴っている位置のまま差し替える。作り直しは 1 本ずつ・間を置いて間引く。離したら止める。再生中は鳴らさない。
//    設定（つかんだノートを鳴らす）で切り替え（既定は鳴らす）。
//  - **テストの起動では音を一切出さない**（--mute。出力の音量 0）。プレビューは「鳴らそうとしたもの」を記録する。
//  - **プラグイン（ARA。ara.js）では Web Audio で鳴らさない**。再生・停止は DAW（ホストの再生の制御）へ、再生位置は DAW から来る
//    （S.head・S.playing は ara.js が入れる）。つかんだノートのプレビュー音は C++ の EditorRenderer が DAW の出力で鳴らす。
import { call, callJob, status } from './engine.js';
import { ARA, araPreview, araSelectedModification, araTransport } from './ara.js';
import { S, audible, timelineRange } from './state.js';
import { dbToGain, gainOf, panOf } from './mixer.js';
import { follow, movePlayhead, renderToolbar } from './draw.js';
import { waitFor } from './edits.js';
import { gainRegions, planClipGain } from './clipedit.js';

let ctx = null;
let raf = null;
let P = null;                 // 再生中: { list, gains, pans, sources, a, b, loop, segs: [[ctx 時刻, タイムラインの秒], ...] }
let loading = false;
let sched = null;             // ループの次の周回の予約（requestAnimationFrame はウィンドウが隠れると止まるので使わない）
const MIN_LOOP = 0.05;
const buffers = new Map();    // ファイルのパス → AudioBuffer（編集を当てた音はパスに版が入るので、編集が変われば別のもの）
const LOOKAHEAD = 0.4;        // ループの次の周回をこれだけ前に予約する

let master = null;            // すべての音はここを通す（テストの起動では音量 0。issue #27）

function audioCtx() {
  ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
  return ctx;
}

/** 出力（音を出さない起動では音量 0。Chromium の --mute-audio・setAudioMuted と三重）。 */
export function output() {
  const c = audioCtx();
  if (!master) {
    master = c.createGain();
    master.gain.value = window.api?.muted ? 0 : 1;
    master.connect(c.destination);
  }
  return master;
}

/** 音を出さない起動か（テスト用）。出力の音量まで見る。 */
export function audioMuted() {
  return !!window.api?.muted && (!master || master.gain.value === 0);
}

/** 編集のたびに呼ばれる（音はパスで引き直すので、ここで捨てるものは無い）。 */
export function invalidate() {}

/** 読み込んだ音を捨てる（別のセッションを開いた。1 トラックで数十 MB あるので持ち越さない）。 */
export function dropBuffers() { buffers.clear(); }

/** トラックごとの音（AudioBuffer 付き）。読み込んでいないものだけ読む。 */
async function stems() {
  const r = await callJob('render_tracks', {}, (s) => status(`音を作っている… ${s} 秒`));
  const out = [];
  for (const st of r.tracks || []) {
    if (st.error || !st.path) { status(`鳴らせないトラック: ${st.error || st.id}`); continue; }
    let buf = buffers.get(st.path);
    if (!buf) {
      status('音を読み込んでいる…');
      const bytes = await window.api.readFile(st.path);
      buf = await audioCtx().decodeAudioData(bytes);
      buffers.set(st.path, buf);
    }
    out.push({ ...st, buf });
  }
  const keep = new Set(out.map((s) => s.path));
  for (const k of [...buffers.keys()]) if (!keep.has(k)) buffers.delete(k);
  status('');
  return out;
}

// モノラルの音は StereoPannerNode の等パワーで中央が −3 dB になる（ステレオの音は中央で変わらない）。
// パンを触る前と同じ音量で鳴らすため、モノラルだけ √2 を掛けて中央を 0 dB にそろえる
const monoComp = (st) => (st.buf.numberOfChannels === 1 ? Math.SQRT2 : 1);

/** トラックの、クリップで消した区間（タイムラインの秒）。 */
function regionsOf(st) {
  const t = S.tracks.find((x) => x.id === st.id);
  return gainRegions(t?.mutes, st.start_sec);
}

/** 消した区間が変わっていたら、鳴らしている最中の音にも当て直す（今から先の分だけ予約し直す）。 */
function syncClipGains() {
  if (!P) return;
  const sig = JSON.stringify(P.list.map((st) => regionsOf(st)));
  if (sig === P.muteSig) return;
  P.muteSig = sig;
  const now = ctx.currentTime;
  for (const st of P.list) {
    const cg = P.clips.get(st.id);
    if (!cg) continue;
    const regions = regionsOf(st);
    cg.gain.cancelScheduledValues(now);
    for (const [c0, tl0] of P.segs) {
      const end = c0 + (P.b - tl0);
      if (end <= now) continue;
      const w = Math.max(now, c0);
      planClipGain(cg.gain, regions, w, tl0 + (w - c0), P.b);
    }
  }
}

/** ミュート／ソロ・音量・パン（と消した区間）を今の音に当てる（再生中に動かしたときも呼ぶ）。 */
export function setGains() {
  if (!P) return;
  syncClipGains();
  const now = ctx.currentTime;
  for (const [id, g] of P.gains) {
    const t = S.tracks.find((x) => x.id === id);
    const v = t && audible(t) ? dbToGain(gainOf(t)) : 0;
    const comp = P.comp.get(id) || 1;
    g.gain.cancelScheduledValues(now);
    g.gain.setTargetAtTime(v * comp, now, 0.006);   // 数 ms かけて（プチッと鳴らさない）
    g.target = v;
    const pn = P.pans.get(id);
    if (pn) {
      const pan = t ? panOf(t) : 0;
      pn.pan.cancelScheduledValues(now);
      pn.pan.setTargetAtTime(pan, now, 0.006);
      pn.target = pan;
    }
  }
}

/** いま鳴らしているもの（テスト用）。 */
export function playState() {
  if (!P) return null;
  return {
    loop: P.loop, range: [P.a, P.b],
    tracks: P.list.map((s) => ({ id: s.id, path: s.path, start: s.start_sec, edited: s.edited,
      duration: s.buf.duration, gain: P.gains.get(s.id)?.target ?? null, pan: P.pans.get(s.id)?.target ?? null,
      muted: regionsOf(s), clipGain: P.clips.get(s.id)?.gain.value ?? null })),
  };
}

function schedule(when, from, to) {
  for (const st of P.list) {
    const s0 = st.start_sec;
    const s1 = s0 + st.buf.duration;
    const ov0 = Math.max(from, s0);
    const ov1 = Math.min(to, s1);
    if (ov1 - ov0 <= 1e-4) continue;
    const src = ctx.createBufferSource();
    src.buffer = st.buf;
    const cg = P.clips.get(st.id);
    src.connect(cg);
    planClipGain(cg.gain, regionsOf(st), when, from, to);
    src.start(when + (ov0 - from), ov0 - s0, ov1 - ov0);
    src.onended = () => { if (P) P.sources = P.sources.filter((x) => x !== src); };
    P.sources.push(src);
  }
}

export async function play() {
  if (ARA) { await araTransport(S.playing ? 'stop' : 'play'); return; }
  if (S.playing) { stop(); return; }
  stopPreview();
  if (!S.vd || loading) return;
  loading = true;
  let list;
  try {
    // 編集の確定・トラックの切り替えの途中は待つ（開き直し中のプロジェクトから音を作らない）
    await waitFor(() => !S.busy && !S.opening);
    list = await stems();
  } catch (err) {
    status(`再生できなかった: ${err.message}`);
    return;
  } finally {
    loading = false;
  }
  const c = audioCtx();
  if (c.state === 'suspended') await c.resume();
  const loop = S.loop && S.loop[1] - S.loop[0] >= MIN_LOOP ? S.loop : null;
  const [a, b] = loop || timelineRange();
  const from = S.head >= a && S.head < b ? S.head : a;
  P = { list, gains: new Map(), pans: new Map(), clips: new Map(), comp: new Map(), sources: [], a, b, loop: !!loop, segs: [], muteSig: null };
  for (const st of list) {
    const g = c.createGain();
    const pn = c.createStereoPanner();
    g.connect(pn);
    pn.connect(output());
    P.gains.set(st.id, g);
    P.pans.set(st.id, pn);
    P.comp.set(st.id, monoComp(st));
    const cg = c.createGain();             // クリップで消した区間を 0 にする（音源の後ろ・ミュート／ソロ／音量の前）
    cg.connect(g);
    P.clips.set(st.id, cg);
  }
  P.muteSig = JSON.stringify(list.map((st) => regionsOf(st)));
  setGains();
  const t0 = c.currentTime + 0.03;
  P.segs.push([t0, from]);
  schedule(t0, from, b);
  S.playing = true;
  S.head = from;
  renderToolbar();
  if (P.loop) sched = setInterval(scheduleLoop, 50);
  tick();
}

/** ループ: 区間の終わりの少し前に次の周回を予約する。止まっていた（隠れていた）ら、今より後の境界まで飛ばす。 */
function scheduleLoop() {
  if (!P || !P.loop) return;
  const now = ctx.currentTime;
  const L = P.b - P.a;
  const last = P.segs[P.segs.length - 1];
  let end = last[0] + (P.b - last[1]);
  if (end < now) end += Math.ceil((now - end) / L) * L;
  if (now > end - LOOKAHEAD && !(last[1] === P.a && Math.abs(last[0] - end) < 1e-6)) {
    P.segs.push([end, P.a]);
    schedule(end, P.a, P.b);
  }
  while (P.segs.length > 2 && P.segs[1][0] <= now) P.segs.shift();
}

function tick() {
  if (!S.playing || !P) return;
  const now = ctx.currentTime;
  if (P.loop) scheduleLoop();
  let seg = P.segs[0];
  for (const sg of P.segs) if (sg[0] <= now) seg = sg;
  S.head = Math.min(P.b, seg[1] + Math.max(0, now - seg[0]));
  if (!P.loop && S.head >= P.b - 1e-3) { stop(); return; }
  follow();
  movePlayhead();
  raf = requestAnimationFrame(tick);
}

export function stop() {
  if (ARA) return;          // 再生は DAW のもの（再生位置を動かしても DAW の再生は止めない。止めるのは再生ボタン・Space）
  S.playing = false;
  if (raf) cancelAnimationFrame(raf);
  raf = null;
  if (sched) clearInterval(sched);
  sched = null;
  if (P) {
    for (const s of P.sources) { try { s.stop(); } catch { /* 既に止まっている */ } }
    for (const g of [...P.gains.values(), ...P.clips.values()]) { try { g.disconnect(); } catch { /* noop */ } }
    for (const pn of P.pans.values()) { try { pn.disconnect(); } catch { /* noop */ } }
  }
  P = null;
  renderToolbar();
}

// ---------------------------------------------------------------- つかんだノートのプレビュー音（issue #27）
const PREVIEW_GAP_MS = 45;    // 作り直しの間隔の下限（間引き）
const PREVIEW_FADE = 0.006;   // ループのつなぎ目・差し替えのフェード（秒）
let previewOn = true;
const PV = { token: 0, note: null, range: null, want: 0, busy: 0, timer: 0, lastAt: 0,
  src: null, gain: null, t0: 0, dur: 0, cents: null, host: false, phase: 'idle', error: null,
  prepared: null, context: null };
let primed = null;
let inflightPrepare = null;
const previewLog = [];        // 鳴らそうとしたもの（テスト用。音は出さずにこれで確かめる）

function setPreviewPhase(phase, error = null) {
  const changed = PV.phase !== phase;
  PV.phase = phase;
  PV.error = error;
  if (changed && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('gliss-preview-state', { detail: previewState() }));
  }
}

export function previewEnabled() { return previewOn; }
/** つかんだノートを鳴らす（設定）。save: ユーザー設定に残す（起動時に読むときは残さない）。 */
export function setPreviewEnabled(on, { save = true } = {}) {
  previewOn = !!on;
  if (!previewOn) stopPreview();
  if (save) {
    window.api?.saveState?.({ preview: previewOn });
    status(`つかんだノートを鳴らす: ${previewOn ? 'オン' : 'オフ'}`);
  }
  renderToolbar();            // メニューバーのチェックも付け直す
}

/** いまの高さ（確定した音からのずらし。セント）。ドラッグ中と、まだ当たっていない前のドラッグの分を含む。 */
function wantCents(id) {
  return Math.round((S.local.pitch.get(id) || 0) * 1000) / 10;
}

function previewRequest(noteId) {
  const n = S.byId.get(noteId);
  if (!n || n.kind !== 'note') return null;
  const a = n.edited_start_sec ?? n.start_sec;
  const b = n.edited_end_sec ?? n.end_sec;
  if (!(b - a > 0.01)) return null;
  return { note_id: noteId, cents: wantCents(noteId), start_sec: +a.toFixed(6), end_sec: +b.toFixed(6) };
}

function previewContext() {
  if (!ARA) return null;
  const track = S.tracks.find((t) => t.id === S.session?.current);
  const selected = araSelectedModification();
  if (!track?.ara_id || !S.projectDir || !S.vd?.view_rev
      || (selected && (selected.track_id !== track.id || selected.ara_id !== track.ara_id))) return null;
  return { view: S.vd, rev: S.vd.view_rev, track: track.id, araId: track.ara_id,
    project: S.projectDir, selected };
}

function previewContextCurrent(ctx, args) {
  if (!ctx || !args || S.playing || S.busy || S.queued || S.pendingPlan) return false;
  const now = previewContext();
  const current = previewRequest(args.note_id);
  return !!now && now.view === ctx.view && now.rev === ctx.rev && now.track === ctx.track
    && now.araId === ctx.araId && now.project === ctx.project
    && now.selected?.track_id === ctx.selected?.track_id
    && now.selected?.ara_id === ctx.selected?.ara_id
    && current?.cents === args.cents && current?.start_sec === args.start_sec
    && current?.end_sec === args.end_sec;
}

/** ARA の押下中だけ補正 PCM を先に作る。長押し判定までは host に渡さない。 */
export function preparePreview(noteId) {
  primed = null;
  if (!ARA || S.playing || !S.vd || S.busy || S.queued || S.pendingPlan) return;
  const args = previewRequest(noteId);
  const context = previewContext();
  if (!args || !context) return;
  if (inflightPrepare) {
    if (inflightPrepare.args.note_id === args.note_id
        && inflightPrepare.args.cents === args.cents
        && inflightPrepare.args.start_sec === args.start_sec
        && inflightPrepare.args.end_sec === args.end_sec
        && previewContextCurrent(inflightPrepare.context, args)) primed = inflightPrepare;
    return; // A short-click burst must not fill the engine's serial request queue.
  }
  const p = { args, context };
  p.result = call('render_audition', args).then(
    (value) => ({ value }), (error) => ({ error }),
  ).finally(() => { if (inflightPrepare === p) inflightPrepare = null; });
  inflightPrepare = p;
  primed = p;
}

/** ノートをつかんだ（ピッチ・移動・端のドラッグの始め）。 */
export function startPreview(noteId) {
  const args = previewRequest(noteId);
  const prepared = ARA && primed && args && primed.args.note_id === args.note_id
    && primed.args.cents === args.cents && primed.args.start_sec === args.start_sec
    && primed.args.end_sec === args.end_sec && previewContextCurrent(primed.context, args)
    ? primed : null;
  stopPreview();
  if (!previewOn || S.playing || !S.vd || !args) return;
  const context = ARA ? (prepared?.context || previewContext()) : null;
  if (ARA && !context) {
    setPreviewPhase('error', '画面を更新してから試聴してください');
    return;
  }
  PV.note = noteId;
  PV.range = [args.start_sec, args.end_sec];
  PV.want = args.cents;
  PV.prepared = prepared?.result || null;
  PV.context = context;
  setPreviewPhase('preparing');
  requestPreview();
}

/** 高さが変わったかもしれない（ピッチのドラッグ中の pointermove）。変わっていれば作り直しを頼む（間引く）。 */
export function updatePreview() {
  if (!PV.note) return;
  const c = wantCents(PV.note);
  if (Math.abs(c - PV.want) < 0.5) return;
  PV.want = c;
  schedulePreview();
}

function schedulePreview() {
  if (PV.busy || PV.timer || !PV.note) return;
  const wait = Math.max(0, PV.lastAt + PREVIEW_GAP_MS - performance.now());
  PV.timer = setTimeout(() => { PV.timer = 0; requestPreview(); }, wait);
}

function araPreviewError(reason) {
  switch (reason) {
    case 'no-editor-renderer': return 'この DAW では試聴出力を使えません';
    case 'host-playing': return 'DAW の再生中は試聴できません';
    case 'invalid-path':
    case 'invalid-audio': return '試聴用の音を読み込めません';
    default: return 'DAW で試聴を開始できません';
  }
}

async function requestPreview() {
  if (!PV.note || PV.busy) return;
  if (S.playing) { stopPreview(); return; }
  const tok = PV.token;
  const note = PV.note;
  const cents = PV.want;
  const [a, b] = PV.range;
  const args = { note_id: note, cents, start_sec: a, end_sec: b };
  const context = ARA ? PV.context : null;
  PV.busy = tok || -1;
  PV.lastAt = performance.now();
  previewLog.push({ note, cents, range: [a, b], at: Date.now() });
  setPreviewPhase('preparing');
  try {
    const prepared = PV.prepared;
    PV.prepared = null;
    const response = prepared ? await prepared : { value: await call('render_audition', args) };
    if (response.error) throw response.error;
    const r = response.value;
    if (tok !== PV.token) return;
    if (ARA && (!previewContextCurrent(context, args) || !r.view_rev || !r.rev
        || r.view_rev !== context.rev || r.track_id !== context.track
        || r.ara_id !== context.araId || r.note_id !== note || r.cents !== cents)) {
      stopPreview();
      return;
    }
    if (ARA) {                      // native は準備した PCM を EditorRenderer へ渡してから ok を返す
      const result = await araPreview('start', { path: r.path, loop: true, note, cents });
      if (tok !== PV.token) return;
      if (!previewContextCurrent(context, args)) { stopPreview(); return; }
      if (!result?.ok) {
        if (result?.reason === 'cancelled') { stopPreview(); return; }
        throw new Error(araPreviewError(result?.reason));
      }
      PV.host = true;
      PV.cents = cents;
      setPreviewPhase('sounding');
      return;
    }
    const bytes = await window.api.readFile(r.path);
    if (tok !== PV.token) return;
    const buf = await audioCtx().decodeAudioData(bytes);
    if (tok !== PV.token || S.playing) return;
    swapPreview(buf, cents);
    setPreviewPhase('sounding');
  } catch (err) {
    if (tok === PV.token) {
      setPreviewPhase('error', err.message);
      status(`プレビューの音を作れなかった: ${err.message}`);
    }
  } finally {
    if (PV.busy === (tok || -1)) PV.busy = 0;
    // 作っている間に高さが変わった: いまの高さでもう一度（最後の高さだけ）
    if (tok === PV.token && PV.note && Math.abs(PV.want - cents) >= 0.5) schedulePreview();
  }
}

/** 頭と尻を短くフェード（ループのつなぎ目でプチッと鳴らさない）。 */
function fadeEdges(buf) {
  const n = Math.min(Math.floor(buf.sampleRate * PREVIEW_FADE), Math.floor(buf.length / 2));
  for (let ch = 0; ch < buf.numberOfChannels; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < n; i++) {
      const g = i / n;
      d[i] *= g;
      d[d.length - 1 - i] *= g;
    }
  }
}

/** 鳴っている位置のまま、新しい高さの音に差し替える（前の音は数 ms でフェードアウト）。 */
function swapPreview(buf, cents) {
  const c = audioCtx();
  if (c.state === 'suspended') c.resume();
  fadeEdges(buf);
  const now = c.currentTime;
  let offset = 0;
  if (PV.src && PV.dur > 0) offset = (((now - PV.t0) % PV.dur) + PV.dur) % PV.dur;
  if (offset >= buf.duration) offset = 0;
  releaseVoice(PV.src, PV.gain);
  const g = c.createGain();
  g.gain.setValueAtTime(0, now);
  g.gain.linearRampToValueAtTime(1, now + PREVIEW_FADE);
  g.connect(output());
  const src = c.createBufferSource();
  src.buffer = buf;
  src.loop = true;
  src.connect(g);
  src.start(now, offset);
  PV.src = src; PV.gain = g; PV.t0 = now - offset; PV.dur = buf.duration; PV.cents = cents;
}

function releaseVoice(src, g) {
  if (!src || !ctx) return;
  const now = ctx.currentTime;
  try {
    g.gain.cancelScheduledValues(now);
    g.gain.setValueAtTime(g.gain.value, now);
    g.gain.linearRampToValueAtTime(0, now + PREVIEW_FADE);
    src.stop(now + PREVIEW_FADE + 0.01);
  } catch { /* 既に止まっている */ }
  src.onended = () => { try { g.disconnect(); } catch { /* noop */ } };
}

/** 離した（か再生を始めた・設定を切った）: 止める。作りかけの音は捨てる。 */
export function stopPreview() {
  const hadPreview = !!(PV.note || PV.busy || PV.host);
  primed = null;
  PV.prepared = null;
  PV.token += 1;
  clearTimeout(PV.timer);
  PV.timer = 0;
  PV.busy = 0;
  PV.note = null;
  PV.range = null;
  PV.context = null;
  PV.host = false;
  if (ARA && hadPreview) araPreview('stop');
  releaseVoice(PV.src, PV.gain);
  PV.src = null; PV.gain = null; PV.dur = 0; PV.cents = null;
  setPreviewPhase('idle');
}

/** テスト用: いま鳴らしているもの（鳴っていなければ null）と、鳴らそうとしたものの記録。 */
export function previewState() {
  return {
    enabled: previewOn, note: PV.note, range: PV.range ? [...PV.range] : null, want: PV.note ? PV.want : null,
    phase: PV.phase, error: PV.error,
    sounding: PV.src || PV.host ? { cents: PV.cents, duration: PV.dur || null } : null,
  };
}
if (typeof window !== 'undefined') {
  window.addEventListener('blur', stopPreview);
  window.addEventListener('gliss-host-play', stopPreview);
  window.addEventListener('gliss-ara-selection', (event) => {
    const context = PV.context || primed?.context;
    if (context && (event.detail?.track_id !== context.track || event.detail?.ara_id !== context.araId))
      stopPreview();
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopPreview(); });
}
export function previewLogOf() { return previewLog.map((x) => ({ ...x, range: [...x.range] })); }
export function clearPreviewLog() { previewLog.length = 0; }
