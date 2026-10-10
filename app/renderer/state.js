// 画面の状態と、`export_view_data` の JSON から作る描画モデル。
//
// 表示の時間軸は **編集後の時間**（耳で聞こえる並び）。編集前の位置は
// 選択したノートの「元の長さ」（帯の上の細いグレーの線。issue #37）にだけ使う。
// ドラッグ中は `local` にだけ差分を持ち、離した時点でエンジンのツールを呼ぶ。
//
// **タイミング（ノート端・ノートの移動・ガイドに合わせる）はエンジンの計画（plan）で描く。**
// 計画 = 節ごとの「編集後の秒 = cur + d × x」。画面は x を動かして同じ式で描き、
// 離したら同じ計画を同じ x で確定する（`engine/vocal_engine/project/timing.py`）。
// プレビューの規則を画面側に写さないので、ドラッグ中と離した後がずれない。

export const LAYOUT = {
  KEYS_W: 44, SCALE_H: 20, LANE_H: 46, EDGE: 8, MIN_SEG: 0.02,
  // 音素の最短の長さ（エンジンの move_boundary と同じ 20 ms）
  MIN_PH: 0.02,
};
// ガイドは濃いグレー（線・帯・トラックビューの波形とも同じ色。テイクの黄とは明るさで見分ける。issue #37）。
// 補正の度合いの色（黄 → 赤・手動の白）は corr.js
export const COLORS = {
  TAKE: '#e6d24a', GUIDE: '#4e4e54', SEL: '#f2f2f2',
  // 選択したノートの元の長さ（帯の上の細い線と両端の縦線。ガイドより明るいグレー）
  WAS: '#a4a4aa',
  // AI（Claude Code など）の編集が最後に当たっているノートの縁（index.html の --ai と同じ）
  AI: '#8fa8ff',
  // 伴奏（トラックビューの波形。背景に近い薄いグレー）・編集中でないボーカル（暗い黄）
  INST: '#707076', VOCAL: '#7d7437',
  // 子音（歌詞があるときだけ）: テイクの黄と同じ色相・明るさのまま、彩度だけ落とす（v3 §6）
  CONS: '#bdb57a',
};

export const S = {
  vd: null,            // export_view_data の JSON
  notes: [],           // 全ノート（無音・息も含む）
  pitched: [],         // kind === 'note' のものだけ
  byId: new Map(),
  aiNotes: new Set(),  // 最後に当たっている編集が AI のノート（枠を AI の色に）
  midiLo: 55, midiHi: 76,  // データの音程の範囲（読み込み時に決める。縦の表示・スクロールの基準）
  // 縦の表示範囲（v3 §9）: top = いちばん上の端の音程（MIDI）、span = 見えている半音の数（6〜36）。
  // null = 未設定（読み込み時にデータの範囲にする）
  pv: null,
  view: { t0: 0, span: 0 },   // span 0 = 未設定（読み込み時に全体にする）
  showGuide: true,
  showAllBounds: false, // 音素境界の全高表示。吸着・編集には影響しない
  sel: [],             // 選択中のノート id
  loop: null,          // [t0, t1]（ソング秒。ARA ではホスト通知だけで更新）
  araLoopDraft: null,  // { trackId, range }。下段スケールの出力秒で、S.loop とは別に描く
  head: 0,
  playing: false,
  drag: null,
  // ドラッグ中の差分。pitch はピッチのドラッグ、btime は音素境界のドラッグ
  // （`move_boundary`。前後 2 音素の線形の伸縮なので境界の上書きで描ける）。
  // fade はフェードのつまみのドラッグ（id → { fi, fo }。編集後の秒。issue #20）
  // mute はミュートツールのなぞった分（id → 無音にする = true / 戻す = false。当たるまでの見かけ）
  local: { pitch: new Map(), btime: new Map(), fade: new Map(), mute: new Map() },
  // エンジンの計画（plan_edit）。{ data, x, x0, pitch, pitch0, guides }
  //   x / pitch    いまの値（タイミング / ピッチの強度。端・移動では x = 秒）
  //   x0 / pitch0  表示中の view data がすでに当たっている値（ポップアップ内の当て直し）
  plan: null,
  pendingPlan: false,  // 端・移動のドラッグを離して、計画の確定を待っている間
  ph: null,            // export_view_data の phonemes ブロック（歌詞が無ければ null）
  phById: new Map(),
  bounds: [],          // 音素境界（ph.boundaries と同じ並び）
  boundById: new Map(),
  lyrics: null,
  lyricsEntries: [],   // 区間ごとの歌詞 [{start_sec, end_sec, text}]（編集前の秒）
  // 聞き取り（音声認識。issue #54。asr.js）: 候補（transcribe の結果＋trackId。確定するまで歌詞レーンに薄く出す）と、
  // 聞き取っている区間 { start_sec, end_sec, trackId }（編集前の秒）
  asrCand: null,
  asrRunning: null,
  busy: false,
  queued: 0,           // 離したが、前の編集を待っていてまだ当てていない操作の数（edits.enqueue）
  // 順番待ちでまだエンジンに渡していない操作（取り消しの名前つき。Ctrl+Z でここから外せる。edits.enqueue）
  pending: [],
  openQueued: 0,       // 順番待ちに入っているトラックの追加・切り替え（その間のドロップは断る）
  // 準備の待ちを「待つのをやめる」でやめたトラック { id, view, pv, watch }（issue #63）。その間は下を空にし、
  // 編集は「準備中」で断る。準備が終わったら描き直す（tracks.js）。解析が済めば（engine.analyzeTake）外す
  awaitPrep: null,
  // エンジンの曲の取り消しの履歴の要約（can_undo / can_redo / undo・redo のラベル。issue #16）
  hist: null,
  projectDir: null,
  take: null,
  guide: null,
  // 開いているプロジェクトのファイル（issue #33）: { kind: 'gliss' | 'untitled' | 'legacy', path, name, dirty, work_dir }
  doc: null,
  // ツール（ヘッダーのアイコン / 1・2・3・4）: 'main' = 矢印、'draw' = 鉛筆、'cut' = はさみ、'mute' = ミュート
  tool: 'main',
  // 「つなぎのなだらかさ」のスライダー中: { keys: Set('a|b'), value }（エンジンは離したときだけ呼ぶ）
  trPreview: null,
  // 鉛筆で描いている線: { vals: Map(フレーム → MIDI), last: {i, m} }（離したら set_pitch_curve(mode=draw)）
  stroke: null,
  strokePhase: 'idle', // drawing / pending / checking / failed / committed
  boundHover: null,
  edgeDraft: null, // 計画待ちの間に見せる仮の端（エンジンには未適用）
  lastEdgeTiming: null, // 直近の端編集の plan / apply / view の実測値
  cutHover: null,      // はさみ: { id, t（編集後の秒）, src（編集前の秒） }
  // 接続の見せ方（B 案）: ポインタが近づいた境目（'a|b'）と、Alt を押しているか
  near: null,
  alt: false,
  // ポインタが乗っているノートの端（矢印のとき）: { id, which }。端に明るい縦線を出す（v3 §3）
  edgeHover: null,
  // ポインタが乗っているノート（矢印のとき）。帯の上の角にフェードのつまみを出す（v3 §5）
  noteHover: null,
  fadeHover: null,     // ポインタが乗っているフェードのつまみ（'id|in' / 'id|out'）
  // ヘッダーのテンポをドラッグ・ホイールで変えている間の見かけの値（確定するまで。issue #18。grid.js の tempo()）
  tempoPreview: null,
  // トラック（issue #7）。engine の list_tracks と同じ形（`docs/track-view.md` §1）
  session: null,       // { dir, guide, current, timeline_sec, tracks }
  tracks: [],
  // 編集対象のトラックのタイムライン上の位置（秒）。ピアノロールの中の秒（編集後の時間）はトラックの頭が 0 で、
  // **再生位置 S.head とループ S.loop はタイムラインの秒**（上下で共通）。ピアノロールに描くときは S.off を引く
  off: 0,
  trackOff: new Map(), // 位置をドラッグしている間の見かけの位置（id → 秒。離して確定するまで）
};

