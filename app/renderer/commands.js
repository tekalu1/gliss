// コマンドの表（issue #17）。**名前・グループ・既定のキー・実行・有効の条件を 1 か所で持つ。**
// 右クリックのメニュー（menus.js）・Electron のメニューバー（appMenu）・キー入力（installKeys）・
// ツールチップ（draw.js の renderToolbar）・キーボードショートカットの設定（keysdlg.js）は、
// すべてこの表から作る。キーの割り当ては keys.js（既定 = ここの keys、設定で上書き）。
//
// run(ctx) の ctx: 右クリックのメニューから呼ぶときの対象（{ x, y }＝root の中の位置、noteId、t＝編集後の秒 など）。
// キーとメニューバーからは ctx 無し（選択・再生位置が対象）。
// スナップ（時間 N・音程 Shift+N）と表示の設定は grid.js（issue #18）。
// ホイールの操作（縦・横のズームとスクロール。issue #27）も WHEEL の表に持ち、キーと同じ設定画面で変える。
import { BLOCK_KINDS, S, histLabel, isSel, spanOf, targets, toSource, totalSec, unwarp } from './state.js';
import { askAi, canAskAi } from './askai.js';
import { render, renderToolbar } from './draw.js';
import {
  clearFades, idle, mergeMany, muteNotes, redo, resetOriginal, snapToSemitone, splitNote, undo, unmuteNotes,
} from './edits.js';
import { G, saveGrid } from './grid.js';
import { openTempoInput } from './tempo.js';
import { beginSelectedAudition, closePop, closeTr, endAudition, openPop, openTr, setTool } from './interact.js';
import { play, previewEnabled, setPreviewEnabled, stop } from './audio.js';
import { ARA, araCompare, araEditorHead, araEditorRegion, araFeatures, araTransport } from './ara.js';
import { status } from './engine.js';
import { startRename } from './tracks.js';
import { guideShown, guideWhy } from './session.js';
import { asrRangeAt, asrReady, asrWhy, defaultAsrTime, transcribeAt } from './asr.js';
import { chooseF0, f0State } from './f0.js';
import {
  accelerator, comboOf, commandFor, keysOf, onKeysChanged, registerDefaults, wheelCombo,
} from './keys.js';

const CUT_MIN_SEC = 0.02;       // 分割: 両端からこれだけ内側（エンジンと同じ）
const TOUCH_SEC = 0.006;        // 結合できる「接している」（エンジンの MERGE_TOL と同じ）

// 画面の外（main.js）が持つもの: メニューバーの処理（ファイルを開くなど）・表示の保存・設定画面
const host = { menu: async () => {}, saveView: () => {}, openKeys: () => {}, zoomReset: () => {}, openAi: () => {}, openUpdates: () => {}, openAddons: () => {} };
export function setHost(h) { Object.assign(host, h); }

const root = () => document.querySelector('#mock');
/** ポップアップを出す位置（キーから開いたとき）: ピアノロールの上の中ほど。 */
function center() {
  const r = root().getBoundingClientRect();
  const sr = document.querySelector('#roll').getBoundingClientRect();
  return { x: sr.left - r.left + sr.width / 2 - 130, y: sr.top - r.top + 40 };
}
const pos = (ctx) => (ctx && ctx.x != null ? { x: ctx.x, y: ctx.y } : center());

/** 選択中の音程ノート（並び順）。 */
export function selectedNotes() {
  return S.pitched.filter((n) => isSel(n.id));
}
/** 選択中の区間（音程ノート・子音・息）。無音にする・戻すは、子音・息にも同じに効く。 */
const selectedBlocks = () => S.blocks.filter((n) => isSel(n.id));
/** 選んだノートが隣どうし接して並んでいる（結合できる）。 */
export function contiguous(ns = selectedNotes()) {
  if (ns.length < 2) return false;
  const idx = new Map(S.pitched.map((n, i) => [n.id, i]));
  for (let k = 1; k < ns.length; k++) {
    const a = ns[k - 1]; const b = ns[k];
    if (idx.get(b.id) !== idx.get(a.id) + 1 || b.kind !== 'note' || a.kind !== 'note') return false;
    if (Math.abs(b.start_sec - a.end_sec) > TOUCH_SEC) return false;
  }
  return true;
}
/** 境目 a｜b が接している（結合できる）。 */
export function touching(a, b) {
  return !!a && !!b && Math.abs(b.start_sec - a.end_sec) <= TOUCH_SEC;
}
/** なだらかさの対象の境目があるか（選択が無ければ全体）。 */
function hasTransitions() {
  const sel = new Set(S.sel);
  return (S.vd?.transitions || []).some((t) => !sel.size || sel.has(t.a) || sel.has(t.b));
}
const hasNotes = () => !!S.vd;
const editable = () => selectedNotes().filter((n) => n.pitch_editable);

