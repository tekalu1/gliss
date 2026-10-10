// ポインタとキーボード。モックの操作系をそのまま実装する。
//
//  - モード切替なし。blob の中央を上下＝ピッチ、左右＝ノートの移動（最初の 4 px の向きで決める）。
//  - ノートの端を左右＝長さ。接続された端は隣と境目を共有して動く。Alt+ドラッグで切り離して
//    自分だけ動く。切り離された端を隣にぶつかるまで伸ばすと吸着して接続になる。
//  - タイミングはエンジンの計画（plan_edit）で描き、離したら同じ計画で確定（apply_plan）。
//  - 空白か歌詞レーンのドラッグで範囲選択、Shift+クリックで追加。
//  - 右クリックは menus.js（右クリックした所の対象のメニュー。issue #17）、キーは commands.js（コマンドの表）。
//  - 数値はドラッグ中のツールチップだけ。
//  - スナップ（issue #18。grid.js）: 時間スナップ（N）はノートの端・移動・はさみの位置をグリッドに、音程スナップ（Shift+N）は
//    ピッチのドラッグで帯の高さ（平均の音程）を半音に寄せる。Shift を押している間は解除。
//  - フェード（issue #20）: ホバー中のノートの帯の上の両端の四角を内側へドラッグ（離したら set_fade）。
//  - プレビュー音（issue #27。audio.js）: ノート・端をつかんだらそのノートを鳴らし、ピッチのドラッグ中は高さに追従、離したら止める。
//  - 子音・息（音程の無いノート。issue #35）: 帯をつかんで横に動かす・ノートの端で幅を変える（音程のあるノートと同じ計画・
//    接続・Alt・スナップ・取り消し）。上下には動かない（音程が無いので、動かし始めの向きによらず横の移動にする）。
//  - ツール（ヘッダーのアイコン / 1・2・3・4）: 矢印（上のとおり）、鉛筆（ドラッグでピッチを描く）、
//    はさみ（ノートをクリックで分割、境目をダブルクリックで結合。音素境界の近くは吸着、Alt で吸着なし）、
//    ミュート（ノートをクリックで無音⇔戻す。なぞると、押したノートと同じ向きにそろえる。離したら 1 つの編集）。
import {
  BLOCK_KINDS, LAYOUT, S, boxOf, buttonReleased, clamp, fadeOf, invalidateWarp, isMuted, isSel, lyricEntryAt, pitchWorld,
  setBoundaryDrag, setPlan, setPlanX, spanOf, strokeData, strokeTo, targets, toEdited, toSource,
  totalSec, utteranceAt,
} from './state.js';
import {
  M, T, X, Y, blockYSpans, edStep, focusRange, hasConnFocus, nearPair, pan, render, renderToolbar, rollBottom, rollTop,
  rowH, scrollPitch, size, zoom, zoomPitch,
} from './draw.js';
import {
  afterQueued, applyBoundary, applyDraw, applyFade, applyPitch, applyPlan, applyTransition, enqueue, idle,
  mergeNotes, muteNotes, refresh, requestPlan, restorePreviews, retryDraw, setLyrics, setNoteSyllable, splitNote, unmuteNotes, waitFor,
  wake,
} from './edits.js';
import { G, pitchSnapOn, saveGrid, snapTime, timeSnapOn } from './grid.js';
import { markNoteEdited, previewEnabled, previewState, releasePreview, setPreviewEnabled, startPreview, stop, stopPreview, updatePreview } from './audio.js';
import { ARA, araLoopEditor, araSeekEditor, pullHostState } from './ara.js';
import { status } from './engine.js';
import { closeMenu, editorMenu, menuOpen } from './menus.js';
import { runCommand, wheelAction } from './commands.js';
import { acceptCandidate, asrCandidate, inCandidate } from './asr.js';
import { toolHint } from './tracks.js';

const { KEYS_W } = LAYOUT;
const AXIS_PX = 4;          // blob の中央: 最初にこれだけ動いた向きで ピッチ／移動 を決める
const SNAP_PX = 8;          // 切り離された端が隣にこれだけ近づいたら吸い付く（はさみの音素境界も同じ）
const CUT_MIN_SEC = 0.02;   // 分割: 両端からこれだけ内側（エンジンと同じ）
const $ = (s) => document.querySelector(s);

let svg = null;
let root = null;
let saveView = () => {};
let auditionRestore = null;
let auditionNote = null;
let auditionLimit = null;

function auditionText(value) {
  const el = $('#auditionState');
  if (el) el.textContent = value;
}

function onPreviewState(e) {
  if (!auditionNote) return;
  const p = e.detail || previewState();
  if (p.phase === 'sounding') auditionText('試聴中');
  else if (p.phase === 'preparing') auditionText('試聴を準備中');
  else if (p.phase === 'idle') auditionText('');
  else if (p.phase === 'error') {
    auditionText('試聴できません');
    status(`試聴できません: ${p.error || 'ホストの出力を確認してください'}`);
  }
}

const AUDITION_KINDS = new Set(['note', 'unvoiced', 'breath']);    // 子音・息も鳴らせる（ずらさない = cents 0 だけ）

function beginAudition(id) {
  if (!id || !AUDITION_KINDS.has(S.byId.get(id)?.kind)) return;
  if (auditionNote === id) return;
  if (auditionNote) endAudition();
  auditionNote = id;
  auditionRestore = previewEnabled();
  if (!auditionRestore) setPreviewEnabled(true, { save: false });
  auditionText('試聴を準備中');
  startPreview(id);
  onPreviewState({ detail: previewState() });
}

/** 押した瞬間から試聴を始める（長押しの判定を待たない）。離すのが早くても、最短（releasePreview）は鳴る。 */
function scheduleNoteHold(dr, id) {
  if (dr.trackId === S.session?.current) beginAudition(id);
}

/** つかんだ区間と一緒に動かす区間（選んである音程ノート・子音・息。つかんだものが選んであるなら、その全部）。 */
function dragBlocks(id) {
  const ids = S.sel.filter((x) => BLOCK_KINDS.has(S.byId.get(x)?.kind));
  return ids.length ? ids : [id];
}

export function beginSelectedAudition({ once = false } = {}) {
  const id = S.sel.find((n) => AUDITION_KINDS.has(S.byId.get(n)?.kind));
  if (!id) { status('試聴するノートを選んでください'); return; }
  beginAudition(id);
  if (once) {
    clearTimeout(auditionLimit);
    auditionLimit = setTimeout(endAudition, 2500);
  }
}

/** 試聴をやめる。離したとき（既定）は最短だけ鳴らして止める。immediate: すぐ止める（フォーカスを失った・DAW が再生を始めた）。 */
export function endAudition({ immediate = false } = {}) {
  clearTimeout(auditionLimit);
  auditionLimit = null;
  if (immediate === true) stopPreview();
  else releasePreview();
  if (auditionRestore === false) setPreviewEnabled(false, { save: false });
  auditionRestore = null;
  auditionNote = null;
  auditionText('');
}

export function install(svgEl, rootEl, onViewChanged) {
  svg = svgEl;
  root = rootEl;
  saveView = onViewChanged || (() => {});
  window.addEventListener('gliss-preview-state', onPreviewState);
  svg.addEventListener('pointerdown', onDown);
  svg.addEventListener('pointerleave', () => {
    lastHover = null;
    let dirty = false;
    if (S.cutHover) { S.cutHover = null; dirty = true; }
    if (S.near && !S.drag) { S.near = null; dirty = true; }
    if (S.edgeHover && !S.drag) { S.edgeHover = null; dirty = true; }
    if (S.boundHover && !S.drag) { S.boundHover = null; dirty = true; }
    if ((S.noteHover || S.fadeHover) && !S.drag) { S.noteHover = null; S.fadeHover = null; dirty = true; }
    if (dirty) render();
  });
  // Alt を押したまま別のウィンドウへ移ると keyup が来ない: 予告を残さない
  window.addEventListener('blur', () => { if (S.alt) { S.alt = false; render(); } });
  window.addEventListener('blur', () => { clearTimeout(S.drag?.holdTimer); endAudition({ immediate: true }); });
  window.addEventListener('gliss-host-play', () => { clearTimeout(S.drag?.holdTimer); endAudition({ immediate: true }); });
  svg.addEventListener('pointermove', onMove);
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  // 離したことが届かなかった（押している間に OS のマウスの動きが割り込んでキャプチャが外れた）。
  // pointerup の後にも来るが、そのときはもう S.drag が無いので何もしない
  svg.addEventListener('lostpointercapture', releaseLost);
  svg.addEventListener('contextmenu', (e) => editorMenu(e, svg));
  svg.addEventListener('wheel', onWheel, { passive: false });
  root.addEventListener('keydown', onKey);
  root.addEventListener('keyup', onAltKey);
  installMenus();
}