/** トラックが聞こえるか（ミュート／ソロ。DAW と同じ: どれかがソロなら、ソロのものだけ）。 */
export function audible(t) {
  const solo = S.tracks.some((x) => x.solo);
  return !t.mute && (!solo || t.solo);
}

/** トラックの位置（ドラッグ中はその見かけの位置）。 */
export function offsetOf(t) {
  return S.trackOff.has(t.id) ? S.trackOff.get(t.id) : (t.offset_sec || 0);
}

/** タイムラインの範囲 [頭, 終わり]（秒）: 0 か一番前のトラックの頭 〜 一番後ろのトラックの終わり。 */
export function timelineRange() {
  if (!S.tracks.length) return [0, Math.max(0.5, S.off + totalSec())];
  let a = 0; let b = 0;
  for (const t of S.tracks) {
    const o = offsetOf(t);
    a = Math.min(a, o);
    b = Math.max(b, o + (t.duration_sec || 0));
  }
  return [a, Math.max(b, a + 0.5)];
}

export function currentTrack() {
  return S.tracks.find((t) => t.id === S.session?.current) || null;
}

/** 画面が今描いているデータ（S.vd・S.byId = S.projectDir）のトラック。切り替えの途中（S.session.current は新しいトラック、
 * S.vd はまだ前のトラック）では current と違う。無ければ null。 */
export function shownTrack() {
  const norm = (p) => String(p || '').replace(/\//g, '\\').toLowerCase();
  return S.projectDir
    ? S.tracks.find((t) => t.project_dir && norm(t.project_dir) === norm(S.projectDir)) || null : null;
}

/** 下に出しているデータ（S.vd = S.projectDir のトラック）の位置を S.off に。切り替えの途中でも、
 * 下の目盛り・再生位置は下に出ているトラックの位置で描く（ドラッグ中の見かけの位置を含む）。 */
export function syncOff() {
  const t = shownTrack() || currentTrack();
  S.off = t ? offsetOf(t) : 0;
}

// ---------------------------------------------------------------- つなぎ・鉛筆（エンジンと同じ式）
// `engine/vocal_engine/project/pitch.py` の写し。画面はノートのドラッグ中・なだらかさのスライダー中・
// 鉛筆で描いている間、エンジンを呼ばずにこの式で黄色の曲線を描き、離したら同じ値でエンジンを呼ぶ。
export const PITCH = { SLOW_MIN_SEC: 0.40, SLOW_FACTOR: 3.0, DRAW_RAMP_SEC: 0.04 };

/** なだらかさ v（0〜1）→ 窓の幅（秒）。0 = 段差、0.5 = 自動、1 = ゆっくり。 */
export function widthFor(v, autoSec) {
  const x = clamp(v, 0, 1);
  if (x <= 0.5) return autoSec * x / 0.5;
  const slow = Math.max(PITCH.SLOW_MIN_SEC, autoSec * PITCH.SLOW_FACTOR);
  return autoSec + (slow - autoSec) * (x - 0.5) / 0.5;
}
export function trHalves(tr, v) {
  const w = widthFor(v, tr.auto_sec);
  return [Math.min(0.5 * w, tr.max_hl), Math.min(0.5 * w, tr.max_hr)];
}
function sOf(t, lo, hi) {
  if (t <= lo) return 0;
  if (t >= hi) return 1;
  return 0.5 - 0.5 * Math.cos(Math.PI * (t - lo) / (hi - lo));
}
function rampW(t, t0, t1, R) {
  if (t >= t0 && t <= t1) return 1;
  if (R <= 0 || t <= t0 - R || t >= t1 + R) return 0;
  const u = t < t0 ? (t - (t0 - R)) / R : ((t1 + R) - t) / R;
  return 0.5 - 0.5 * Math.cos(Math.PI * u);
}

/** フレーム → 音程のノート id（編集前の秒で判定）。 */
export function frameNoteIds() {
  const vd = S.vd;
  if (!vd) return [];
  if (vd._frameNote) return vd._frameNote;
  const hop = vd.f0.hop_sec;
  const t0 = vd.f0.t0_sec;
  const n = vd.f0.take_midi.length;
  const out = new Array(n).fill(null);
  let k = 0;
  const ns = S.notes;
  for (let i = 0; i < n && ns.length; i++) {
    const t = t0 + i * hop;
    while (k < ns.length - 1 && t >= ns[k].end_sec) k++;
    if (t >= ns[k].start_sec && t < ns[k].end_sec) out[i] = ns[k].id;
  }
  vd._frameNote = out;
  return out;
}
/** ノート id → [最初のフレーム, 最後のフレーム]（編集前の秒で判定。blob を描く範囲）。 */
export function noteFrames() {
  const vd = S.vd;
  if (!vd) return new Map();
  if (vd._noteFrames) return vd._noteFrames;
  const fn = frameNoteIds();
  const out = new Map();
  for (let i = 0; i < fn.length; i++) {
    const id = fn[i];
    if (!id) continue;
    const r = out.get(id);
    if (r) r[1] = i; else out.set(id, [i, i]);
  }
  vd._noteFrames = out;
  return out;
}

/** ガイドノート id → [最初のフレーム, 最後のフレーム]（`f0.guide_sec` はテイクの時間に置いた秒。`guide_basis`）。 */
export function guideFrames() {
  const vd = S.vd;
  if (!vd) return new Map();
  if (vd._guideFrames) return vd._guideFrames;
  const gs = vd.f0.guide_sec || [];
  const out = new Map();
  // ガイドの置き方（ガイドの時刻 + 全体のずれ / DTW の写像）はどちらも単調（減らない）なので、区間の頭を二分探索で探す
  const lower = (t) => {
    let lo = 0; let hi = gs.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (gs[m] < t) lo = m + 1; else hi = m; }
    return lo;
  };
  for (const g of vd.guide_notes || []) {
    const a = lower(g.start_sec);
    const b = lower(g.end_sec) - 1;
    if (b >= a) out.set(g.id, [a, b]);
  }
  vd._guideFrames = out;
  return out;
}