/** 分ける（Alt+X / 右クリックの「ここで分ける」）。t: 編集後の秒（省くと再生位置）。 */
function splitAt(ctx) {
  const head = ctx?.t == null ? araEditorHead() : null;
  if (ctx?.t == null && head == null) { status('再生位置がこのトラックのイベントの外にある'); return null; }
  const t = ctx?.t != null ? ctx.t : head - S.off;
  const edited = unwarp(t);
  if (edited == null || (ARA && !araEditorRegion(edited))) {
    status('このトラックのイベントの外では分割できない'); return null;
  }
  const n = ctx?.noteId ? S.byId.get(ctx.noteId)
    : S.blocks.find((x) => {                // 音程ノート・子音・息（エンジンの split はどれも受ける）
      const [a, b] = spanOf(x);
      return t > a && t < b && (!S.sel.length || isSel(x.id));
    });
  if (!n || !BLOCK_KINDS.has(n.kind)) { status('分ける区間が無い（再生位置を選んだ区間の上に置く）'); return null; }
  if (!idle()) { status('前の編集を当てている間は分割しない。当て終わってからもう一度'); return null; }
  const src = toSource(edited);
  if (src < n.start_sec + CUT_MIN_SEC || src > n.end_sec - CUT_MIN_SEC) {
    status('ノートの端に近すぎる（両端から 20 ms 以上内側で分ける）');
    return null;
  }
  return splitNote(n.id, +src.toFixed(4));
}

function mergeSelected() {
  const ns = selectedNotes();
  if (!contiguous(ns)) { status('隣り合って接しているノートだけ結合できる'); return null; }
  if (!idle()) { status('前の編集を当てている間は結合しない。当て終わってからもう一度'); return null; }
  return mergeMany(ns.map((n) => n.id));
}

function toggleGuide() { S.showGuide = !S.showGuide; render(); }
function showAll() {
  if (!S.vd) return;
  S.view = { t0: 0, span: totalSec() };
  render();
  host.saveView();
}
function renameCurrent(ctx) {
  const id = ctx?.trackId || S.session?.current;
  const t = S.tracks.find((x) => x.id === id);
  if (t) startRename(t);
}
/** スナップのオン・オフ（ヘッダーのアイコン・N / Shift+N・メニュー）。ユーザー設定に残す（取り消しの履歴には入れない）。 */
function toggleSnap(which) {
  if (which === 'time') G.snapT = !G.snapT; else G.snapP = !G.snapP;
  const on = which === 'time' ? G.snapT : G.snapP;
  status(`${which === 'time' ? '時間スナップ' : '音程スナップ（半音）'}: ${on ? 'オン（ドラッグ中 Shift で解除）' : 'オフ'}`);
  saveGrid();
  render();
}
/** 再生位置に追従（オートスクロール。ヘッダーのアイコン・F・メニュー。issue #40）。ユーザー設定に残す（取り消しの履歴には入れない）。 */
function toggleFollow() {
  G.follow = !G.follow;
  status(`再生位置に追従: ${G.follow ? 'オン' : 'オフ'}`);
  saveGrid();
  render();
}
const hasFades = () => selectedNotes().some((n) => (n.fade_in_sec || 0) > 0 || (n.fade_out_sec || 0) > 0);