const px = (e) => e.clientX - svg.getBoundingClientRect().left;
const py = (e) => e.clientY - svg.getBoundingClientRect().top;

// ---------------------------------------------------------------- ドラッグ
function onDown(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  S.near = null;             // 押したら、記号はドラッグ・選択の境目だけにする
  S.edgeHover = null;        // 端の縦線は、ドラッグ中はドラッグしている端に出す（draw.js）
  root.focus({ preventScroll: true });
  const d = e.target.dataset || {};

  if (d.keys !== undefined) return;          // 鍵盤: 何もしない（カーソルも矢印）
  if (d.scale !== undefined) {
    // タイムスケールの二度押し = その位置の**発声区間へ飛ぶ**（158 秒の素材で要る）
    if (isDoubleTap(scaleTap, e)) { jumpToUtterance(clamp(T(px(e)), 0, totalSec())); return; }
    S.araLoopDraft = null;
    S.drag = { type: 'scale', x0: e.clientX, t0: clamp(T(px(e)), 0, totalSec()), trackId: S.session?.current };
    svg.setPointerCapture(e.pointerId);
    return;
  }
  // 前の編集をエンジンが当てている間（S.busy・計画の確定待ち）でも、ドラッグは受け付ける。
  // 離したら前のが終わるのを待って順番に当てる（edits.enqueue）。計画は前のが当たってから頼む。
  if (S.tool === 'draw' && startStroke(e)) return;
  if (S.tool === 'cut' && cutDown(e, d)) return;
  if (S.tool === 'mute' && muteDown(e, d)) return;
  if (d.bound !== undefined) {
    // 音素境界のドラッグ。ノート境界と一致しなくても**音素境界が優先**。
    S.drag = { type: 'bound', id: d.bound, x0: e.clientX, moved: false, dt: 0 };
    svg.setPointerCapture(e.pointerId);
    render();
    return;
  }
  if (d.fade !== undefined) {
    // フェードのつまみ（v3 §5）: 内側へドラッグで長さ。反対側のフェードと合わせてノートの長さまで
    const n = S.byId.get(d.note);
    if (!n) return;
    const { fi, fo } = fadeOf(n);
    const [a, b] = spanOf(n);
    S.drag = { type: 'fade', id: n.id, side: d.fade, x0: e.clientX, moved: false,
      f0: d.fade === 'in' ? fi : fo, other: d.fade === 'in' ? fo : fi, L: b - a, v: d.fade === 'in' ? fi : fo };
    svg.setPointerCapture(e.pointerId);
    render();
    return;
  }
  if (d.nop !== undefined && S.tool === 'main') {
    // 音程のない区間: 端 = 幅、帯 = 横の移動。選択は元の長さの表示にも使う。
    const n = S.byId.get(d.nop);
    if (!n) return;
    if (e.shiftKey) S.sel = isSel(n.id) ? S.sel.filter((id) => id !== n.id) : [...S.sel, n.id];
    else if (!isSel(n.id)) S.sel = [n.id];     // 選んである区間を押しても選択を保つ（まとめて動かせる）
    if (d.nopEdge !== undefined) {
      const dr = { type: 'edge', id: n.id, which: d.nopEdge, x0: e.clientX, moved: false, alt: e.altKey, want: 0, nop: true,
        trackId: S.session?.current, startedAt: performance.now() };
      if (e.altKey) window.api.consumeAlt?.();
      planFor(dr, { op: 'edge', note_id: n.id, side: d.nopEdge, detach: e.altKey });
      S.drag = dr;
    } else {
      S.drag = { type: 'note', ids: dragBlocks(n.id), x0: e.clientX, y0: e.clientY, moved: false, anchor: n, axis: null,
        want: 0, nop: true, shift0: e.shiftKey, trackId: S.session?.current };
    }
    svg.setPointerCapture(e.pointerId);
    if (ARA) beginAudition(n.id);
    else startPreview(n.id);                 // 音程ノートと同じ（つかんだ区間を鳴らす。cents 0）
    render();
    return;
  }
  if (d.edge !== undefined) {
    // 端のドラッグ。計画はエンジンに作らせる（Alt = 接続を切って自分だけ動く）
    const dr = { type: 'edge', id: d.note, which: d.edge, x0: e.clientX, moved: false,
      alt: e.altKey, want: 0, trackId: S.session?.current, startedAt: performance.now() };
    if (e.altKey) window.api.consumeAlt?.();
    planFor(dr, { op: 'edge', note_id: d.note, side: d.edge, detach: e.altKey });
    S.drag = dr;
    svg.setPointerCapture(e.pointerId);
    if (ARA) beginAudition(d.note);
    else startPreview(d.note);
    render();
    return;
  }
  if (d.note !== undefined) {
    const id = d.note;
    const n = S.byId.get(id);
    if (e.shiftKey) {
      S.sel = isSel(id) ? S.sel.filter((x) => x !== id) : S.sel.concat([id]);
    } else if (!isSel(id)) {
      S.sel = [id];
    }
    // 横に動かすときは選んだ区間（子音・息も）をまとめて。音高のときは音程ノートだけ（onMove で dr.pids に絞る）
    const ids = dragBlocks(id);
    S.drag = { type: 'note', ids, x0: e.clientX, y0: e.clientY, moved: false, anchor: n,
      axis: null, want: 0, trackId: S.session?.current };
    svg.setPointerCapture(e.pointerId);
    if (ARA) scheduleNoteHold(S.drag, id);
    else startPreview(id);                   // 単体版のつかんだノートの試聴は従来どおり
    render();
    return;
  }
  const x = px(e); const y = py(e);
  if (x <= KEYS_W) return;
  const base = e.shiftKey ? S.sel.slice() : [];
  if (y > rollTop() && y < rollBottom()) {
    S.drag = { type: 'box', x0: x, y0: y, x1: x, y1: y, moved: false, shift: e.shiftKey, base };
  } else if (y >= rollBottom()) {
    // 歌詞レーンの二度押し = **クリックした位置を含む発声区間**に歌詞を付ける
    if (isDoubleTap(laneTap, e)) {
      const tSrc = toSource(T(x));
      // 聞き取りの候補の上 = 候補を入れた入力欄（直して Enter で採用。issue #54）
      if (inCandidate(tSrc)) { editCandidate(); return; }
      if (y < rollBottom() + 24 && openSyllable(tSrc)) return;
      openLyrics(tSrc);
      return;
    }
    S.drag = { type: 'lane', x0: x, y0: rollTop(), x1: x, y1: rollBottom(), moved: false, shift: e.shiftKey, base };
  }
  if (S.drag) svg.setPointerCapture(e.pointerId);
}

function selectRange(dr, rx0, rx1, ry0, ry1) {
  const sel = dr.base.slice();
  for (const n of S.pitched) {
    const b = boxOf(n);
    const x0 = X(b.s); const x1 = X(b.e); const y0 = Y(b.hi); const y1 = Y(b.lo);
    if (x0 < rx1 && x1 > rx0 && y0 < ry1 && y1 > ry0 && sel.indexOf(n.id) < 0) sel.push(n.id);
  }
  // 子音・息も、描いている帯に範囲がかかれば入る（選んだ後の音程の操作は音程ノートだけに効く）
  const ys = blockYSpans();
  for (const n of S.blocks) {
    const y = ys.get(n.id);
    if (!y) continue;
    const [s, e] = spanOf(n);
    if (X(s) < rx1 && X(e) > rx0 && y[0] < ry1 && y[1] > ry0 && sel.indexOf(n.id) < 0) sel.push(n.id);
  }
  S.sel = sel;
}

/** 画面の横の px → 秒。 */
function pxToSec(dxPx) {
  const { W } = size();
  return dxPx / (W - KEYS_W) * S.view.span;
}

/** 端・移動のドラッグの計画を頼む。**前に離した操作が当たり終わってから**頼む
 * （計画は当たった後の状態から作る。前の確定の途中で頼むと、前の計画のプレビューを奪い合う）。 */