export function frameIndex(tSrc) {
  const f = S.vd.f0;
  return Math.round((tSrc - f.t0_sec) / f.hop_sec);
}
export function frameTime(i) {
  const f = S.vd.f0;
  return f.t0_sec + i * f.hop_sec;
}

/** 鉛筆の線 → [[編集前の秒, MIDI], ...]（描いた順に後から描いたフレームが勝つ）と、有声に切り詰めた範囲。 */
export function strokeData() {
  const st = S.stroke;
  if (!st || st.vals.size === 0) return null;
  const idx = [...st.vals.keys()].sort((a, b) => a - b);
  const lo = idx[0]; const hi = idx[idx.length - 1];
  const tm = S.vd.f0.take_midi;
  let v0 = -1; let v1 = -1;
  for (let i = Math.max(0, lo); i <= Math.min(tm.length - 1, hi); i++) {
    if (tm[i] == null) continue;
    if (v0 < 0) v0 = i;
    v1 = i;
  }
  const pts = idx.map((i) => [frameTime(i), st.vals.get(i)]);
  return { lo, hi, v0, v1, pts };
}

/** いまの計画（端のドラッグ）で確定したら変わる接続。{ off: Set('a|b'), add: つなぎ | null }。
 *
 *   エンジンの `apply_plan` と同じ規則: x ≠ 0 のときだけ、Alt の切り離し（set_connections）が効き、
 *   吸着の位置（snap_x）ちょうどで離すと、切り離されていた組が接続になる（snap_transition）。 */
export function planConnChanges() {
  const pl = S.plan;
  const d = pl?.data;
  if (!d || d.kind !== 'edge' || Math.abs(pl.x || 0) < 1e-9) return null;
  const off = new Set();
  for (const [a, b, c] of d.set_connections || []) if (!c) off.add(`${a}|${b}`);
  let add = null;
  const st = d.snap_transition;
  if (st && d.snap_x != null && Math.abs(pl.x - d.snap_x) < 1e-6) {
    const key = `${st.a}|${st.b}`;
    if (off.has(key)) off.delete(key);
    else if (!(S.vd.transitions || []).some((t) => t.a === st.a && t.b === st.b)) add = st;
  }
  return off.size || add ? { off, add } : null;
}

/** 画面に描く黄色の曲線（編集後の MIDI、フレームごと）。
 *
 *   view data の値（エンジンが当てた結果）に、いまのドラッグ・スライダー・鉛筆の分だけ足す:
 *   - ノートのピッチ（ドラッグ／ガイドに合わせるのプレビュー）: そのノートのフレームに d
 *   - つなぎ: 接続された境目の窓の中に keep·[(S' − H)·Δ' − (S − H)·Δ]（Δ' = Δ + dΔ、S' = スライダーの値の窓）
 *   - 鉛筆: 描いている線を一番上に（範囲の中は描いた値、両端 40 ms でつなぐ） */