// ---------------------------------------------------------------- 表
// [id, 名前, グループ, 既定のキー, 実行, 有効の条件, チェック]
// プラグイン（ARA）では、プロジェクトの開く・保存・書き出し・トラックの追加・名前（DAW が持つ）・AI・更新・アドオンは使えない。
// キーも割り当てない（DAW に渡す）
const ARA_OFF = new Set(['new-project', 'open-take', 'save', 'save-as', 'add-track', 'export', 'export-as', 'rename',
  'ai-connect', 'check-updates', 'addons']);
const GR = { play: '再生・ツール', edit: '編集', f0: 'ピッチ検出の方式', note: 'ノート', lyrics: '歌詞', view: '表示', track: 'トラック', file: 'ファイル', help: 'ヘルプ' };
export const COMMANDS = [
  // プラグインでは再生は DAW のもの（ホストの再生の制御へ。audio.js の play/stop は使わない）
  ['play', '再生／停止', GR.play, ['Space'], () => (ARA ? araTransport('toggle') : (S.playing ? stop() : play()))],
  ['tool-main', 'メインツール', GR.play, ['1'], () => setTool('main'), null, () => S.tool === 'main'],
  ['tool-draw', '鉛筆', GR.play, ['2'], () => setTool('draw'), null, () => S.tool === 'draw'],
  ['tool-cut', 'はさみ', GR.play, ['3'], () => setTool('cut'), null, () => S.tool === 'cut'],
  ['tool-mute', 'ミュート', GR.play, ['4'], () => setTool('mute'), null, () => S.tool === 'mute'],
  // つかんだノートを鳴らす（Melodyne と同じ。issue #27）。ユーザー設定（取り消しの履歴に入れない）
  ['preview-notes', 'つかんだノートを鳴らす', GR.play, [], () => setPreviewEnabled(!previewEnabled()), null,
    () => previewEnabled()],
  ['audition-selected', '選択ノートを試聴', GR.play, ['P'], (ctx) => beginSelectedAudition({ once: ctx?.source !== 'keyboard' }),
    () => selectedNotes().some((n) => n.kind === 'note')],
  // 原音と比べる（プラグインだけ。Melodyne の比較と同じ）: キャッシュを読まずに原音を返す
  ...(ARA ? [['ara-compare', '原音と比べる', GR.play, [], () => araCompare(!araFeatures().compare), null,
    () => araFeatures().compare]] : []),

  ['undo', '元に戻す', GR.edit, ['Ctrl+Z'], () => { closePop(); closeTr(); return undo(); }],
  ['redo', 'やり直す', GR.edit, ['Ctrl+Shift+Z', 'Ctrl+Y'], () => { closePop(); closeTr(); return redo(); }],
  ['select-all', 'すべて選択', GR.edit, ['Ctrl+A'], () => { S.sel = S.blocks.map((n) => n.id); render(); }, hasNotes],
  ['tempo', 'テンポを入力', GR.edit, [], () => openTempoInput('bpm'), () => S.tracks.length > 0],
  ['keys', 'ショートカット（キー・ホイール）…', GR.edit, ['Ctrl+,'], () => host.openKeys()],
  // ピッチ（F0）検出の方式（ユーザー設定。替えたら開いているトラックを解析し直す。f0.js）。
  // チェックは開いている曲でエンジンが実際に使う方式（前に解析した方式のまま。RMVPE の重みが無ければ Gliss）
  ['f0-rmvpe', 'RMVPE（既定）', GR.f0, [], () => chooseF0('rmvpe'), () => f0State().rmvpe,
    () => f0State().effective === 'rmvpe'],
  ['f0-gliss', 'Gliss', GR.f0, [], () => chooseF0('gliss'), null, () => f0State().effective === 'gliss'],
  ['f0-praat', 'Praat', GR.f0, [], () => chooseF0('praat'), null, () => f0State().effective === 'praat'],

  ['guide-match', 'ガイドに合わせる…', GR.note, ['G'], (ctx) => { const p = pos(ctx); openPop(p.x, p.y); }, guideShown],
  ['semitone', '半音に合わせる', GR.note, ['Q'], () => snapToSemitone(editable().map((n) => n.id)), () => editable().length > 0],
  ['split', 'ここで分ける', GR.note, ['Alt+X'], splitAt, (ctx) => (ctx?.noteId ? true : hasNotes())],
  ['merge', '結合', GR.note, ['Ctrl+J'], mergeSelected, () => contiguous()],
  ['transition', 'なだらかさ…', GR.note, ['T'], (ctx) => { const p = pos(ctx); openTr(p.x, p.y, ctx?.pair ? { pair: ctx.pair } : {}); }, hasTransitions],
  ['reset-original', 'オリジナルに戻す', GR.note, [], () => resetOriginal(targets()), hasNotes],
  ['mute', '無音にする', GR.note, ['Delete'], () => muteNotes(selectedBlocks().filter((n) => !n.muted).map((n) => n.id)),
    () => selectedBlocks().some((n) => !n.muted)],
  ['unmute', '無音を戻す', GR.note, [], () => unmuteNotes(selectedBlocks().filter((n) => n.muted).map((n) => n.id)),
    () => selectedBlocks().some((n) => n.muted)],
  ['clear-fade', 'フェードを消す', GR.note, [], () => clearFades(selectedNotes()
    .filter((n) => (n.fade_in_sec || 0) > 0 || (n.fade_out_sec || 0) > 0).map((n) => n.id)), hasFades],
  // 選んだノート・範囲を AI に頼む文をクリップボードへ（askai.js）
  ['ask-ai', 'AI に頼む', GR.note, [], askAi, canAskAi],

  // 聞き取り（区間の音声認識。issue #54）: 右クリックした区間（ctx.t＝編集前の秒）、キーからは選んだノート・再生位置の区間。
  // 結果は候補で、確定の歌詞は変えない（Enter で採用・ダブルクリックで直す・Esc で取り消し。asr.js）
  ['transcribe', '聞き取る', GR.lyrics, [], (ctx) => transcribeAt(ctx?.t ?? defaultAsrTime()),
    (ctx) => hasNotes() && asrReady() && !!asrRangeAt(ctx?.t ?? defaultAsrTime())],

  ['snap-time', '時間スナップ', GR.view, ['N'], () => toggleSnap('time'), null, () => G.snapT],
  ['snap-pitch', '音程スナップ（半音）', GR.view, ['Shift+N'], () => toggleSnap('pitch'), null, () => G.snapP],
  // 再生位置に追従（Studio One のオートスクロールと同じ F。issue #40）
  ['follow', '再生位置に追従', GR.view, ['F'], toggleFollow, null, () => G.follow],
  ['guide-view', 'ガイドを重ねて表示', GR.view, [], toggleGuide, guideShown, () => S.showGuide],
  ['phoneme-bounds', '音素境界を全高に表示', GR.view, [], () => { S.showAllBounds = !S.showAllBounds; render(); },
    null, () => S.showAllBounds],
  ['show-all', '全体を表示', GR.view, [], showAll, hasNotes],
  ['zoom-reset', 'ズームを戻す', GR.view, [], () => host.zoomReset(), () => hasNotes() || S.tracks.length > 0],

  ['rename', 'トラックの名前を変える', GR.track, ['F2'], renameCurrent, () => S.tracks.length > 0],

  // プロジェクトのファイル（issue #33。DAW と同じ）。id の open-take は前の「テイクを開く…」のまま（キーの設定を引き継ぐ）
  ['new-project', '新規プロジェクト', GR.file, ['Ctrl+N']],
  ['open-take', '開く…', GR.file, ['Ctrl+O']],
  ['save', '保存', GR.file, ['Ctrl+S'], null, () => !!S.doc],
  ['save-as', '名前を付けて保存…', GR.file, ['Ctrl+Shift+S'], null, () => !!S.doc],
  ['open-guide', 'ガイドを開く…', GR.file, []],
  ['add-track', 'トラックを追加…', GR.file, ['Ctrl+Shift+O']],
  ['load-lyrics', '歌詞を読み込む（テキスト）…', GR.file, []],
  ['import-lyrics', '歌詞を読み込む（SVP / MIDI）…', GR.file, [], null, () => !!S.take],
  ['export', '書き出し', GR.file, ['Ctrl+E']],
  ['export-as', '書き出し先を選んで書き出し…', GR.file, ['Ctrl+Shift+E']],

  // AI とつなぐ（Claude Code / Claude Desktop に登録・AI に許可。aidlg.js）
  ['ai-connect', 'AI とつなぐ…', GR.help, [], () => host.openAi()],
  // 自動更新（updates.js）: 開いて、すぐに確かめる
  ['check-updates', '更新を確認…', GR.help, [], () => host.openUpdates({ check: true })],
  // 解析モデルと任意機能のアドオン（漢字の歌詞の読み。addons.js）。初回画面と同じ行をダイアログで出す
  ['addons', 'モデルと追加の機能…', GR.help, [], () => host.openAddons()],
].map(([id, label, group, keys, run, enabled, checked]) => ({
  id, label, group, keys: ARA && ARA_OFF.has(id) ? [] : keys,
  // ファイルの操作はダイアログ・ファイルを開くので main.js（メニューバーと同じ処理）に任せる
  run: run || ((ctx) => host.menu({ cmd: id, arg: ctx })),
  enabled: ARA && ARA_OFF.has(id) ? () => false
    : ARA && id === 'open-guide' ? () => araFeatures().fileGuide      // ファイルのガイドは C++ が対応したときだけ
      : (enabled || (() => true)),
  checked: checked || null,
}));