function planFor(dr, args) {
  if (dr.type === 'edge') {
    S.edgeDraft = { id: dr.id, which: dr.which, want: 0, trackId: dr.trackId, drag: dr, planId: null };
  }
  const started = performance.now();
  dr.planTimer = setTimeout(() => {
    if (!dr.canceled && S.edgeDraft?.drag === dr) status('ノートの端を準備中…');
  }, 450);
  dr.planReq = afterQueued()
    .then(() => dr.canceled || dr.trackId !== S.session?.current ? null : requestPlan(args))
    .then((data) => {
      clearTimeout(dr.planTimer);
      if (!data) return null;
      if (dr.canceled || dr.trackId !== S.session?.current) return null;
      dr.planId = data.plan_id;
      if (S.edgeDraft?.drag === dr) S.edgeDraft.planId = data.plan_id;
      if (S.drag === dr) { setPlan(data); dragX(dr); render(); }
      dr.planMs = performance.now() - started;
      return data;
    })
    .catch((err) => { clearTimeout(dr.planTimer); status(`計画を作れなかった: ${err.message}`); return null; });
}

/** 端・移動のドラッグで動かす時刻（編集後の秒。計画を当てる前）: 端ならその端、移動ならつかんだノートの頭。 */
function dragEdgeTime(dr) {
  if (dr.type === 'edge') {
    const n = S.byId.get(dr.id);
    return n ? (dr.which === 'start' ? n.edited_start_sec : n.edited_end_sec) : null;
  }
  const a = dr.anchor && S.byId.get(dr.anchor.id);
  return a ? a.edited_start_sec : null;
}

/** ドラッグの量を計画に入れる（範囲で止める・吸着）。**このドラッグの計画のときだけ**
 * （計画が届く前は、前のドラッグの確定待ちの計画が S.plan に載っていることがある）。
 * 時間スナップ中は動かす時刻（タイムラインの秒）をグリッドに寄せる（Shift で解除。issue #18）。 */
function dragX(dr) {
  if (!S.plan || S.plan.data.plan_id !== dr.planId) return 0;
  let want = dr.want;
  if (timeSnapOn({ shiftKey: !!dr.shift })) {
    const e0 = dragEdgeTime(dr);
    if (e0 != null) want = snapTime(S.off + e0 + want, edStep()) - S.off - e0;
  }
  let x = setPlanX(want);
  const snap = S.plan.data.snap_x;
  if (snap != null && Math.abs(dr.want - snap) <= pxToSec(SNAP_PX)) x = setPlanX(snap);
  dr.dt = x;
  return x;
}

function onMove(e) {
  const dr = S.drag;
  G.shift = !!e.shiftKey;
  if (!dr) {
    if (S.tool === 'cut') cutHover(e);
    else connHover(e);
    return;
  }
  // ボタンはもう離されている: この move（離した後のポインタの位置）では動かさず、離したことにする
  if (buttonReleased(e)) { releaseLost(); return; }
  // Shift を押す・離すだけでスナップの有無を描き直せるように、最後のポインタを覚える
  dr.last = { clientX: e.clientX, clientY: e.clientY, shiftKey: !!e.shiftKey, altKey: !!e.altKey, target: e.target };
  dr.shift = !!e.shiftKey;
  if (dr.type === 'mute') { muteMove(dr, e); return; }
  if (dr.type === 'fade') {
    const dxPx = e.clientX - dr.x0;
    if (Math.abs(dxPx) >= 2) dr.moved = true;
    const dt = pxToSec(dxPx);
    const v = clamp(dr.f0 + (dr.side === 'in' ? dt : -dt), 0, Math.max(0, dr.L - dr.other));
    dr.v = v;
    S.local.fade.set(dr.id, dr.side === 'in' ? { fi: v, fo: dr.other } : { fi: dr.other, fo: v });
    render();
    return;
  }
  if (dr.type === 'stroke') {
    strokeTo(toSource(T(px(e))), clamp(M(py(e)), ...pitchWorld()));
    dr.moved = true;
    render();
    return;
  }
  if (dr.type === 'note') {
    const dx = e.clientX - dr.x0;
    const dy = e.clientY - dr.y0;
    if (!dr.axis) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < AXIS_PX) return;
      // 子音・息は横だけ（音程が無い。issue #35）
      dr.axis = dr.nop || Math.abs(dx) > Math.abs(dy) ? 'time' : 'pitch';
      if (dr.nop && Math.abs(dy) > Math.abs(dx)) status('子音・息は音程が無いので、横にだけ動かせます');
      if (dr.axis === 'pitch') dr.pids = dr.ids.filter((x) => S.byId.get(x)?.pitch_editable);
      clearTimeout(dr.holdTimer);
      if (dr.axis === 'time') { if (!ARA) stopPreview(); planFor(dr, { op: 'move', note_ids: dr.ids }); }   // ARA は鳴らし続ける（cents 0）
      else if (ARA) beginAudition(dr.anchor?.id);
    }
    dr.moved = true;
    if (dr.axis === 'time') {
      dr.want = pxToSec(dx);
      dragX(dr);
      render();
      return;
    }
    const ds = -dy / rowH();
    // 差分はドラッグ自身が持つ（S.local.pitch は描き直し（adopt）で消える。離す直前に
    // 描き直しが入っても、離したときの量で当てる）
    // 同じノートの前のドラッグがまだ当たっていない（順番待ち）ときは、その分も足した位置から動かす
    // （shift_pitch は相対なので、足さないと見た目より前の分だけ高く／低く当たる）
    dr.deltas = new Map();
    const snapP = pitchSnapOn(e);
    for (const id of dr.pids) {
      const n = S.byId.get(id);
      if (!n) continue;
      const q = queuedPitch.get(id) || 0;
      const base = (n.edited_pitch_midi ?? n.pitch_midi ?? 0) + q;
      const [wlo, whi] = pitchWorld();
      // 音程スナップ: 移動量ではなく帯の高さ（ノートの平均の音程）を半音に合わせる（Melodyne と同じ。v3 §4）
      let dd = ds;
      if (snapP) {
        const band0 = (n.band_midi ?? n.edited_pitch_midi ?? n.pitch_midi ?? 0) + q;
        dd = Math.round(band0 + ds) - band0;
      }
      const want = clamp(base + dd, wlo + 0.5, whi - 0.5);
      dr.deltas.set(id, want - base);
      S.local.pitch.set(id, q + want - base);
    }
    updatePreview();
    render();
  } else if (dr.type === 'bound') {
    const dxPx = e.clientX - dr.x0;
    if (Math.abs(dxPx) >= 2) dr.moved = true;
    const { W } = size();
    dr.dt = setBoundaryDrag(dr.id, dxPx / (W - KEYS_W) * S.view.span);
    render();
  } else if (dr.type === 'edge') {
    const dxPx = e.clientX - dr.x0;
    if (Math.abs(dxPx) >= 2) dr.moved = true;
    dr.want = pxToSec(dxPx);
    if (S.edgeDraft?.drag === dr) S.edgeDraft.want = dr.want;
    dragX(dr);
    render();
  } else if (dr.type === 'box') {
    dr.x1 = px(e); dr.y1 = py(e);
    if (Math.abs(dr.x1 - dr.x0) > 3 || Math.abs(dr.y1 - dr.y0) > 3) dr.moved = true;
    if (dr.moved) {
      selectRange(dr, Math.min(dr.x0, dr.x1), Math.max(dr.x0, dr.x1),
        Math.min(dr.y0, dr.y1), Math.max(dr.y0, dr.y1));
    }
    render();
  } else if (dr.type === 'lane') {
    dr.x1 = px(e);
    if (Math.abs(dr.x1 - dr.x0) > 3) dr.moved = true;
    if (dr.moved) {
      selectRange(dr, Math.min(dr.x0, dr.x1), Math.max(dr.x0, dr.x1), rollTop(), rollBottom());
    }
    render();
  } else if (dr.type === 'scale') {
    const t1 = clamp(T(px(e)), 0, totalSec());
    if (Math.abs(e.clientX - dr.x0) > 3) {
      dr.loopDraft = [Math.min(dr.t0, t1), Math.max(dr.t0, t1)];
      if (ARA) S.araLoopDraft = { trackId: dr.trackId, range: dr.loopDraft };
      else S.loop = dr.loopDraft.map((t) => t + S.off);
      render();
    }
  }
}

/** 離したこと（pointerup）が届かなかったドラッグを、**最後に見せていた位置で**離したことにする
 * （pointercancel と同じく確定する。見た目 = 離した後。離した操作は捨てない）。 */
function releaseLost() {
  const dr = S.drag;
  if (!dr) return;
  endDrag({ clientX: dr.last?.clientX ?? dr.x0, clientY: dr.last?.clientY ?? dr.y0 });
}