export function editedCurve() {
  const vd = S.vd;
  if (!vd) return [];
  const base = vd.f0.take_edited_midi;
  const shape = S.plan?.data?.params?.match_pitch_shape ? S.plan?.shapeFrames : null;
  const shapeStrength = shape ? (S.plan.pitch || 0) - (S.plan.pitch0 || 0) : 0;
  const shapeMidi = ([h0, h1, w], strength) => 69 + 12 * Math.log2((h0 + strength * w * (h1 - h0)) / 440);
  const hasPitch = S.local.pitch.size > 0 || !!(S.plan && S.plan.data && S.plan.data.pitch
    && Object.keys(S.plan.data.pitch).length && (S.plan.pitch || 0) !== (S.plan.pitch0 || 0));
  const cc = planConnChanges();
  if (!hasPitch && !S.trPreview && !S.stroke && !cc) return base;
  const out = base.slice();
  const fn = frameNoteIds();
  const keep = vd.f0.draw_keep || null;
  const n = out.length;
  if (hasPitch) {
    for (let i = 0; i < n; i++) {
      if (out[i] == null) continue;
      const id = fn[i];
      if (id) out[i] += shape ? (S.local.pitch.get(id) || 0) : pitchDelta(id);
      if (shapeStrength && shape?.has(i)) {
        const frame = shape.get(i);
        out[i] += shapeMidi(frame, S.plan.pitch || 0) - shapeMidi(frame, S.plan.pitch0 || 0);
      }
    }
  }
  // 計画で接続が変わる境目（Alt の切り離し = つなぎが消える、吸着 = つなぎが生まれる）も
  // ドラッグ中に反映する（離した後になだらかさが現れる／消えることが無いように）
  const trs = (vd.transitions || []).map((tr) => [tr, true, !(cc && cc.off.has(`${tr.a}|${tr.b}`))]);
  if (cc && cc.add) trs.push([cc.add, false, true]);
  for (const [tr, was, now] of trs) {
    const dd = hasPitch ? ((shape ? (S.local.pitch.get(tr.b) || 0) : pitchDelta(tr.b))
      - (shape ? (S.local.pitch.get(tr.a) || 0) : pitchDelta(tr.a))) * 100 : 0;
    const prev = S.trPreview && S.trPreview.keys.has(`${tr.a}|${tr.b}`) ? S.trPreview.value : null;
    if (was && now && Math.abs(dd) < 1e-9 && prev == null) continue;
    const [hl2, hr2] = prev == null ? [tr.hl, tr.hr] : trHalves(tr, prev);
    const lo1 = tr.ta - tr.hl; const hi1 = tr.tb + tr.hr;
    const lo2 = tr.ta - hl2; const hi2 = tr.tb + hr2;
    const d1 = was ? tr.delta : 0; const d2 = now ? tr.delta + dd : 0;
    const a = Math.max(0, frameIndex(Math.min(lo1, lo2)) - 1);
    const b = Math.min(n - 1, frameIndex(Math.max(hi1, hi2)) + 1);
    for (let i = a; i <= b; i++) {
      if (out[i] == null) continue;
      const t = frameTime(i);
      // 隙間（無声）の中は触らない。t == ta もエンジンでは隙間の側（右から見た値）
      if (tr.ta < tr.tb && t >= tr.ta && t < tr.tb) continue;
      const H = t >= tr.tb ? 1 : 0;
      const s1 = (tr.hl + tr.hr > 1e-6 && t >= lo1 && t <= hi1) ? sOf(t, lo1, hi1) - H : 0;
      const s2 = (hl2 + hr2 > 1e-6 && t >= lo2 && t <= hi2) ? sOf(t, lo2, hi2) - H : 0;
      const k = keep ? (keep[i] ?? 1) : 1;
      out[i] += k * (s2 * d2 - s1 * d1) / 100;
    }
  }
  const sd = S.stroke ? strokeData() : null;
  if (sd && sd.v0 >= 0) {
    const R = PITCH.DRAW_RAMP_SEC;
    const t0 = frameTime(sd.v0); const t1 = frameTime(sd.v1);
    const orig = vd.f0.take_midi;
    const val = (i) => { const v = S.stroke.vals.get(i); return v == null ? interpStroke(sd, i) : v; };
    // 描き直し: 新しい線の範囲にすっぽり入る前の鉛筆は、エンジンが外す（draw_specs）。プレビューでも
    // 外した曲線（鉛筆を当てる前の曲線 take_nodraw_midi）から描く（issue #6。以前は端 40 ms で最大 25 セントずれた）
    const nd = vd.f0.take_nodraw_midi;
    if (nd && vd.draws?.length) {
      const cov = vd.draws.filter((d) => d.t0 >= t0 - 1e-4 && d.t1 <= t1 + 1e-4);
      const rest = vd.draws.filter((d) => !cov.includes(d));
      for (const d of cov) {
        const a0 = Math.max(0, frameIndex(d.lo) - 1);
        const b0 = Math.min(n - 1, frameIndex(d.hi) + 1);
        for (let i = a0; i <= b0; i++) {
          if (out[i] == null || nd[i] == null) continue;
          const t = frameTime(i);
          if (t < d.lo || t > d.hi) continue;
          if (rest.some((r) => t > r.lo && t < r.hi)) continue;   // 残る線がかかるところは今のまま
          out[i] = nd[i] + (out[i] - base[i]);
        }
      }
    }
    const offA = val(sd.v0) - orig[sd.v0];
    const offB = val(sd.v1) - orig[sd.v1];
    const a = Math.max(0, frameIndex(t0 - R) - 1);
    const b = Math.min(n - 1, frameIndex(t1 + R) + 1);
    for (let i = a; i <= b; i++) {
      if (out[i] == null || orig[i] == null) continue;
      const t = frameTime(i);
      const w = rampW(t, t0, t1, R);
      if (w <= 0) continue;
      if (i >= sd.v0 && i <= sd.v1) { out[i] = val(i); continue; }
      const tgt = i < sd.v0 ? offA : offB;
      out[i] = orig[i] + (1 - w) * (out[i] - orig[i]) + w * tgt;
    }
  }
  return out;
}
function interpStroke(sd, i) {
  let a = null; let b = null;
  for (const [t, m] of sd.pts) {
    const k = frameIndex(t);
    if (k <= i) a = [k, m];
    if (k >= i && b == null) b = [k, m];
  }
  if (!a) return b ? b[1] : 0;
  if (!b || b[0] === a[0]) return a[1];
  return a[1] + (b[1] - a[1]) * (i - a[0]) / (b[0] - a[0]);
}

/** 鉛筆: (tSrc, midi) まで線を引く（前の点との間のフレームを埋める。戻って描いたら上書き）。
 *
 *   前の点とこの点を結ぶ線分を、**各フレームの正確な時刻**で読む（フレームに丸めて後勝ちにすると、
 *   描いた向きと逆へ最大で半フレームずれる）。同じフレームをまたがない短い動きは、次の点で線分が
 *   そのフレームを越えるまで値を持たない（先端は画面の strokeTip が生の位置で見せる）。 */
export function strokeTo(tSrc, midi) {
  const st = S.stroke;
  if (!st) return;
  const f0 = S.vd.f0;
  const f = (tSrc - f0.t0_sec) / f0.hop_sec;          // 小数のフレーム位置
  const n = f0.take_midi.length;
  if (st.last) {
    const { f: fa, m: ma } = st.last;
    if (f !== fa) {
      const lo = Math.ceil(Math.min(fa, f) - 1e-9);
      const hi = Math.floor(Math.max(fa, f) + 1e-9);
      for (let k = Math.max(0, lo); k <= Math.min(n - 1, hi); k++) st.vals.set(k, ma + (midi - ma) * (k - fa) / (f - fa));
    }
  } else {
    const k = Math.round(f);
    if (k >= 0 && k < n) st.vals.set(k, midi);
  }
  st.last = { f, m: midi };
}

