// 右クリックのメニュー（issue #17。v3 §2）。**右クリックした所の対象だけを出す**。9 か所:
//   ノート・複数選択・ノートの境目・空白（ピアノロール）・歌詞レーン・音素・トラック見出し・クリップ・ルーラー（上下とも）
// 1 つのメニューは 3〜7 項目、キーがあるものは右に表記（キーはコマンドの表と設定から。commands.js / keys.js）。
// 未選択のノートを右クリックすると、そのノートを選んでから出す（Melodyne・Studio One と同じ）。
// 見た目はモック（proposal/v3.html）と同じ: 左にチェックの列、右にキー、区切りは細い線。枠・色は足さない。
import {
  S, isSel, lyricEntryAt, offsetOf, spanOf, toSource, utteranceAt,
} from './state.js';
import { T, X, Y, render, rollBottom } from './draw.js';
import { command, isEnabled, runCommand, touching } from './commands.js';
import { keyText } from './keys.js';
import { closePop, closeTr, editCandidate, openLyrics } from './interact.js';
import { acceptCandidate, asrReady, asrWhy, dismissCandidate, inCandidate } from './asr.js';
import {
  connectGap, mergeNotes, resetBoundaries, setConnection, setLyrics,
} from './edits.js';
import {
  commitOffset, openGuidePicker, removeTrack, selectTrack, setGuide, setKind, setTrackGuide, soundRegion, startRename,
} from './tracks.js';
import { status } from './engine.js';
import { boxOf, bandOf } from './state.js';
import { G, tempo } from './grid.js';
import { openTempoPop } from './tempo.js';
import { renderTracks as redrawTracks } from './tracks.js';
import { ARA, araLoop } from './ara.js';

const SEP = { sep: true };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let menu = null;
let root = null;
let items = [];
let anchor = null;          // プルダウンとして出したときのボタンを返す関数（閉じたら aria-expanded を戻し、フォーカスを返す）
let closedAt = { el: null, t: 0 };   // 外を押して閉じたボタンと時刻（同じボタンの click で開き直さない）
const MENU_LABEL = '右クリックのメニュー';

export function installMenus(rootEl) {
  root = rootEl;
  menu = document.querySelector('#menu');
  menu.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    const it = items[+b.dataset.i];
    if (!closeMenu()) root.focus({ preventScroll: true });
    try {
      const r = it.run?.();
      if (r && typeof r.catch === 'function') r.catch((err) => status(`${it.label} に失敗: ${err.message}`));
    } catch (err) {
      status(`${it.label} に失敗: ${err.message}`);
    }
  });
  // 上下の矢印で項目を移る（Enter はボタンのまま。Esc は interact.js）
  menu.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    e.stopPropagation();
    const bs = [...menu.querySelectorAll('button:not([disabled])')];
    if (!bs.length) return;
    const i = bs.indexOf(document.activeElement);
    const j = e.key === 'ArrowDown' ? (i + 1) % bs.length : (i - 1 + bs.length) % bs.length;
    bs[j].focus();
  });
  document.addEventListener('pointerdown', (e) => {
    if (menu.hidden || menu.contains(e.target)) return;
    const a = anchor?.();
    if (a && a.contains(e.target)) closedAt = { el: a, t: performance.now() };
    closeMenu({ focus: false });
  }, true);
}

/** メニューを閉じる。プルダウン（anchor 付き）ならボタンにフォーカスを返して true（呼び手は root に移さない）。 */
export function closeMenu({ focus = true } = {}) {
  if (!menu || menu.hidden) return false;
  menu.hidden = true;
  menu.setAttribute('aria-label', MENU_LABEL);
  const a = anchor?.();
  anchor = null;
  if (!a) return false;
  a.setAttribute('aria-expanded', 'false');
  if (!focus || !a.isConnected) return false;
  a.focus({ preventScroll: true });
  return true;
}
export function menuOpen() { return !!menu && !menu.hidden; }
/** 外を押して閉じたばかりのボタンか（そのボタンの click でもう一度開かない）。 */
export function justClosed(el) { return closedAt.el === el && performance.now() - closedAt.t < 500; }