// ---------------------------------------------------------------- ホイール（issue #27）
// 既定は Studio One に合わせる: ホイール = 縦スクロール、Shift = 横スクロール、Ctrl = 縦ズーム、
// Ctrl+Shift = 横ズーム。ズームはポインタの位置が中心。タッチパッドの横の量（deltaX）は割り当てによらず横スクロール。
// 上のトラックビューでは、縦ズーム = トラックの高さ・縦スクロール = トラックのスクロール、横のズーム・スクロールは
// 上の時間の表示範囲（下のエディターとは別。issue #39）。ポインタのある側に効く。
export const WHEEL_GROUP = 'ホイール';
export const WHEEL = [
  ['wheel-zoom-v', '縦ズーム（音程の幅・トラックの高さ）', ['Ctrl+Wheel']],
  ['wheel-zoom-h', '横ズーム（時間の幅）', ['Ctrl+Shift+Wheel']],
  ['wheel-scroll-v', '縦スクロール（音程・トラック）', ['Wheel']],
  ['wheel-scroll-h', '横スクロール（時間）', ['Shift+Wheel']],
].map(([id, label, keys]) => ({ id, label, group: WHEEL_GROUP, keys, wheel: true }));
registerDefaults(WHEEL);

/** ホイールの操作（イベント）→ 割り当てた動き（'zoom-v' / 'zoom-h' / 'scroll-v' / 'scroll-h'、無ければ null）。 */
export function wheelAction(e) {
  const combo = wheelCombo(e);
  for (const w of WHEEL) if (keysOf(w.id).includes(combo)) return w.id.slice(6);
  return null;
}