/** 元に戻す線（ペンの右ドラッグ）: tSrc までなぞった範囲の有声のフレームに、録音のピッチを入れる。
 *
 *   前の点との間のフレームも全部通る（なぞった区間は途切れない）。戻す値 = 録音のピッチ（take_midi）。 */
export function restoreTo(tSrc) {
  const st = S.stroke;
  if (!st) return;
  const f0 = S.vd.f0;
  const tm = f0.take_midi;
  const f = (tSrc - f0.t0_sec) / f0.hop_sec;
  const fa = st.last ? st.last.f : f;
  const lo = Math.max(0, Math.ceil(Math.min(fa, f) - 1e-9));
  const hi = Math.min(tm.length - 1, Math.floor(Math.max(fa, f) + 1e-9));
  for (let k = lo; k <= hi; k++) if (tm[k] != null) st.vals.set(k, tm[k]);
  st.last = { f, m: 0 };
  const a = Math.round(Math.min(fa, f)); const b = Math.round(Math.max(fa, f));
  st.span = st.span ? [Math.min(st.span[0], a), Math.max(st.span[1], b)] : [a, b];
}

export function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

/** ドラッグ中に来た move が、**左ボタンを離した後**のものか（離したことが届いていない）。
 *
 * 押している間に OS のマウスの動き（押していない実マウス・ウィンドウの切り替えなど）が割り込むと、
 * Chromium はポインタのキャプチャを外し（lostpointercapture）、ボタンを押していない move を送ってくる。
 * pointerup は来ないことがある。これをドラッグの続きとして扱うと、ボタンを離したのにノートの端が
 * マウスについて行き、次にどこかで離した位置で当たる（見た目と違うところに当たる）。
 * buttons を持たない呼び出し（Shift を押しただけで描き直す onMove など）は対象にしない。 */
export const buttonReleased = (e) => typeof e?.buttons === 'number' && (e.buttons & 1) === 0;

const NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
export const BLACK = [1, 3, 6, 8, 10];
export function noteName(m) { return NAMES[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1); }
export function fmtTime(t) {
  if (t < 0) return `−${fmtTime(-t)}`;         // トラックを前へずらすとタイムラインが 0 より前から始まる
  const m = Math.floor(t / 60); const s = t - m * 60;
  return `${m}:${s < 10 ? '0' : ''}${s.toFixed(3)}`;
}
export function sign(v) { return (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v); }

/** 編集前の秒 → 編集後の秒（区分線形）。 */
export function toEdited(t) {
  const m = S.vd?.time_map;
  if (!m) return t;
  return interp(t, m.src_sec, m.out_sec);
}
/** 編集後の秒 → 編集前の秒。 */
export function toSource(t) {
  const m = S.vd?.time_map;
  if (!m) return t;
  return interp(t, m.out_sec, m.src_sec);
}
function interp(t, xs, ys) {
  if (!xs || xs.length === 0) return t;
  if (t <= xs[0]) return ys[0] + (t - xs[0]);
  const n = xs.length;
  if (t >= xs[n - 1]) return ys[n - 1] + (t - xs[n - 1]);
  let lo = 0; let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= t) lo = mid; else hi = mid;
  }
  const d = xs[hi] - xs[lo];
  return d > 0 ? ys[lo] + (t - xs[lo]) / d * (ys[hi] - ys[lo]) : ys[lo];
}

// ---------------------------------------------------------------- 音素の局所補間
// 境界を 1 本つまむ／タイミングのスライダーを動かす間は、エンジンを呼ばずに
// ここで時間軸を曲げる（離した時点で move_boundary / correct_to_guide を呼ぶ）。
// S.local.btime は「境界の index → 編集後の時刻（秒）」の上書き。

/** 編集後の秒 → ドラッグ中の見かけの秒。何も動かしていなければそのまま。
 *
 * side は無音を挟んだ時刻の読み方（'left' = 手前 = ノートの尻、'right' = 頭）。 */
export function warp(tEd, side = 'right') {
  if (S.plan) return planWarp(tEd, side);
  const m = warpMap();
  if (!m) return tEd;
  return interp(tEd, m.src, m.dst);
}

/** 見かけの秒 → 編集後の秒（波形を描くため）。計画の無音の中は null。 */
export function unwarp(t) {
  if (!S.plan) {
    const m = warpMap();
    return m ? interp(t, m.dst, m.src) : t;
  }
  const P = planArrays();
  if (!P) return t;
  const n = P.from.length;
  if (t <= P.to[0]) return P.from[0] + (t - P.to[0]);
  if (t >= P.to[n - 1]) return P.from[n - 1] + (t - P.to[n - 1]);
  let k = 0;
  while (k < n - 2 && P.to[k + 1] < t) k++;
  const fa = P.from[k]; const fb = P.from[k + 1]; const ta = P.to[k]; const tb = P.to[k + 1];
  if (P.mode[k] === 'g') return (t >= fa && t <= fb) ? t : null;
  return tb - ta > 1e-12 ? fa + (t - ta) * (fb - fa) / (tb - ta) : fa;
}

let _warpCache = null;
let _planCache = null;
export function invalidateWarp() { _warpCache = null; _planCache = null; }

function warpMap() {
  if (!S.local.btime.size || !S.bounds.length) return null;
  if (_warpCache) return _warpCache;
  const src = []; const dst = [];
  for (const b of S.bounds) {
    const v = S.local.btime.get(b.index);
    src.push(b.edited_sec);
    dst.push(v == null ? b.edited_sec : v);
  }
  _warpCache = { src, dst };
  return _warpCache;
}

/** 計画の節の「表示中の位置（x0）」と「いまの位置（x）」。 */
function planArrays() {
  const pl = S.plan;
  if (!pl || !pl.data) return null;
  if (_planCache && _planCache.x === pl.x && _planCache.x0 === pl.x0
    && _planCache.data === pl.data) return _planCache;
  const ks = pl.data.knots;
  const from = new Array(ks.length); const to = new Array(ks.length);
  for (let i = 0; i < ks.length; i++) {
    const [, cur, d] = ks[i];
    from[i] = cur + d * (pl.x0 || 0);
    to[i] = cur + d * (pl.x || 0);
  }
  _planCache = { x: pl.x, x0: pl.x0, data: pl.data, from, to, mode: pl.data.pieces };
  return _planCache;
}