function endDrag(e) {
  endAudition();
  const dr = S.drag;
  if (!dr) return;
  clearTimeout(dr.holdTimer);
  S.drag = null;
  dr.releasedAt = performance.now();
  if (dr.moved && (dr.type === 'note' || dr.type === 'edge')) markNoteEdited(dr.type === 'note' ? (dr.pids || dr.ids) : [dr.id]);   // 試聴は編集したノートだけ厳密に
  if (dr.type === 'stroke') { finishStroke(); return; }
  if (dr.type === 'fade') { finishFade(dr); return; }
  if (dr.type === 'mute') { finishMute(dr); return; }
  if (dr.type === 'note' && dr.moved && dr.axis === 'pitch') {
    const deltas = new Map(dr.deltas || S.local.pitch);
    for (const [id, v] of deltas) queuedPitch.set(id, (queuedPitch.get(id) || 0) + v);
    const unqueue = () => {
      // 当たった（view data に入った）・失敗した・Ctrl+Z で外した: もう順番待ちの分ではない
      for (const [id, v] of deltas) {
        const r = (queuedPitch.get(id) || 0) - v;
        if (Math.abs(r) < 1e-9) queuedPitch.delete(id); else queuedPitch.set(id, r);
      }
    };
    // まだ当たっていない間のプレビュー（前の操作の描き直しの後にも載せ直す）
    const preview = () => {
      if (S.drag?.type === 'note' && S.drag.axis === 'pitch') return;   // ドラッグ中は、そのドラッグが足して持つ
      for (const [id, v] of deltas) S.local.pitch.set(id, (S.local.pitch.get(id) || 0) + v);
    };
    enqueue(() => {
      // 待っている間に前の編集の描き直しでプレビューが消えている: 当てる直前に載せ直す
      restorePreviews();
      for (const [id, v] of deltas) S.local.pitch.set(id, (S.local.pitch.get(id) || 0) + v);
      return applyPitch(deltas).finally(unqueue);
    }, { label: 'ピッチ', preview, cancel: unqueue });
    return;
  }
  if ((dr.type === 'note' && dr.axis === 'time') || dr.type === 'edge') {
    finishPlanDrag(dr);
    return;
  }
  if (dr.type === 'bound') {
    if (dr.moved) {
      const dt = dr.dt || 0;
      enqueue(() => applyBoundary(dr.id, setBoundaryDrag(dr.id, dt)), {
        label: '音素の境界',
        cancel: () => { S.local.btime.clear(); invalidateWarp(); },
      });
      return;
    }
    S.local.btime.clear();
    invalidateWarp();
  }
  if ((dr.type === 'box' || dr.type === 'lane') && !dr.moved && !dr.shift) S.sel = [];
  if (dr.type === 'scale' && !dr.loopDraft) {
    if (!ARA) S.loop = null;               // プラグインのループは DAW のもの（クリックでは解除しない）
    seekEditorTime(dr.t0);
    if (S.playing) stop();
  } else if (ARA && dr.type === 'scale') {
    if (dr.trackId !== S.session?.current) { S.araLoopDraft = null; render(); return; }
    const draft = dr.loopDraft;
    const held = S.araLoopDraft;
    araLoopEditor(draft[0], draft[1]).then((r) => {
      if (S.araLoopDraft !== held) return;
      if (!r?.ok) { S.araLoopDraft = null; status('DAW のその範囲をループできない'); render(); return; }
      setTimeout(async () => {
        if (S.araLoopDraft !== held) return;
        await pullHostState();
        if (S.araLoopDraft === held) { S.araLoopDraft = null; render(); }
      }, 1200);
    }).catch((err) => {
      if (S.araLoopDraft !== held) return;
      S.araLoopDraft = null; status(`DAW のループを設定できない: ${err.message}`); render();
    });
  }
  render();
}

/** フェードのつまみを離した: 変わっていれば set_fade（順番待ち。当たるまで帯はドラッグした形のまま）。 */
function finishFade(dr) {
  if (!dr.moved || Math.abs(dr.v - dr.f0) < 5e-4) {
    S.local.fade.delete(dr.id);
    render();
    return;
  }
  const val = dr.side === 'in' ? { fi: dr.v, fo: dr.other } : { fi: dr.other, fo: dr.v };
  S.local.fade.set(dr.id, val);
  const preview = () => { S.local.fade.set(dr.id, val); };
  const patch = dr.side === 'in' ? { fadeIn: val.fi } : { fadeOut: val.fo };
  enqueue(() => {
    restorePreviews();
    preview();
    return applyFade([dr.id], patch);
  }, { label: 'フェード', preview, cancel: () => { if (S.local.fade.get(dr.id) === val) S.local.fade.delete(dr.id); } });
  render();
}

/** 離したが、まだ当たっていないピッチのドラッグの量（ノートごとの合計、半音）。 */
const queuedPitch = new Map();

/** 端・移動のドラッグを離した: 前に離した操作と計画を待ってから、同じ計画を同じ x で確定。 */
let plansPending = 0;
async function finishPlanDrag(dr) {
  if (!dr.moved) {
    dr.canceled = true;
    clearTimeout(dr.planTimer);
    if (S.edgeDraft?.drag === dr) S.edgeDraft = null;
    if (dr.planId && S.plan?.data.plan_id === dr.planId) setPlan(null);
    render();
    return;
  }
  // 確定し終わるまでは「計画の確定待ち」（はさみの結合はこの間受け付けない。テストもこれを待つ）
  plansPending += 1;
  S.pendingPlan = true;
  const cancel = () => {
    dr.canceled = true;
    clearTimeout(dr.planTimer);
    if (S.edgeDraft?.drag === dr) S.edgeDraft = null;
    if (dr.planId && S.plan?.data.plan_id === dr.planId) setPlan(null);
  };
  try {
    await enqueue(async () => {
      const data = await dr.planReq;
      if (!data || dr.canceled || dr.trackId !== S.session?.current) { cancel(); render(); return; }
      if (!S.plan || S.plan.data.plan_id !== data.plan_id) setPlan(data);
      dr.planId = data.plan_id;
      const x = dragX(dr);
      if (!dr.moved || Math.abs(x) < 1e-6) {
        if (S.edgeDraft?.drag === dr) S.edgeDraft = null;
        if (S.plan?.data.plan_id === data.plan_id) setPlan(null);
        render();
        return;
      }
      await waitFor(() => !S.busy);        // 計画を待つ間に Ctrl+Z などが走り出していた
      if (dr.canceled || dr.trackId !== S.session?.current) { cancel(); render(); return; }
      dr.applying = true;
      if (S.edgeDraft?.drag === dr) S.edgeDraft = null;
      const timing = {};
      const result = await applyPlan({ planId: data.plan_id, x, onTiming: (v) => Object.assign(timing, v) });
      if (S.plan?.data.plan_id === data.plan_id) {
        if (result.ok) await refresh().catch(() => {}); // 先の再取得が新しい世代に負けた場合
        if (S.plan?.data.plan_id === data.plan_id) setPlan(null);
        render();
      }
      if (dr.type === 'edge') S.lastEdgeTiming = {
        planMs: dr.planMs, pointerMs: dr.releasedAt - dr.startedAt,
        queueMs: performance.now() - dr.releasedAt - (timing.applyMs || 0) - (timing.viewMs || 0),
        ...timing, totalMs: performance.now() - dr.startedAt,
      };
    }, { label: dr.type === 'edge' ? 'ノートの長さ' : 'ノートの移動', cancel });
  } finally {
    if (S.edgeDraft?.drag === dr) S.edgeDraft = null;
    plansPending -= 1;
    S.pendingPlan = plansPending > 0;
    wake();
  }
}

/** 矢印: 近づいた境目にだけ接続の記号を出す。端のつかみに乗ったら端に明るい縦線（変わったときだけ描き直す）。 */
function connHover(e) {
  const near = nearPair(px(e), py(e));
  const d = e.target.dataset || {};
  const eh = d.edge !== undefined ? { id: d.note, which: d.edge }
    : d.nopEdge !== undefined ? { id: d.nop, which: d.nopEdge } : null;
  const bh = d.bound !== undefined ? d.bound : null;
  const sameEdge = (!eh && !S.edgeHover)
    || (eh && S.edgeHover && eh.id === S.edgeHover.id && eh.which === S.edgeHover.which);
  // ノートに乗っている間は濃くし、帯の上の角にフェードのつまみを出す（子音・息も同じ。鍵盤の上は出さない）
  const hid = d.note !== undefined ? d.note : d.nop;
  const nh = (S.tool === 'main' || S.tool === 'mute') && hid !== undefined && BLOCK_KINDS.has(S.byId.get(hid)?.kind) ? hid : null;
  const fh = nh && d.fade !== undefined ? `${nh}|${d.fade}` : null;
  // Alt はポインタのイベントの値も見る（フォーカスが外にあって keydown を取りこぼしたとき）
  if (near === S.near && e.altKey === S.alt && sameEdge && nh === S.noteHover && fh === S.fadeHover
    && bh === S.boundHover) return;
  S.near = near;
  S.boundHover = bh;
  S.alt = e.altKey;
  S.edgeHover = eh;
  S.noteHover = nh;
  S.fadeHover = fh;
  render();
}