export const GROUPS = Object.values(GR);
const byId = new Map(COMMANDS.map((c) => [c.id, c]));
registerDefaults(COMMANDS);

export function command(id) { return byId.get(id) || null; }
export function isEnabled(id, ctx) {
  const c = byId.get(id);
  try { return !!c && !!c.enabled(ctx); } catch { return false; }
}
/** コマンドを実行する（無効なら何もしない）。返り値は実行の Promise（あれば）。 */
export function runCommand(id, ctx) {
  const c = byId.get(id);
  if (!c || !isEnabled(id, ctx)) return undefined;
  return c.run(ctx);
}

// ---------------------------------------------------------------- キー入力
const TEXT_INPUT = /^(text|search|number|email|password|url|tel)$/i;
function typing(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  return el.tagName === 'INPUT' && TEXT_INPUT.test(el.type || 'text');
}

const NEEDS = {
  semitone: 'ノートを選んでから', mute: 'ノートを選んでから（もう無音のノートは除く）',
  unmute: '無音のノートを選んでから',
  merge: '隣り合って接しているノートを 2 つ以上選んでから', 'guide-match': guideWhy,
  'guide-view': guideWhy, transition: '接続された境目が無い', rename: 'トラックが無い',
  'clear-fade': 'フェードのあるノートを選んでから', tempo: 'トラックが無い',
  save: 'プロジェクトが無い', 'save-as': 'プロジェクトが無い',
  split: '分ける区間が無い（再生位置を選んだ区間の上に置く）', 'select-all': 'ノートが無い',
  'show-all': 'ノートが無い', 'reset-original': 'ノートが無い', 'ask-ai': 'ノートを選ぶか、ループの範囲を決めてから',
  'audition-selected': 'ノートを選んでから',
  transcribe: () => (asrReady() ? '発声のある所で（ノートを選ぶか、再生位置を発声の上に置く）' : asrWhy()),
};
/** コマンドが使えない理由（ガイドは重ならない理由をその場で。issue #32）。 */
export function needs(id) {
  const n = NEEDS[id];
  return (typeof n === 'function' ? n() : n) || '対象が無い';
}
let keysOpen = () => false;
/** キー入力をコマンドへ（document の keydown）。文字を入力している欄・設定画面の中では効かない。 */
export function installKeys({ dialogOpen } = {}) {
  keysOpen = dialogOpen || keysOpen;
  document.addEventListener('keyup', (e) => {
    if (commandFor(comboOf(e)) === 'audition-selected' || e.key.toLowerCase() === 'p') endAudition();
  });
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || keysOpen() || typing(e.target)) return;
    const combo = comboOf(e);
    if (!combo) return;
    const id = commandFor(combo);
    if (!id) return;
    // ボタンにフォーカスがあるときの Space / Enter はボタンを押す（ブラウザのまま）
    if (e.target?.tagName === 'BUTTON' && (combo === 'Space' || combo === 'Enter')) return;
    e.preventDefault();
    if (e.altKey) window.api?.consumeAlt?.();   // Alt+X などの後に Alt を離してもメニューバーへ行かない
    if (!isEnabled(id)) {                       // 黙って何もしないのではなく、使えない理由を出す
      const c = command(id);
      status(`${c.label}: 今は使えない（${needs(id)}）`);
      return;
    }
    const r = runCommand(id, id === 'audition-selected' ? { source: 'keyboard' } : undefined);
    if (r && typeof r.catch === 'function') r.catch((err) => status(`${command(id).label} に失敗: ${err.message}`));
  });
  onKeysChanged(() => { renderToolbar(); syncAppMenu(); });
}