/** 計画で動かした見かけの秒（エンジンの `timing.py: realize` と同じ規則）。
 *
 *   stretch / keep の区間: 両端の節の間を線形に（keep は長さが変わらない）
 *   gap（切り離された隙間）: 中身は元の位置のまま、端だけ動く（はみ出しは端に寄せる） */
export function planWarp(t, side = 'right') {
  const P = planArrays();
  if (!P) return t;
  const n = P.from.length;
  // view data の秒は 1 µs に丸めてあるので、節とはその程度の誤差で「同じ」とみなす
  const E = 2e-6;
  if (n < 2 || t < P.from[0] - E || t > P.from[n - 1] + E) return t;
  // side = right: from[k] <= t の最大の k、left: from[k] >= t の最小の k の手前
  let lo = 0; let hi = n - 1;
  let k;
  if (side === 'left') {
    while (lo < hi) { const m = (lo + hi) >> 1; if (P.from[m] >= t - E) hi = m; else lo = m + 1; }
    k = Math.max(0, lo - 1);
  } else {
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (P.from[m] <= t + E) lo = m; else hi = m - 1; }
    k = Math.min(lo, n - 2);
  }
  const fa = P.from[k]; const fb = P.from[k + 1]; const ta = P.to[k]; const tb = P.to[k + 1];
  if (P.mode[k] === 'g') return clamp(t, ta, tb);
  if (fb - fa <= 1e-12) return side === 'left' ? ta : tb;
  return ta + (t - fa) * (tb - ta) / (fb - fa);
}

/** 計画のピッチ（ガイドに合わせる）: 表示中の値からの差（半音）。 */
function planPitch(id) {
  const pl = S.plan;
  if (!pl || !pl.data || !pl.data.pitch) return 0;
  if (pl.data.params?.match_pitch_shape) return 0; // 鉛筆と同じ層: 帯の中心は変わらない
  const c = pl.data.pitch[id];
  if (c == null) return 0;
  return c * ((pl.pitch || 0) - (pl.pitch0 || 0)) / 100;
}

/** 音素の（ドラッグ中を含む）編集後の区間。 */
export function phSpan(p) {
  return [warp(p.edited_start_sec, 'right'), warp(p.edited_end_sec, 'left')];
}
/** 境界の（ドラッグ中を含む）編集後の時刻。 */
export function boundSec(b) {
  const v = S.local.btime.get(b.index);
  return v == null ? b.edited_sec : v;
}

/** ドラッグ中の差分を込みにしたノートの位置と音程。 */
export function pitchDelta(id) {
  return (S.local.pitch.get(id) || 0) + planPitch(id);
}
export function pitchOf(n) {
  return (n.edited_pitch_midi ?? n.pitch_midi ?? 0) + pitchDelta(n.id);
}
/** 帯（blob）を置く高さ = ノートの平均の音程（音量で重み付け、無声・子音を除く。エンジンの `band_midi`）
 * ＋ドラッグ中の差分。ピッチのドラッグ・ガイドに合わせるはノート全体を同じ量ずらすので、
 * 平均もちょうどその量だけ動く（ドラッグ中 = 離した後）。 */
export function bandOf(n) {
  return (n.band_midi ?? n.edited_pitch_midi ?? n.pitch_midi ?? 0) + pitchDelta(n.id);
}
export function guideBandOf(g) { return g.band_midi ?? g.pitch_midi; }
/** ノートのフェード（編集後の秒。ドラッグ中はその値）。{ fi, fo } */
export function fadeOf(n) {
  const l = S.local.fade.get(n.id);
  return l || { fi: n.fade_in_sec || 0, fo: n.fade_out_sec || 0 };
}
/** 時刻 t（見かけの秒）での音量の倍率（フェードの形 = 等パワー。エンジンの fades.apply_fades と同じ）。 */
export function fadeGain(n, t, span = null) {
  const { fi, fo } = fadeOf(n);
  if (fi <= 0 && fo <= 0) return 1;
  const [a, b] = span || spanOf(n);
  let g = 1;
  if (fi > 0 && t < a + fi) g *= Math.sin(Math.PI / 2 * clamp((t - a) / fi, 0, 1));
  if (fo > 0 && t > b - fo) g *= Math.sin(Math.PI / 2 * clamp((b - t) / fo, 0, 1));
  return g;
}
export function spanOf(n) {
  const a = warp(n.edited_start_sec, 'right');
  const b = warp(n.edited_end_sec, 'left');
  const d = S.edgeDraft;
  if (!d || d.id !== n.id || d.trackId !== S.session?.current || (d.planId && S.plan?.data?.plan_id === d.planId)) return [a, b];
  return d.which === 'start' ? [Math.min(a + d.want, b - LAYOUT.MIN_SEG), b]
    : [a, Math.max(b + d.want, a + LAYOUT.MIN_SEG)];
}
export function boxOf(n) {
  const p = pitchOf(n);
  const d = pitchDelta(n.id);
  const lo = Math.min((n.edited_lo_midi ?? p) + d, p - 0.4);
  const hi = Math.max((n.edited_hi_midi ?? p) + d, p + 0.4);
  const [s, e] = spanOf(n);
  return { s, e, lo: lo - 0.5, hi: hi + 0.5 };
}
export function guideBox(g) {
  const lo = Math.min(g.lo_midi ?? g.pitch_midi, g.pitch_midi - 0.4);
  const hi = Math.max(g.hi_midi ?? g.pitch_midi, g.pitch_midi + 0.4);
  return { s: g.start_sec, e: g.end_sec, lo: lo - 0.5, hi: hi + 0.5 };
}

export function isSel(id) { return S.sel.indexOf(id) >= 0; }
/** 無音のノートか（ミュートツールでなぞった分・当たるのを待っている分を含む）。 */
export function isMuted(n) { return S.local.mute.has(n.id) ? S.local.mute.get(n.id) : !!n.muted; }

/** 選択が無ければ全体が対象（モックと同じ）。 */
export function targets() {
  return S.sel.length ? S.sel.slice() : S.pitched.map((n) => n.id);
}

export function selectionRange() {
  if (!S.sel.length) return {};
  let a = Infinity; let b = -Infinity;
  for (const id of S.sel) {
    const n = S.byId.get(id);
    if (!n) continue;
    a = Math.min(a, n.start_sec);
    b = Math.max(b, n.end_sec);
  }
  return Number.isFinite(a) ? { start_sec: a, end_sec: b } : {};
}