// ---------------------------------------------------------------- 鉛筆
/** ピアノロールの上でドラッグを始めたら線を描く（タイムスケール・歌詞レーンはふつうどおり）。 */
function startStroke(e) {
  const x = px(e); const y = py(e);
  if (x <= KEYS_W || y <= rollTop() || y >= rollBottom() || !S.vd) return false;
  if (S.stroke && S.strokePhase !== 'drawing') {
    status('前の描線を確定または取り消してから描いてください');
    return true;
  }
  S.stroke = { vals: new Map(), last: null, trackId: S.session?.current, phase: 'drawing' };
  S.strokePhase = 'drawing';
  strokeTo(toSource(T(x)), clamp(M(y), ...pitchWorld()));
  S.drag = { type: 'stroke', moved: false };
  svg.setPointerCapture(e.pointerId);
  render();
  return true;
}

/** 離した: 描いた線をエンジンへ。無声だけ・短すぎる線は捨てる（描いても効かない）。 */
function finishStroke() {
  let sd = strokeData();
  if (!sd) { S.stroke = null; S.strokePhase = 'idle'; render(); return; }
  if (sd.pts.length === 1 && sd.v0 >= 0) {
    // 同じ解析フレーム内の短い一筆も、隣の有声フレームまでを最小の描線にする。
    const k = sd.lo;
    const tm = S.vd.f0.take_midi;
    const other = k + 1 < tm.length && tm[k + 1] != null ? k + 1 : k - 1;
    if (other >= 0 && tm[other] != null) {
      S.stroke.vals.set(other, sd.pts[0][1]);
      sd = strokeData();
    }
  }
  if (sd.pts.length < 2) {
    S.stroke = null; S.strokePhase = 'idle';
    status('この区間は短すぎて描けません（有声の区間を少し長くなぞってください）');
    render(); return;
  }
  if (sd.v0 < 0) {
    S.stroke = null;
    S.strokePhase = 'idle';
    status('無声のところには描けない（音程が無いので効かない）');
    render();
    return;
  }
  // 描いている間に前の編集（Ctrl+Z・タイミングの確定）が走っていても捨てない。線を出したまま
  // 前のが終わるのを待って当てる（描いた線は編集前の秒と MIDI なので、前の編集の後でも同じ意味）
  const st = S.stroke;
  st.projectDir = S.projectDir;
  const pts = sd.pts.map(([t, m]) => [+t.toFixed(6), +m.toFixed(4)]);
  st.points = pts;
  st.phase = S.strokePhase = 'pending';
  enqueue(() => applyDraw(pts, st), {
    label: '鉛筆', cancel: () => { if (S.stroke === st) { S.stroke = null; S.strokePhase = 'idle'; } },
  });
  render();
}

// ---------------------------------------------------------------- はさみ
/** ノートの上の x（px）→ 切る位置（編集後の秒・編集前の秒）。音素境界が 8 px 以内なら寄せる（Alt で寄せない）。
 * 時間スナップ中はグリッドに寄せる（Shift で解除。issue #18）。 */
function cutAt(n, x, alt, shift = false) {
  let tEd = T(x);
  let src = toSource(tEd);
  if (timeSnapOn({ shiftKey: shift })) {
    tEd = snapTime(tEd + S.off, edStep()) - S.off;
    src = toSource(tEd);
  } else if (!alt) {
    let best = null; let bd = SNAP_PX + 1;
    for (const b of S.bounds) {
      // 切れない位置（両端から 20 ms 以内。歌詞が無いときはノートの端そのもの）へは寄せない
      if (b.sec < n.start_sec + CUT_MIN_SEC || b.sec > n.end_sec - CUT_MIN_SEC) continue;
      const dx = Math.abs(X(b.edited_sec) - x);
      if (dx < bd) { bd = dx; best = b; }
    }
    if (best && bd <= SNAP_PX) { tEd = best.edited_sec; src = best.sec; }
  }
  const ok = src >= n.start_sec + CUT_MIN_SEC && src <= n.end_sec - CUT_MIN_SEC;
  return { t: tEd, src: +src.toFixed(4), ok };
}

let lastHover = null;       // はさみ: 最後のポインタ（Alt を押す・離すだけで切る線を描き直す）
function cutHover(e, alt = e.altKey) {
  lastHover = { target: e.target, clientX: e.clientX, clientY: e.clientY, shiftKey: !!e.shiftKey };
  const d = e.target.dataset || {};
  const n = d.note !== undefined ? S.byId.get(d.note) : null;
  const c = n ? cutAt(n, px(e), alt, !!e.shiftKey) : null;
  const next = c && c.ok ? { id: n.id, t: c.t, src: c.src } : null;
  const same = (!S.cutHover && !next) || (S.cutHover && next && S.cutHover.id === next.id
    && Math.abs(S.cutHover.t - next.t) < 1e-9);
  S.cutHover = next;
  if (!same) render();
}

const joinTap = { t: 0, x: 0, key: null };
/** はさみ: ノートをクリックで分割、接した境目をダブルクリックで結合。 */
function cutDown(e, d) {
  if (d.join !== undefined) {
    const now = Date.now();
    const hit = joinTap.key === d.join && now - joinTap.t < 450 && Math.abs(px(e) - joinTap.x) < 8;
    joinTap.t = now; joinTap.x = px(e); joinTap.key = d.join;
    if (hit) {
      joinTap.key = null;
      const [a, b] = d.join.split('|');
      // 結合はクリックだけの操作で、対象（2 つのノート）が前の編集で変わりうるので待たずに断る。
      // 黙って捨てず、断ったことを出す
      if (!idle()) status('前の編集を当てている間は結合しない。当て終わってからもう一度ダブルクリックしてください');
      else mergeNotes(a, b);
    }
    return true;
  }
  joinTap.key = null;
  if (d.note === undefined) return false;          // 空白はふつうどおり（範囲選択）
  const n = S.byId.get(d.note);
  if (!n) return true;
  if (!idle()) {                                   // 結合と同じ理由で、待たずに断る（断ったことは出す）
    status('前の編集を当てている間は分割しない。当て終わってからもう一度クリックしてください');
    return true;
  }
  const c = cutAt(n, px(e), e.altKey, e.shiftKey);
  if (!c.ok) { status('ノートの端に近すぎる（両端から 20 ms 以上内側で分ける）'); return true; }
  if (e.altKey) window.api.consumeAlt?.();
  S.cutHover = null;
  splitNote(n.id, c.src);
  return true;
}

// ---------------------------------------------------------------- ミュート
/** 画面の点（クライアント座標）の下のノート（音程のあるノート・子音・息）。押している間は svg にキャプチャされて
 * e.target がノートにならないので、点の下の要素から引く（当たりははさみと同じ rect）。 */
function noteUnder(x, y) {
  const d = document.elementFromPoint(x, y)?.dataset || {};
  return d.note !== undefined ? S.byId.get(d.note) || null : null;
}

/** ミュート: ノートを押したら、そのノートの今の状態の反対（無音⇔戻す）を、なぞったノートすべてに当てる向きにする。 */
function muteDown(e, d) {
  const n = d.note !== undefined ? S.byId.get(d.note) : null;
  if (!n) return false;                        // 空白はふつうどおり（範囲選択）
  const to = !isMuted(n);
  S.drag = { type: 'mute', to, ids: [n.id], x0: e.clientX, y0: e.clientY, last: null };
  S.local.mute.set(n.id, to);
  svg.setPointerCapture(e.pointerId);
  render();
  return true;
}

/** なぞっている間: 通ったノートを押したノートと同じ向きにする（速く動かして飛ばした分は、前の点との間を細かく見る）。 */
function muteMove(dr, e) {
  const from = dr.at || { x: dr.x0, y: dr.y0 };
  const steps = Math.max(1, Math.ceil(Math.hypot(e.clientX - from.x, e.clientY - from.y) / 4));
  let dirty = false;
  for (let k = 1; k <= steps; k++) {
    const n = noteUnder(from.x + (e.clientX - from.x) * k / steps, from.y + (e.clientY - from.y) * k / steps);
    if (!n || dr.ids.includes(n.id)) continue;
    dr.ids.push(n.id);
    S.local.mute.set(n.id, dr.to);
    dirty = true;
  }
  dr.at = { x: e.clientX, y: e.clientY };
  if (dirty) render();
}