// ---------------------------------------------------------------- メニューバー
// 並びだけをここに持ち、名前・キーはコマンドの表から作って main に送る（main が Electron の Menu に直し、
// その写しをタイトルバーのメニューバー（titlebar.js）が描く）。
const SEP = { sep: true };
const MENUBAR = [
  ['ファイル', ['new-project', 'open-take', { recent: true, label: '最近使ったプロジェクト' }, SEP, 'save', 'save-as', SEP,
    'add-track', 'open-guide', 'load-lyrics', 'import-lyrics', SEP, 'export', 'export-as', SEP, { role: 'quit', label: '終了' }]],
  ['編集', ['undo', 'redo', SEP, 'select-all', 'tempo', SEP, 'preview-notes', 'audition-selected',
    { label: 'ピッチ検出の方式', submenu: ['f0-rmvpe', 'f0-gliss', 'f0-praat'] }, 'keys']],
  ['ノート', ['guide-match', 'semitone', 'split', 'merge', 'transition', SEP, 'clear-fade', 'reset-original', 'mute', 'unmute',
    SEP, 'ask-ai']],
  ['表示', ['guide-view', 'phoneme-bounds', SEP, 'follow', 'snap-time', 'snap-pitch', SEP, 'show-all', 'zoom-reset']],
  // 名前・版・アイコン（main の app.setAboutPanelOptions。issue #29）
  ['ヘルプ', ['ai-connect', 'addons', SEP, 'check-updates', { role: 'about', label: 'Gliss について' }]],
];