// ---------------------------------------------------------------- 縦の表示（音程。v3 §9）
export const PITCH_VIEW = { MIN: 6, MAX: 36, MARGIN: 12 };

/** 縦にスクロールできる範囲 [下, 上]（MIDI）: データの範囲の上下に 1 オクターブ。 */
export function pitchWorld() {
  const lo = Math.max(0, S.midiLo - PITCH_VIEW.MARGIN);
  const hi = Math.min(127, S.midiHi + PITCH_VIEW.MARGIN);
  return [lo, Math.max(hi, lo + PITCH_VIEW.MAX)];
}

/** 既定の縦の表示: データの範囲（上下 3 半音の余白込み）。36 半音を超えるときは真ん中の 36 半音。 */
export function defaultPitchView() {
  const span = clamp(S.midiHi - S.midiLo + 1, PITCH_VIEW.MIN, PITCH_VIEW.MAX);
  const mid = (S.midiLo + S.midiHi) / 2;
  return clampPitchView({ top: mid + span / 2, span });
}

/** 縦の表示を範囲に収める（半音の数は 6〜36、上下はスクロールできる範囲の中）。 */
export function clampPitchView(pv) {
  const span = clamp(pv.span, PITCH_VIEW.MIN, PITCH_VIEW.MAX);
  const [lo, hi] = pitchWorld();
  return { span, top: clamp(pv.top, lo + span, hi) };
}

// ---------------------------------------------------------------- AI の編集の見分け
/** 取り消しの履歴の項目の名前。AI（Claude Code など）の操作には「AI · 」を付ける（ツールチップ・編集メニュー・ステータス）。 */
export function histLabel(e) {
  if (!e?.label) return e?.label || null;
  return e.author === 'ai' ? `AI · ${e.label}` : e.label;
}

/** 最後に当たっている編集が AI のものであるノート（枠を AI の色にする）。
 * 編集（view data の edits。当たっている順）の対象: ノートの id か、範囲（音素の境目も範囲を持つ）と重なるノート。 */
export function aiNotesOf(edits, notes) {
  const last = new Map();
  for (const e of edits) {
    const t = e.target || {};
    if (t.note_id) { last.set(t.note_id, e.author); continue; }
    if (t.start_sec == null || t.end_sec == null) continue;
    for (const n of notes) if (n.start_sec < t.end_sec && n.end_sec > t.start_sec) last.set(n.id, e.author);
  }
  return new Set([...last].filter(([, a]) => a === 'ai').map(([id]) => id));
}

/** 新しい view-data を取り込む。 */
export function adopt(vd, { keepView = true } = {}) {
  if (S.stroke && S.stroke.trackId !== S.session?.current) {
    S.stroke = null;
    S.strokePhase = 'idle';
  }
  if (S.edgeDraft && S.edgeDraft.trackId !== S.session?.current) S.edgeDraft = null;
  S.vd = vd;
  // 曲の取り消しの履歴（セッション）の要約。セッションの無いプロジェクトはプロジェクトの changeset から
  const vh = vd.history || {};
  S.hist = 'undo' in vh ? vh : { ...vh, undo: vh.can_undo ? { label: '編集' } : null,
    redo: vh.can_redo ? { label: '編集' } : null };
  S.notes = vd.notes || [];
  S.pitched = S.notes.filter((n) => n.kind === 'note');
  // タイミングの単位（音程ノート・子音・息。種類によらず同じ規則。無音は隙間）と、記号の高さ・当たりの元にする音程ノート
  S.blocks = S.notes.filter((n) => BLOCK_KINDS.has(n.kind)).sort((a, b) => a.start_sec - b.start_sec);
  S.blockAnchor = anchorsOf(S.blocks);
  S.byId = new Map(S.notes.map((n) => [n.id, n]));
  S.aiNotes = aiNotesOf(vd.edits || [], S.pitched);
  S.local.pitch.clear();
  S.local.btime.clear();
  // フェードのつまみをドラッグ中は、そのノートの見かけの値を残す（前の編集の描き直しで消えない）
  const fd = S.drag?.type === 'fade' ? S.local.fade.get(S.drag.id) : null;
  S.local.fade.clear();
  if (fd) S.local.fade.set(S.drag.id, fd);
  // ミュートツールでなぞっている間は、なぞった分を残す（前の編集の描き直しで消えない）
  const mu = S.drag?.type === 'mute' ? [...S.local.mute] : [];
  S.local.mute.clear();
  for (const [id, v] of mu) S.local.mute.set(id, v);
  invalidateWarp();
  S.ph = vd.phonemes?.has_lyrics ? vd.phonemes : null;
  S.phById = new Map((S.ph?.phonemes || []).map((p) => [p.id, p]));
  S.bounds = S.ph?.boundaries || [];
  S.boundById = new Map(S.bounds.map((b) => [b.id, b]));
  S.lyrics = vd.lyrics || null;
  S.lyricsEntries = vd.lyrics_entries || [];
  S.projectDir = vd.project_dir;
  S.take = vd.take;
  S.guide = vd.guide;
  syncOff();

  // 縦の範囲（固定。データから決める）
  const vals = [];
  for (const v of vd.f0?.take_midi || []) if (v != null) vals.push(v);
  for (const v of vd.f0?.guide_midi || []) if (v != null) vals.push(v);
  if (vals.length) {
    vals.sort((a, b) => a - b);
    const lo = vals[Math.floor(vals.length * 0.005)];
    const hi = vals[Math.floor(vals.length * 0.995)];
    S.midiLo = Math.floor(lo) - 3;
    S.midiHi = Math.ceil(hi) + 3;
    if (S.midiHi - S.midiLo < 12) S.midiHi = S.midiLo + 12;
  }
  S.pv = (!keepView || !S.pv) ? defaultPitchView() : clampPitchView(S.pv);
  const total = Math.max(0.5, toEdited(vd.duration_sec));
  if (!keepView || !(S.view.span > 0)) {
    S.view = { t0: 0, span: total };
  }
  S.view.span = Math.min(S.view.span, total);
  S.view.t0 = clamp(S.view.t0, 0, Math.max(0, total - S.view.span));
  S.sel = S.sel.filter((id) => S.byId.has(id));
  return S;
}