/** コマンドの項目（名前・キー・有効はコマンドの表から）。over で名前・実行を差し替える。 */
function cmd(id, ctx, over = {}) {
  const c = command(id);
  return {
    cmd: id,
    label: over.label || c.label,
    key: keyText(id),
    disabled: over.disabled ?? !isEnabled(id, ctx),
    checked: over.checked ?? (c.checked ? c.checked() : false),
    run: over.run || (() => runCommand(id, ctx)),
    title: over.title,
  };
}

/** メニューを出す（items の hidden は出さない。続く区切り・端の区切りは詰める）。
 * opts.label: 読み上げの名前。opts.anchor: プルダウンとして出すボタンを返す関数（見出しは描き直すので要素ではなく関数）。
 * 項目の radio: true は選択肢（menuitemradio。checked の項目に最初のフォーカス）。 */
export function openMenu(e, list, opts = {}) {
  e.preventDefault?.();
  if (anchor) closeMenu({ focus: false });
  closePop();
  closeTr();
  const out = [];
  for (const it of list) {
    if (!it || it.hidden) continue;
    if (it.sep && (!out.length || out[out.length - 1].sep)) continue;
    out.push(it);
  }
  while (out.length && out[out.length - 1].sep) out.pop();
  items = out;
  menu.innerHTML = items.map((it, i) => (it.sep ? '<hr>'
    : `<button role="${it.radio ? 'menuitemradio' : 'menuitem'}" data-i="${i}"${it.cmd ? ` data-cmd="${it.cmd}"` : ''}`
      + `${it.id ? ` data-item="${esc(it.id)}"` : ''}${it.radio ? ` aria-checked="${!!it.checked}"` : ''}`
      + `${it.disabled ? ' disabled' : ''}${it.title ? ` title="${esc(it.title)}"` : ''}>`
      + `<span class="c" aria-hidden="true">${it.checked ? '✓' : ''}</span><span class="l">${esc(it.label)}</span>`
      + `<span class="k">${esc(it.key || '')}</span></button>`)).join('');
  menu.setAttribute('aria-label', opts.label || MENU_LABEL);
  menu.hidden = false;
  anchor = opts.anchor || null;
  anchor?.()?.setAttribute('aria-expanded', 'true');
  const r = root.getBoundingClientRect();
  const w = menu.offsetWidth; const h = menu.offsetHeight;
  menu.style.left = `${Math.max(0, Math.min(e.clientX - r.left, r.width - w - 2))}px`;
  menu.style.top = `${Math.max(0, Math.min(e.clientY - r.top, r.height - h - 2))}px`;
  (menu.querySelector('button[aria-checked="true"]:not([disabled])')
    || menu.querySelector('button:not([disabled])'))?.focus({ preventScroll: true });
}