/** 離した: 向きが変わるノートだけを 1 回の呼び出し（= 取り消し 1 回）で当てる。 */
function finishMute(dr) {
  const ids = dr.ids.filter((id) => !!S.byId.get(id)?.muted !== dr.to);
  if (!ids.length) { for (const id of dr.ids) S.local.mute.delete(id); render(); return; }
  (dr.to ? muteNotes : unmuteNotes)(ids);
  render();
}

/** ツールを切り替える（1 / 2 / 3 / 4・ヘッダーのアイコン）。 */
export function setTool(tool) {
  if (S.tool === tool) return;
  S.tool = tool;
  S.cutHover = null;
  S.near = null;
  S.edgeHover = null;
  S.noteHover = null;
  S.fadeHover = null;
  closeMenu();
  if (S.drag?.type === 'stroke') { S.drag = null; S.stroke = null; }
  status(toolHint(tool));                 // 下と上（トラックビュー）での働きを 1 行で
  render();
}

// ---------------------------------------------------------------- ホイール（v3 §9・issue #27）
//   割り当ては設定（編集 > ショートカット（キー・ホイール）…）。既定は Studio One に合わせて
//   ホイール = 縦スクロール（音程）、Shift = 横スクロール、Ctrl = 縦ズーム（ポインタの下の音程を中心に 6〜36 半音）、
//   Ctrl+Shift = 横ズーム（ポインタの下の時刻を中心に）。タッチパッドのピンチは Ctrl+ホイールで届く（= 縦ズーム）。
//   Windows では Shift+ホイールが横の量（deltaX）で届くので、修飾キーがあるときは縦・横の量の両方を見る。
//   修飾キー無しの横の量（タッチパッドの横スクロール）は割り当てによらず横スクロール。
const WHEEL_NOTCH = 100;     // マウスのホイール 1 目盛りの量（px）
function onWheel(e) {
  e.preventDefault();
  const mods = e.ctrlKey || e.metaKey || e.shiftKey || e.altKey;
  if (!mods && e.deltaX) pan(e.deltaX * 0.6);
  const d = mods ? (e.deltaY || e.deltaX) : e.deltaY;
  if (d) {
    const act = wheelAction(e);
    if (act && e.altKey) window.api?.consumeAlt?.();   // Alt+ホイールの後に Alt を離してもメニューバーへ行かない
    wheelDo(act, d, e);
  }
  // 画面が動いたので、ポインタの下の境目を付け直す（前の境目の記号を残さない）
  if (!S.drag) { S.near = nearPair(px(e), py(e)); S.edgeHover = null; }
  render();
  saveView();
}

function wheelDo(act, d, e) {
  if (act === 'zoom-v') zoomPitch(d > 0 ? 1.15 : 1 / 1.15, py(e));
  else if (act === 'zoom-h') zoom(d > 0 ? 1.15 : 1 / 1.15, px(e));
  else if (act === 'scroll-v') {
    // 1 目盛りで見えている幅の 1/8（最低 1 半音）。下へ回すと低い方へ。タッチパッドの細かい量はその割合で
    const step = Math.max(1, (S.pv?.span || 12) / 8);
    scrollPitch(-clamp(d / WHEEL_NOTCH, -3, 3) * step);
  } else if (act === 'scroll-h') pan(d * 0.6);
}

// ---------------------------------------------------------------- ポップアップ
let pop = null;
// ポップアップ 1 回ぶんの確定の状態（開くたびに作り直す）。changeset = 前回当てたもの（当て直しで取り消す）。
// 確定は順番待ちに入れる（issue #16）ので、閉じた・開き直した後に当たったものは、そのとき開いていた回に残る
let popSes = { changeset: null };
let popPlan = null;          // Promise<plan JSON>
let popPlans = null;         // 同じ編集前の状態で作った形 ON / OFF の計画
let popTr = null;
let trSes = { changeset: null };
let trScope = null;          // { noteIds, keys }

function place(el, x, y, w, h) {
  const r = root.getBoundingClientRect();
  el.style.left = `${clamp(x, 0, r.width - w)}px`;
  el.style.top = `${clamp(y, 0, r.height - h)}px`;
}

function openPop(x, y) {
  closeMenu();
  closeTr();
  clearGuideStatus();
  popSes = { changeset: null };
  // 対象ノートが決まった時点で、エンジンに 100% の計画を 1 回だけ作らせる。
  // スライダーはこの計画に強度を掛けて描くだけ、離したら同じ計画・同じ強度で確定する。
  const ids = S.sel.filter((id) => S.byId.get(id)?.kind === 'note');
  setPlan(null);
  const args = { op: 'guide', note_ids: ids.length ? ids : null };
  const load = (shape) => requestPlan({ ...args, match_pitch_shape: shape })
    .catch((err) => { status(`計画を作れなかった: ${err.message}`); return null; });
  const plans = {
    shape: load(true),
    average: load(false),
  };
  popPlans = plans;
  $('#popPitchShape').checked = true;
  choosePopPlan();
  $('#popScope').textContent = ids.length ? `選択 ${ids.length} ノート` : '全体';   // 子音・息は数えない（計画の対象も音程ノートだけ）
  $('#popPitch').value = 0; $('#popPitchV').textContent = '0%';
  $('#popTime').value = 0; $('#popTimeV').textContent = '0%';
  pop.hidden = false;
  place(pop, x, y, 270, 130);
  $('#popPitch').focus();
}
function closePop() {
  if (pop.hidden) return;
  pop.hidden = true;
  clearGuideStatus();
  popSes = { changeset: null };
  popPlan = null;
  popPlans = null;
  if (S.plan?.data?.kind === 'guide') { setPlan(null); render(); }
}

function choosePopPlan() {
  const req = $('#popPitchShape').checked ? popPlans?.shape : popPlans?.average;
  if (!req) return;
  setPlan(null);
  popPlan = req;
  req.then((data) => {
    if (data && popPlan === req && !pop.hidden) { guideStatusText = guideStatus(data); status(guideStatusText); }
    if (data && popPlan === req && !pop.hidden && !popSes.changeset) {
      setPlan(data); previewMacro();
    }
  });
}

let guideStatusText = null;   // ポップアップが出したステータス行（閉じる・開き直すときに消す。前の対象の数を残さない）
function clearGuideStatus() {
  if (guideStatusText && $('#status')?.textContent === guideStatusText) status('');
  guideStatusText = null;
}

/** ステータス行: 対象ノートのうちガイドと確かに対応するもの・タイミングを合わせるもの（issue #53）。
 * 確かな発音の頭の組が 1 つも無いガイド（別の演奏など）は「タイミングを合わせられない」。 */
export function guideStatus(d) {
  const info = d.info || {};
  const rows = d.notes || [];
  if (!info.timing_possible) return info.timing_message || 'このガイドとはタイミングを合わせられない';
  const n = rows.length;
  const tim = rows.filter((r) => r.timing).length;
  const conf = rows.filter((r) => r.confirmed).length;
  const base = `ガイドとの対応: 確か ${conf}/${n} ノート・タイミング ${tim}/${n}`
    + `（基準点 ${info.timing_anchor_notes ?? 0}・補間 ${info.timing_interp_notes ?? 0}）`;
  return tim ? base : `${base}。選んだ範囲にはタイミングを合わせる基準点が無い`;
}

/** スライダーを動かしている間は計画に強度を掛けて描くだけ（エンジンは呼ばない）。 */
function previewMacro() {
  if (!S.plan || S.plan.data.kind !== 'guide') return;
  S.plan.pitch = +$('#popPitch').value / 100;
  S.plan.x = +$('#popTime').value / 100;
  invalidateWarp();
  render();
}

/** 離した: 同じ計画を同じ強度で確定する（前回ぶんはエンジンが取り消してから当てる）。
 *
 * 確定は順番待ちに入れる（前の編集を追い越さない・確定の途中でもう一度離しても取りこぼさない）。
 * 離したときの値で当てる。前回の changeset は当てる直前に見る（続けて離したら、前の確定の結果を取り消す）。 */