// プラグインの並び: ファイルは「ガイドを開く…（C++ が対応したとき）・歌詞」だけ、ヘルプはキーボードショートカットだけ
const MENUBAR_ARA = [
  ['ファイル', ['open-guide', 'load-lyrics', 'import-lyrics']],
  ['編集', ['undo', 'redo', SEP, 'select-all', 'tempo', SEP, 'preview-notes', 'audition-selected',
    { label: 'ピッチ検出の方式', submenu: ['f0-rmvpe', 'f0-gliss', 'f0-praat'] }]],
  MENUBAR[2],
  ['表示', ['ara-compare', SEP, 'guide-view', 'phoneme-bounds', SEP, 'follow', 'snap-time', 'snap-pitch', SEP, 'show-all', 'zoom-reset']],
  ['ヘルプ', ['keys']],
];

/** 元に戻す／やり直すの名前（「元に戻す: ノートの長さ」。AI の操作は「元に戻す: AI · ピッチ」）。
 * ツールバーのツールチップ（draw.js）も同じもの。まだ当たっていない操作は画面の操作（人）。 */
export function undoLabels() {
  const h = S.hist || {};
  const pend = S.pending.filter((it) => !it.started && !it.canceled);
  const edge = S.edgeDraft?.drag;
  const cancellableEdge = edge && !edge.applying && (edge.moved || S.pendingPlan);
  const u = cancellableEdge ? 'ノートの長さ' : pend.length ? pend[pend.length - 1].label : histLabel(h.undo);
  const rd = cancellableEdge || pend.length ? null : histLabel(h.redo);
  return { u, rd };
}

export function appMenuTemplate() {
  const { u, rd } = undoLabels();
  const item = (it) => {
    if (it.submenu) return { label: it.label, submenu: it.submenu.map(item) };
    if (typeof it !== 'string') return it;
    const c = byId.get(it);
    let l = c.label;
    // 有効の条件は右クリックのメニュー・キーと同じ（選択が変わると描き直しで送り直す）
    let en = isEnabled(it);
    if (it === 'undo') { l = u ? `元に戻す: ${u}` : '元に戻す'; en = !!u; }
    if (it === 'redo') { l = rd ? `やり直す: ${rd}` : 'やり直す'; en = !!rd; }
    if (it === 'f0-rmvpe' && !en) l = 'RMVPE（既定・モデル未取得）';     // 重みが無い: チェックは代わりに使う Gliss に付く
    const k = keysOf(it)[0];
    return { cmd: it, label: l, accelerator: accelerator(k), enabled: en,
      ...(c.checked ? { checked: !!c.checked() } : {}) };
  };
  const bar = ARA ? MENUBAR_ARA : MENUBAR;
  return bar.map(([label, items]) => ({ label, submenu: items.filter((it) => !(ARA && it === 'open-guide' && !araFeatures().fileGuide)).map(item) }));
}

let lastMenu = '';
let menuTimer = 0;
let pointerDown = false;
/** メニューバーを作り直す（変わったときだけ main に送る）。
 * 選択が変わると有効／無効が変わるので、範囲選択のドラッグ中は毎回変わりうる。Electron のメニューの
 * 作り直しは重く、ドラッグの最中にしない: ボタンを押している間は待ち、離してから少し置いて 1 回だけ送る。 */
export function syncAppMenu() {
  clearTimeout(menuTimer);
  if (pointerDown) return;                  // 離したとき（pointerup）にもう一度呼ぶ
  menuTimer = setTimeout(sendAppMenu, 60);
}
/** 今すぐ送る（タイトルバーのメニューを開く直前。ボタンを押している最中でも送る）。 */
export function flushAppMenu() {
  clearTimeout(menuTimer);
  sendAppMenu(true);
}
function sendAppMenu(force = false) {
  if (pointerDown && !force) return;
  const tpl = appMenuTemplate();
  const key = JSON.stringify(tpl);
  if (key === lastMenu) return;
  lastMenu = key;
  window.api?.setAppMenu?.(tpl);
}
document.addEventListener('pointerdown', () => { pointerDown = true; clearTimeout(menuTimer); }, true);
for (const ev of ['pointerup', 'pointercancel']) {
  document.addEventListener(ev, () => { pointerDown = false; syncAppMenu(); }, true);
}
window.addEventListener('blur', () => { if (pointerDown) { pointerDown = false; syncAppMenu(); } });