/** クリックの位置（root の中）。ポップアップを同じ所に出す。 */
function at(e) {
  const r = root.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

// ---------------------------------------------------------------- エディター（ピアノロール・歌詞レーン・ルーラー）

/** ノート・複数選択。 */
function noteMenu(e, n, t) {
  if (!isSel(n.id)) { S.sel = [n.id]; render(); }
  const ctx = { ...at(e), noteId: n.id, t };
  const multi = S.sel.length > 1;
  return [
    cmd('guide-match', ctx),
    cmd('semitone', ctx),
    multi ? cmd('merge', ctx) : cmd('split', ctx),
    cmd('transition', ctx),
    SEP,
    // フェード（issue #20）: フェードのあるノートのときだけ出す
    { ...cmd('clear-fade', ctx), hidden: !isEnabled('clear-fade', ctx) },
    cmd('reset-original', ctx),
    cmd('mute', ctx),
    // 無音のノートのときだけ（フェードを消すと同じ）
    { ...cmd('unmute', ctx), hidden: !isEnabled('unmute', ctx) },
    SEP,
    cmd('ask-ai', ctx),
  ];
}

/** ノートの境目（a の次が b）。 */
function boundaryMenu(e, a, b) {
  const ctx = { ...at(e), pair: `${a.id}|${b.id}` };
  const touch = touching(a, b);
  const conn = !!a.connected_next;
  const gap = spanOf(b)[0] - spanOf(a)[1];
  return [
    cmd('merge', ctx, { disabled: !touch, run: () => mergeNotes(a.id, b.id) }),
    conn
      ? { id: 'detach', label: '切り離す', key: 'Alt+ドラッグ', run: () => setConnection(a.id, b.id, false) }
      : { id: 'connect', label: 'つなぐ', key: 'Alt+ドラッグ',
        run: () => (gap > 0.0005 ? connectGap(a.id) : setConnection(a.id, b.id, true)) },
    cmd('transition', ctx, { disabled: !conn }),
  ];
}

function emptyMenu(e) {
  const ctx = at(e);
  return [
    cmd('select-all', ctx),
    SEP,
    cmd('snap-time', ctx),
    cmd('snap-pitch', ctx),
    SEP,
    cmd('show-all', ctx),
    SEP,
    // 選んだノート・ループの範囲があるときだけ（AI に頼む文をクリップボードへ）
    { ...cmd('ask-ai', ctx), hidden: !isEnabled('ask-ai', ctx) },
  ];
}

/** 歌詞レーン（上段）と音素（下段）。tSrc: 編集前の秒。 */
function laneMenu(e, tSrc, phRow) {
  const ph = phRow && S.ph
    ? (S.ph.phonemes || []).find((p) => p.label !== 'silence' && tSrc >= p.start_sec && tSrc < p.end_sec)
    : null;
  if (ph) {
    const bs = (S.bounds || []).filter((b) => b.before_index === ph.index || b.after_index === ph.index);
    const moved = bs.filter((b) => b.moved).map((b) => b.id);
    return [
      { id: 'reset-boundary', label: '子音｜母音の境目を元に戻す', disabled: !moved.length, run: () => resetBoundaries(moved) },
      { id: 'edit-lyrics', label: '歌詞を編集…', key: 'ダブルクリック', run: () => openLyrics(tSrc) },
    ];
  }
  const ent = lyricEntryAt(tSrc);
  const u = utteranceAt(tSrc);
  const load = cmd('load-lyrics', {}, { label: '歌詞を読み込む…' });
  // 聞き取りの候補の上（issue #54）: 採用・直して採用・閉じる
  if (inCandidate(tSrc)) {
    return [
      { id: 'asr-accept', label: '聞き取りの候補を歌詞にする', key: 'Enter', run: () => acceptCandidate() },
      { id: 'asr-edit', label: '直して歌詞にする…', key: 'ダブルクリック', run: () => editCandidate() },
      { id: 'asr-dismiss', label: '候補を閉じる', key: 'Esc', run: () => dismissCandidate() },
      SEP,
      cmd('transcribe', { t: tSrc }, { label: 'もう一度聞き取る' }),
    ];
  }
  if (!ent && !u) return [load];
  const has = !!(ent && (ent.text || '').trim());
  return [
    { id: 'edit-lyrics', label: has ? '歌詞を編集…' : '歌詞を入力…', key: 'ダブルクリック', run: () => openLyrics(tSrc) },
    { id: 'clear-lyrics', label: 'この区間の歌詞を消す', disabled: !has,
      run: () => setLyrics('', ent.whole ? null : { start_sec: ent.start_sec, end_sec: ent.end_sec }) },
    // 聞き取る（issue #54）: 使えないとき（faster-whisper が無い）は理由をツールチップに
    cmd('transcribe', { t: tSrc }, { title: asrReady() ? '' : asrWhy() }),
    SEP,
    load,
  ];
}

/** ルーラーの読み方を切り替える（小節・拍 / 分:秒。取り消しの履歴には入れない = 表示の操作）。 */
function setFmt(f) {
  G.fmt = f;
  render();
  redrawTracks();
}

/** ルーラー（上のトラックビューと下のエディターで同じ）。 */
function rulerMenu(e) {
  const t = tempo();
  const bars = !!t && G.fmt !== 'sec';
  const p = at(e);
  return [
    { id: 'clear-loop', label: 'ループを解除', disabled: !S.loop,
      run: () => { S.loop = null; render(); if (ARA) araLoop(null); } },
    { id: 'tempo', label: 'テンポと拍子…', disabled: !S.tracks.length, run: () => openTempoPop(p.x, p.y) },
    SEP,
    { id: 'fmt-bars', label: '表示: 小節・拍', disabled: !t, checked: bars, run: () => setFmt('bars'),
      title: t ? '' : 'テンポが無い（ヘッダーの「— BPM」か「テンポと拍子…」で入れる）' },
    { id: 'fmt-sec', label: '表示: 分:秒', checked: !bars, run: () => setFmt('sec') },
  ];
}

/** 空白の中の、隙間のある境目（前のノートの尻と次のノートの頭の間。縦は両方の帯の近く）。 */
function gapAt(x, y) {
  const P = S.pitched;
  for (let i = 0; i + 1 < P.length; i++) {
    const a = P[i]; const b = P[i + 1];
    const xa = X(spanOf(a)[1]); const xb = X(spanOf(b)[0]);
    if (x < xa || x > xb || xb - xa < 1) continue;
    const ba = boxOf(a); const bb = boxOf(b);
    const top = Math.min(Y(ba.hi), Y(bb.hi), Y(bandOf(a)), Y(bandOf(b))) - 16;
    const bot = Math.max(Y(ba.lo), Y(bb.lo), Y(bandOf(a)), Y(bandOf(b))) + 16;
    if (y >= top && y <= bot) return [a, b];
  }
  return null;
}

/** ピアノロール（#roll）の右クリック。 */
export function editorMenu(e, svg) {
  e.preventDefault();
  if (!S.vd) return;
  const r = svg.getBoundingClientRect();
  const x = e.clientX - r.left; const y = e.clientY - r.top;
  const d = e.target.dataset || {};
  if (d.scale !== undefined) { openMenu(e, rulerMenu(e)); return; }
  if (d.keys !== undefined) return;
  if (y >= rollBottom()) { openMenu(e, laneMenu(e, toSource(T(x)), y > rollBottom() + 22)); return; }
  const P = S.pitched;
  const idx = (id) => P.findIndex((n) => n.id === id);
  if (d.join !== undefined) {
    const [a, b] = d.join.split('|').map((id) => S.byId.get(id));
    if (a && b) { openMenu(e, boundaryMenu(e, a, b)); return; }
  }
  if (d.edge !== undefined) {
    // 隣と接している端（接続でも切り離しでも）= 境目のメニュー。隙間のある端はそのノートのメニュー
    const i = idx(d.note);
    const n = P[i];
    const nb = d.edge === 'end' ? P[i + 1] : P[i - 1];
    const conn = d.edge === 'end' ? n?.connected_next : n?.connected_prev;
    const shared = n && nb && (conn || (d.edge === 'end' ? touching(n, nb) : touching(nb, n)));
    if (shared) {
      const [a, b] = d.edge === 'end' ? [n, nb] : [nb, n];
      openMenu(e, boundaryMenu(e, a, b));
      return;
    }
    if (n) { openMenu(e, noteMenu(e, n, T(x))); return; }
  }
  if (d.note !== undefined) {
    const n = S.byId.get(d.note);
    if (n) { openMenu(e, noteMenu(e, n, T(x))); return; }
  }
  if (d.bound !== undefined) {
    const t = T(x);
    const n = P.find((m) => { const [a, b] = spanOf(m); return t >= a && t <= b; });
    if (n) { openMenu(e, noteMenu(e, n, t)); return; }
  }
  const g = gapAt(x, y);
  if (g) { openMenu(e, boundaryMenu(e, g[0], g[1])); return; }
  openMenu(e, emptyMenu(e));
}

// ---------------------------------------------------------------- トラックビュー
// ガイドはトラックごと（docs/track-view.md §9）: 見出しのガイドのボタン（プルダウン）で「共通のガイド」か、ほかの
// ボーカルのトラックを選ぶ（set_track_guide）。共通のガイドの指定（set_guide_track）も同じプルダウンの最後に置く。

/** プルダウンに出すトラックの名前（プラグインは修飾の名前。DAW のトラック名が違えば右に添える。共通のガイドはその旨も）。 */
function guideChoice(x) {
  const daw = ARA && x.group && x.group !== x.name ? x.group : '';
  return { label: x.name, key: [daw, x.guide ? '共通のガイド' : ''].filter(Boolean).join(' · ') };
}

/** トラックのガイドのプルダウンの項目（共通のガイド・ほかのボーカル・共通のガイドの指定）。 */
export function guideItems(t) {
  const common = S.tracks.find((x) => x.id === S.session?.guide) || null;
  const own = t.guide_id || null;
  const head = !common
    ? { label: 'なし（共通のガイドも未指定）', title: '共通のガイドを決めると、ガイドを選んでいないトラックはそれに合わせる' }
    : common.id === t.id ? { label: 'なし（このトラックが共通のガイド）' }
      : { label: `共通のガイド（${common.name}）`, title: '共通のガイドを変えると、このトラックのガイドも変わる' };
  const others = S.tracks.filter((x) => x.kind === 'vocal' && x.id !== t.id);
  return [
    { id: 'guide-common', radio: true, checked: !own, ...head, run: () => setTrackGuide(t.id, null) },
    SEP,
    ...others.map((x) => ({ id: `guide-to:${x.id}`, radio: true, checked: own === x.id, ...guideChoice(x),
      run: () => setTrackGuide(t.id, x.id) })),
    SEP,
    { id: 'guide', label: t.guide ? '共通のガイドから外す' : 'このトラックを共通のガイドにする',
      title: '共通のガイド: ガイドを選んでいないトラックが合わせるトラック', run: () => setGuide(t.guide ? null : t.id) },
  ];
}

/** 見出しのガイドのボタンの下にプルダウンを出す。anchor: ボタンを返す関数（見出しは描き直すので要素ではなく関数）。 */
export function openGuideMenu(t, anchorFn) {
  const el = anchorFn();
  if (!el) return;
  const b = el.getBoundingClientRect();
  openMenu({ clientX: b.left, clientY: b.bottom + 2 }, guideItems(t), { label: `${t.name} のガイド`, anchor: anchorFn });
}

/** トラック見出し（とクリップの外のレーン）。 */
export function trackItems(t) {
  const vocals = S.tracks.filter((x) => x.kind === 'vocal').length;
  const guide = [
    { id: 'guide-pick', label: 'ガイドを選ぶ…', hidden: t.kind !== 'vocal', run: () => openGuidePicker(t.id) },
    { id: 'guide', label: t.guide ? '共通のガイドから外す' : 'このトラックを共通のガイドにする', hidden: t.kind !== 'vocal',
      run: () => setGuide(t.guide ? null : t.id) },
  ];
  // プラグイン: 名前・位置・種類・外す は DAW が決める。ガイドの指定だけ残す
  if (ARA) return guide;
  return [
    cmd('rename', { trackId: t.id }, { label: '名前を変える', disabled: false, run: () => startRename(t) }),
    ...guide,
    { id: 'zero', label: '元の位置に戻す', disabled: Math.abs(offsetOf(t)) < 1e-9,
      run: () => commitOffset(t.id, offsetOf(t), 0) },
    // 編集中のトラック・最後のボーカルは伴奏にできない
    { id: 'kind', label: t.kind === 'vocal' ? '伴奏として扱う' : 'ボーカルとして扱う',
      disabled: t.kind === 'vocal' && (t.id === S.session?.current || vocals <= 1),
      run: () => setKind(t.id, t.kind === 'vocal' ? 'inst' : 'vocal') },
    SEP,
    { id: 'remove', label: 'トラックを外す', disabled: t.kind === 'vocal' && vocals <= 1,
      run: () => removeTrack(t.id) },
  ];
}

/** クリップ（tl: クリックしたタイムラインの秒）。 */
export function clipItems(t, tl) {
  const vocals = S.tracks.filter((x) => x.kind === 'vocal').length;
  if (ARA) return [{ id: 'show', label: 'ここを下に表示', disabled: t.kind !== 'vocal',
    run: () => selectTrack(t.id, { view: soundRegion(t, tl) }) }];
  return [
    { id: 'show', label: 'ここを下に表示', disabled: t.kind !== 'vocal',
      run: () => selectTrack(t.id, { view: soundRegion(t, tl) }) },
    { id: 'zero', label: '元の位置に戻す', disabled: Math.abs(offsetOf(t)) < 1e-9,
      run: () => commitOffset(t.id, offsetOf(t), 0) },
    SEP,
    { id: 'remove', label: 'トラックを外す', disabled: t.kind === 'vocal' && vocals <= 1,
      run: () => removeTrack(t.id) },
  ];
}

export function openTrackMenu(e, t) { openMenu(e, trackItems(t)); }
export function openClipMenu(e, t, tl) { openMenu(e, clipItems(t, tl)); }
export function openRulerMenu(e) { e.preventDefault(); openMenu(e, rulerMenu(e)); }

/** テスト用: いま出しているメニューの項目。 */
export function menuItems() {
  if (!menu || menu.hidden) return null;
  return items.filter((it) => !it.sep).map((it) => ({
    label: it.label, key: it.key || '', disabled: !!it.disabled, checked: !!it.checked,
    cmd: it.cmd || null, id: it.id || null,
  }));
}