function releaseMacro() {
  const ses = popSes;
  const planReq = popPlan;
  const want = { pitch: +$('#popPitch').value / 100, x: +$('#popTime').value / 100 };
  return enqueue(async () => {
    const data = await planReq;
    if (!data) return;
    const active = () => ses === popSes && !pop.hidden && popPlan === planReq;
    const switching = S.plan?.data.plan_id !== data.plan_id;
    // 別モードの計画は前回の確定前の状態を基準にしている。差し替えが終わるまでは
    // その計画を現在の view data に重ねず、確定後の値をプレビューの基準にする。
    if (want.pitch === 0 && want.x === 0 && !ses.changeset) {
      if (active()) { setPlan(data); previewMacro(); }
      return;
    }
    const r = await applyPlan({ planId: data.plan_id, x: want.x, pitch: want.pitch,
      replaces: ses.changeset, drag: false });
    if (!r.ok) return;                              // 失敗: 前回の changeset はそのまま
    ses.changeset = r.changeset;                    // 0% に戻した: null（履歴の項目も消えている）
    if (active()) {
      if (switching) setPlan(data, { x0: want.x, pitch0: want.pitch });
      previewMacro();
    }
  }, { label: 'ガイドに合わせる' });
}

// ---- つなぎのなだらかさ（ガイドに合わせると同じ形。スライダー 1 本、離すたびに当て直す）
const trLabel = (v) => (v === 0 ? '段差' : v === 50 ? '自動' : String(v));
/** 中央付近は「自動」に吸い付ける（自動へ戻しやすく）。 */
const trDetent = (v) => (Math.abs(v - 50) <= 2 ? 50 : v);

let trGen = 0;               // ポップアップを開くたびに増やす（確定の await 中に開き直したら、古い結果を書き戻さない）
let trPointer = false;       // スライダーをポインタで動かしている（「自動」への吸着はこのときだけ）

/** pair: 'a|b'（右クリックした境目 1 つだけ）。省くと選択したノートの両側（選択が無ければ全体）。 */
function openTr(x, y, { pair = null } = {}) {
  closeMenu();
  closePop();
  trGen++;
  trSes = { changeset: null };
  const ids = pair ? pair.split('|') : S.sel.filter((id) => S.byId.get(id)?.kind === 'note');
  const sel = new Set(ids);
  const trs = (S.vd?.transitions || []).filter((t) => (pair ? `${t.a}|${t.b}` === pair
    : !ids.length || sel.has(t.a) || sel.has(t.b)));
  if (!trs.length) { status('接続された境目が無い（切り離しの境目には効かない）'); return; }
  trScope = { noteIds: ids, pair, keys: new Set(trs.map((t) => `${t.a}|${t.b}`)) };
  trPointer = false;
  const vals = [...new Set(trs.map((t) => Math.round(t.value * 100)))];
  const v0 = vals.length === 1 ? vals[0] : 50;
  $('#popTrScope').textContent = pair ? '境目 1 つ' : ids.length ? `選択 ${ids.length} ノート` : '全体';
  $('#popTrV').value = v0;
  // 境目ごとに値が違う: 「自動」と出すと事実と違う（動かすまでエンジンは呼ばない）
  $('#popTrVV').textContent = vals.length === 1 ? trLabel(v0) : '混在';
  popTr.hidden = false;
  place(popTr, x, y, 270, 64);
  $('#popTrV').focus();
}
function closeTr() {
  if (!popTr || popTr.hidden) return;
  popTr.hidden = true;
  trGen++;
  trSes = { changeset: null };
  trScope = null;
  if (S.trPreview) { S.trPreview = null; render(); }
}
/** 矢印キーでは 1 ずつ動かしたいので吸着しない（吸着して書き戻すと 48〜52 を抜けられない）。 */
function trValue() { const v = +$('#popTrV').value; return trPointer ? trDetent(v) : v; }
function previewTr() {
  if (!trScope) return;
  const v = trValue();
  $('#popTrVV').textContent = trLabel(v);
  S.trPreview = { keys: trScope.keys, value: v / 100 };
  render();
}
/** 離した: エンジンに当てる（前回ぶんは取り消してから）。確定は順番待ちに入れ、離したときの値で当てる。 */
function releaseTr() {
  if (!trScope || popTr.hidden) return Promise.resolve();
  const v = trValue();
  $('#popTrV').value = v;
  trPointer = false;
  previewTr();
  const ses = trSes;
  const noteIds = trScope.noteIds;
  const pair = trScope.pair;
  return enqueue(async () => {
    const r = await applyTransition({ value: v / 100, noteIds, pair, replaces: ses.changeset });
    if (r.ok) ses.changeset = r.changeset;          // 元の値に戻した: null（履歴の項目も消えている）
  }, { label: 'なだらかさ' });
}

function installMenus() {
  const auditionButton = $('#bAudition');
  auditionButton.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    auditionButton.setPointerCapture(e.pointerId);
    beginSelectedAudition();
  });
  auditionButton.addEventListener('pointerup', endAudition);
  auditionButton.addEventListener('pointercancel', endAudition);
  auditionButton.addEventListener('lostpointercapture', endAudition);
  auditionButton.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (!e.repeat) beginSelectedAudition();
  });
  auditionButton.addEventListener('keyup', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); endAudition(); }
  });
  $('#strokeRetry').addEventListener('click', () => { retryDraw().finally(render); });
  $('#strokeCancel').addEventListener('click', () => {
    S.stroke = null; S.strokePhase = 'idle'; status('描線を取り消しました'); render();
  });
  popTr = $('#popTr');
  $('#popTrV').addEventListener('pointerdown', () => { trPointer = true; });
  $('#popTrV').addEventListener('input', previewTr);
  $('#popTrV').addEventListener('change', releaseTr);
  for (const [id, tool] of [['#bToolMain', 'main'], ['#bToolDraw', 'draw'], ['#bToolCut', 'cut'], ['#bToolMute', 'mute']]) {
    $(id).addEventListener('click', () => { setTool(tool); root.focus({ preventScroll: true }); });
  }
  pop = $('#pop');
  $('#popPitch').addEventListener('input', (e) => {
    $('#popPitchV').textContent = `${e.target.value}%`;
    previewMacro();
  });
  $('#popTime').addEventListener('input', (e) => {
    $('#popTimeV').textContent = `${e.target.value}%`;
    previewMacro();
  });
  $('#popPitch').addEventListener('change', releaseMacro);
  $('#popTime').addEventListener('change', releaseMacro);
  $('#popPitchShape').addEventListener('change', () => { choosePopPlan(); releaseMacro(); });
  document.addEventListener('pointerdown', (e) => {
    const menu = $('#menu');
    if (!pop.hidden && !pop.contains(e.target) && !$('#bMacro').contains(e.target)
      && !menu.contains(e.target)) closePop();
    if (!popTr.hidden && !popTr.contains(e.target) && !menu.contains(e.target)) closeTr();
  }, true);

  // ヘッダーのボタンもコマンドの表を通す（キー・メニューと同じ実行）
  $('#bPlay').addEventListener('click', () => runCommand('play'));
  $('#bGuide').addEventListener('click', () => runCommand('guide-view'));
  $('#bUndo').addEventListener('click', () => runCommand('undo'));
  // スナップ（issue #18）とグリッドの細かさ
  $('#bFollow').addEventListener('click', () => { runCommand('follow'); root.focus({ preventScroll: true }); });
  $('#bSnapT').addEventListener('click', () => { runCommand('snap-time'); root.focus({ preventScroll: true }); });
  $('#bSnapP').addEventListener('click', () => { runCommand('snap-pitch'); root.focus({ preventScroll: true }); });
  $('#gridDiv').addEventListener('change', (e) => {
    if (e.target.dataset.mode === 'bars') G.divBars = e.target.value; else G.divSec = e.target.value;
    saveGrid();
    render();
    root.focus({ preventScroll: true });
  });
  $('#bRedo').addEventListener('click', () => runCommand('redo'));
  $('#bMacro').addEventListener('click', (e) => {
    if (!pop.hidden) { closePop(); return; }
    const r = root.getBoundingClientRect();
    const b = e.currentTarget.getBoundingClientRect();
    openPop(b.left - r.left, b.bottom - r.top + 2);
  });

  $('#lyrIn').addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); commitLyrics(); }
    if (e.key === 'Escape') { e.preventDefault(); closeLyrics(); }
  });
  $('#lyrIn').addEventListener('blur', () => closeLyrics());
}

// ---------------------------------------------------------------- 歌詞の入力
// ヘッダーには置かない（設計の方針「枠と情報量を最小に」）。
// **歌詞レーンをダブルクリックすると 1 行の入力欄**が出る。
let lyrRange = null;          // いま編集している歌詞の区間（編集前の秒）
let lyrSyllable = null;
let lyrCand = false;          // 聞き取りの候補を直している（Enter で候補の区間に採用する。issue #54）