/** 編集対象のプロジェクトが無い（新規の空のプロジェクト・伴奏だけ・閉じた。issue #33）: 下のエディターを空にする。 */
export function clearProject() {
  S.vd = null;
  S.notes = [];
  S.pitched = [];
  S.byId = new Map();
  S.aiNotes = new Set();
  S.ph = null;
  S.phById = new Map();
  S.bounds = [];
  S.boundById = new Map();
  S.lyrics = null;
  S.lyricsEntries = [];
  S.projectDir = null;
  S.take = null;
  S.guide = null;
  S.sel = [];
  S.plan = null;
  S.stroke = null;
  S.strokePhase = 'idle';
  S.edgeDraft = null;
  S.lastEdgeTiming = null;
  S.boundHover = null;
  S.loop = null;
  S.head = 0;
  S.pv = null;
  S.view = { t0: 0, span: 0 };
  S.local.pitch.clear();
  S.local.btime.clear();
  S.local.fade.clear();
  S.local.mute.clear();
  invalidateWarp();
  syncOff();
}

// ---------------------------------------------------------------- 発声のかたまり
// 「無音で区切った塊」。歌詞レーンのダブルクリックで歌詞を付ける単位であり、
// タイムスケールのダブルクリックで飛ぶ先でもある。**編集前の秒**で持つ
// （`set_lyrics(start_sec, end_sec)` に渡す値がそのまま作れるように）。
export const UTTER_GAP = 0.30;
export const UTTER_PAD = 0.12;

/** 音程のあるノート（`kind === 'note'`）を 0.30 秒以内の隙間でつないだ塊。
 *
 * 息（breath）と無声（unvoiced）は入れない。158 秒の素材で試したところ、
 * 息を入れると全部が 1 つにつながり、無声を入れると曲の終わりまで伸びた。 */
export function utterances(gapSec = UTTER_GAP) {
  const out = [];
  for (const n of S.notes) {
    if (n.kind !== 'note') continue;
    const last = out[out.length - 1];
    if (last && n.start_sec - last[1] <= gapSec) last[1] = Math.max(last[1], n.end_sec);
    else out.push([n.start_sec, n.end_sec]);
  }
  return out;
}

/** その時刻（編集前の秒）を含む歌詞の区間。無ければ null。 */
export function lyricEntryAt(tSrc) {
  for (const e of S.lyricsEntries) {
    if (e.start_sec == null) return { ...e, start_sec: null, end_sec: null, whole: true };
    if (tSrc >= e.start_sec - 0.05 && tSrc <= e.end_sec + 0.05) return e;
  }
  return null;
}

/** その時刻を含む（無ければ 1 秒以内でいちばん近い）発声のかたまり。 */
export function utteranceAt(tSrc, pad = UTTER_PAD, near = 1.0) {
  let best = null; let bd = Infinity;
  for (const [a, b] of utterances()) {
    if (tSrc >= a && tSrc <= b) return [Math.max(0, a - pad), b + pad];
    const d = tSrc < a ? a - tSrc : tSrc - b;
    if (d < bd) { bd = d; best = [Math.max(0, a - pad), b + pad]; }
  }
  return bd <= near ? best : null;
}

/** 次（dir=+1）／前（dir=−1）の発声のかたまり。 */
export function nextUtterance(tSrc, dir = 1) {
  const us = utterances();
  if (!us.length) return null;
  if (dir > 0) return us.find(([a]) => a > tSrc + 1e-3) || us[us.length - 1];
  const prev = us.filter(([, b]) => b < tSrc - 1e-3);
  return prev.length ? prev[prev.length - 1] : us[0];
}

export const BLOCK_KINDS = new Set(['note', 'unvoiced', 'breath']);

/** 区間 → 高さの元にする音程ノート（音程ノートはその自身、子音・息は直前の音程ノート。無ければ直後）。 */
function anchorsOf(blocks) {
  const out = new Map();
  let last = null;
  let nextIdx = 0;
  blocks.forEach((n, i) => {
    if (n.kind === 'note') { last = n; out.set(n.id, n); return; }
    if (last) { out.set(n.id, last); return; }
    while (nextIdx < blocks.length && (nextIdx <= i || blocks[nextIdx].kind !== 'note')) nextIdx++;
    out.set(n.id, blocks[nextIdx] || null);
  });
  return out;
}

export function totalSec() {
  return Math.max(0.5, toEdited(S.vd?.duration_sec || 1));
}

// ---------------------------------------------------------------- 境界の移動
/** 境界を dt 秒動かしたときの上書きを作る。両隣は 20 ms を切らない。 */
export function setBoundaryDrag(boundaryId, dt) {
  const b = S.boundById.get(boundaryId);
  S.local.btime.clear();
  invalidateWarp();
  if (!b) return 0;
  const i = S.bounds.indexOf(b);
  const prev = S.bounds[i - 1];
  const next = S.bounds[i + 1];
  if (!prev || !next) return 0;
  const lo = prev.edited_sec + LAYOUT.MIN_PH - b.edited_sec;
  const hi = next.edited_sec - LAYOUT.MIN_PH - b.edited_sec;
  const d = clamp(dt, Math.min(lo, 0), Math.max(hi, 0));
  S.local.btime.set(b.index, b.edited_sec + d);
  invalidateWarp();
  return d;
}

/** 計画を画面に載せる（`plan_edit` の JSON）。x / pitch は 0 から。 */
export function setPlan(data, extra = {}) {
  const shapeFrames = new Map((data?.pitch_curve || []).map(([t, h0, h1, w]) => [frameIndex(t), [h0, h1, w]]));
  S.plan = data ? {
    data, x: 0, x0: 0, pitch: 0, pitch0: 0,
    shapeFrames,
    guides: new Set((data.pairs || []).flatMap((p) => p.guide)),
    takes: new Set((data.pairs || []).flatMap((p) => p.take)),
    ...extra,
  } : null;
  invalidateWarp();
}

/** 計画の x を動かす（範囲で止める）。止めた後の値を返す。 */
export function setPlanX(x) {
  if (!S.plan) return 0;
  const [lo, hi] = S.plan.data.x_range;
  S.plan.x = clamp(x, lo ?? -Infinity, hi ?? Infinity);
  invalidateWarp();
  return S.plan.x;
}