function openSyllable(tSrc) {
  const syll = S.ph?.syllables?.find((s) => tSrc >= s.start_sec && tSrc < s.end_sec);
  if (!syll) return false;
  const notes = S.pitched.filter((n) => n.start_sec < syll.end_sec && n.end_sec > syll.start_sec);
  if (!notes.length) return false;
  const note = notes.reduce((best, n) => {
    const overlap = (v) => Math.max(0, Math.min(v.end_sec, syll.end_sec)
      - Math.max(v.start_sec, syll.start_sec));
    return overlap(n) > overlap(best) ? n : best;
  });
  openLyrics(tSrc);
  lyrSyllable = { noteId: note.id, index: syll.index };
  $('#lyrIn').value = syll.kana;
  $('#lyrIn').setAttribute('aria-label', '1音節のかな');
  $('#lyrIn').placeholder = '1音節のかな（例: か・しゃ・ん）';
  $('#lyrIn').dataset.range = `音節 ${syll.index + 1}`;
  $('#lyrIn').select();
  return true;
}

/** 聞き取りの候補を入力欄に入れて開く（ダブルクリック・右クリックの「直して採用…」）。Enter で採用。 */
export function editCandidate() {
  const c = asrCandidate();
  if (!c) return false;
  openLyrics((c.start_sec + c.end_sec) / 2);
  if ($('#lyr').hidden) return false;
  lyrRange = { start_sec: c.start_sec, end_sec: c.end_sec };
  lyrCand = true;
  const input = $('#lyrIn');
  input.value = c.lyrics || c.text || '';
  input.dataset.range = `${c.start_sec.toFixed(2)}–${c.end_sec.toFixed(2)}`;
  input.setAttribute('aria-label', '聞き取りの候補');
  input.placeholder = '聞き取りの候補を直す。Enter で歌詞にする、Esc でやめる';
  input.select();
  status(`聞き取りの候補を直す: ${c.start_sec.toFixed(2)}〜${c.end_sec.toFixed(2)} 秒（Enter で歌詞にする）`);
  return true;
}

function openLyrics(tSrc) {
  lyrSyllable = null;
  lyrCand = false;
  const box = $('#lyr');
  const input = $('#lyrIn');
  input.setAttribute('aria-label', '歌詞');
  input.placeholder = 'フレーズの歌詞（かな・カナ・漢字混じり可）。Enter で確定、Esc で取り消し';
  const r = root.getBoundingClientRect();
  const sr = svg.getBoundingClientRect();
  // 区間を決める: 既に歌詞のある区間ならそれを編集、無ければ発声のかたまりに新しく付ける
  let text = S.lyrics || '';
  lyrRange = null;
  if (tSrc != null) {
    const ent = lyricEntryAt(tSrc);
    if (ent && !ent.whole) {
      lyrRange = { start_sec: ent.start_sec, end_sec: ent.end_sec };
      text = ent.text || '';
    } else if (ent && ent.whole) {
      text = ent.text || '';
    } else {
      const u = utteranceAt(tSrc);
      if (!u) {
        status('ここには発声が無い（発声のあるところをダブルクリックする）');
        return;
      }
      lyrRange = { start_sec: +u[0].toFixed(3), end_sec: +u[1].toFixed(3) };
      text = '';
    }
  }
  box.hidden = false;
  box.style.left = `${LAYOUT.KEYS_W}px`;
  box.style.top = `${sr.top - r.top + rollBottom()}px`;
  box.style.width = `${Math.max(240, sr.width - LAYOUT.KEYS_W)}px`;
  input.value = text;
  input.dataset.range = lyrRange
    ? `${lyrRange.start_sec.toFixed(2)}–${lyrRange.end_sec.toFixed(2)}` : '';
  input.focus();
  input.select();
  status(lyrRange
    ? `歌詞: ${lyrRange.start_sec.toFixed(2)}〜${lyrRange.end_sec.toFixed(2)} 秒`
    : '歌詞: 素材全体');
}

export function closeLyrics() {
  const box = $('#lyr');
  if (box.hidden) return;
  box.hidden = true;
  root.focus({ preventScroll: true });
}

function commitLyrics() {
  const text = $('#lyrIn').value;
  const range = lyrRange;
  const syllable = lyrSyllable;
  const fromCand = lyrCand;
  if (syllable && !/^[ぁ-ゖァ-ヶ](?:[ゃゅょぁぃぅぇぉャュョァィゥェォー])?$/.test(text.trim())) {
    status('1音節のかなを入力（例: か・しゃ・ん）');
    $('#lyrIn').focus();
    $('#lyrIn').select();
    return;
  }
  closeLyrics();
  if (fromCand) {
    lyrCand = false;
    const p = asrCandidate() ? acceptCandidate(text) : setLyrics(text, range);
    p?.catch?.((err) => status(`歌詞にできなかった: ${err.message}`));
  } else if (syllable) setNoteSyllable(syllable.noteId, syllable.index, text);
  else setLyrics(text, range);
}

// `pointerdown` で preventDefault しているので `dblclick` は飛んでこない。
// 押した時刻と位置を見て自前で二度押しを取る（歌詞レーンとタイムスケールの 2 か所）。
const laneTap = { t: 0, x: 0 };
const scaleTap = { t: 0, x: 0 };
function isDoubleTap(store, e) {
  const x = px(e);
  const now = Date.now();
  const hit = (now - store.t < 450) && Math.abs(x - store.x) < 8;
  store.t = now;
  store.x = x;
  return hit;
}

/** その位置（編集後の秒）の発声区間へ飛ぶ。無ければいちばん近いかたまり。 */
export function jumpToUtterance(tEdited) {
  const u = utteranceAt(toSource(tEdited), 0.15, 1e9);
  if (!u) return false;
  focusRange(toEdited(u[0]), toEdited(u[1]));
  seekEditorTime(toEdited(u[0]));
  render();
  return true;
}

/** 下段の編集秒へ移動。ARA のソング秒はホスト通知だけを正とする。 */
export function seekEditorTime(sec) {
  if (!ARA) { S.head = sec + S.off; return; }
  araSeekEditor(sec).then((r) => {
    if (!r?.ok) status('DAW のその位置へ移動できない');
  }).catch((err) => status(`DAW の再生位置を動かせない: ${err.message}`));
}

// ---------------------------------------------------------------- キーボード
/** Alt を押す・離す。
 *   矢印: 押した瞬間、記号を出している境目の線を破線に（ここで切れる予告）
 *   はさみ: マウスを動かさなくても切る線を吸着あり／なしに描き直す */
function onAltKey(e) {
  if (e.key === 'Shift') { onShiftKey(e); return; }
  if (e.key !== 'Alt') return;
  const down = e.type === 'keydown';
  if (S.tool === 'main') {
    // 予告を出している間はメニューバーへ行かない（記号が無いときはふつうの Alt）
    if (down && hasConnFocus()) window.api.consumeAlt?.();
    if (S.alt !== down) { S.alt = down; render(); }
    return;
  }
  if (S.tool !== 'cut' || !lastHover || S.drag) return;
  if (e.type === 'keydown') window.api.consumeAlt?.();   // 離したときにメニューバーへ行かない
  cutHover(lastHover, e.type === 'keydown');
}

/** Shift を押す・離す: スナップの解除（ドラッグ中はその場で描き直す。はさみの切る線も）。 */
function onShiftKey(e) {
  const down = e.type === 'keydown';
  if (G.shift === down) return;
  G.shift = down;
  const dr = S.drag;
  if (dr?.last && (dr.type === 'note' || dr.type === 'edge')) onMove({ ...dr.last, shiftKey: down });
  else if (!dr && S.tool === 'cut' && lastHover) cutHover({ ...lastHover, shiftKey: down });
}

/** Esc・Alt・Shift だけ（ほかのキーはコマンドの表で割り当てる。commands.js の installKeys）。 */
function onKey(e) {
  if (e.key === 'Alt') { onAltKey(e); return; }
  if (e.key === 'Shift') { onShiftKey(e); return; }
  if (e.key === 'Escape') {
    if (S.drag?.type === 'stroke') { S.drag = null; S.stroke = null; render(); }
    else if (S.drag?.type === 'fade') { S.local.fade.delete(S.drag.id); S.drag = null; render(); }
    else if (menuOpen()) { if (closeMenu()) return; }    // プルダウン: 開いたボタンにフォーカスを返す
    else if (!pop.hidden) closePop();
    else if (!popTr.hidden) closeTr();
    else if (S.sel.length) { S.sel = []; render(); }
    root.focus({ preventScroll: true });
  }
}

export { closePop, closeTr, openLyrics, openPop, openTr };
