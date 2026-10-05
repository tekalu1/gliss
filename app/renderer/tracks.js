// トラックビュー（上）。モック `proposal/track-view.html` の設計のとおり（issue #7。`docs/track-view.md` §3）。
//
//  - トラックの見出しは 1 段目が 名前・ガイド指定のアイコン・M・S、2 段目が音量のスライダー・パンのノブ（高さ 40 px 未満は
//    2 段目を畳む）。音量・パンは M／S と同じ聴き比べの操作（再生だけに効く・取り消しの対象外・session に保存）。
//    見出しとレーンの境目をドラッグして見出しの幅を変える（140〜360 px。state.json の view に保存）。
//    裏の準備（issue #63）がまだの間だけ、名前の右に小さな印（準備中の輪・待ちの点線の輪・失敗の !）を足す。
//  - クリップ（音声ファイル 1 本）をクリック → そのトラックを編集対象にして、クリックした所の歌っている
//    かたまり（無ければクリップ全体）を下に出す。レーンを横にドラッグ → その範囲に下がズーム（ドラッグ中も追従）。
//    トラック名のクリック → 表示範囲はそのまま、編集対象だけ切り替え。伴奏はクリックしても再生位置が動くだけ。
//  - 下の表示範囲は、上の編集中のトラックに白枠で出す。再生位置とループは上下で共通（どちらのルーラーでも
//    クリックで再生位置、ドラッグでループ）。
//  - 上下の境界はドラッグで高さを変え、ダブルクリックで上を 1 トラック分に畳む／戻す。既定は全トラックが入る
//    高さ（画面の 40% まで）。
//  - 色は足さない: 編集中のトラックの波形は黄（テイク）、ガイドはグレー（エディターのガイドと同じ）、他は暗い灰。
//    聞こえないトラック（ミュート／ソロ）は波形を暗く。
//  - **クリップの下半分を横にドラッグ → 音源全体の位置をずらす**（非破壊。`set_track(offset_sec)`。§4）。
//    上半分は範囲のドラッグ（Studio One／Fender Studio Pro のスマートツールと同じ分け方: 上半分 = 範囲、
//    下半分 = 矢印）。動かさずに離せば、どちらの半分でもクリック。
//    Shift で細かく（1/10）、元の位置（0）の 6 px・30 ms 以内に吸い付く（Shift・Alt の間は吸い付かない）。Ctrl+Z で戻す。
//  - **ツールで上の動きが変わる**（承認済み 2026-10-03。docs/track-view.md §8）: はさみ = クリップをクリックで切る・切れ目の
//    ダブルクリックでつなぐ（ホバーで縦線・時間スナップ、Shift で外す）、ミュート = 部分のクリックで消す⇔戻す（なぞってまとめて）、
//    鉛筆 = メインと同じ（カーソルも矢印）、メイン = 今のまま。切れ目と消した部分はトラックの `cuts` / `mutes`（トラックの頭が
//    0 の秒。エンジンの split_track / join_track / mute_track_range）。消した部分は再生で鳴らさず（audio.js）、書き出しにも効く。
//  - トラックの操作（追加・外す・位置・名前・種類・ガイドの指定）はエンジンの曲の取り消しの履歴に入る（issue #16）。
//    Ctrl+Z で別のトラックの操作を戻すと、エンジンがそのトラックを編集対象にする（setHistoryHandler で画面に反映）。
//  - トラックの追加（ファイル > トラックを追加… / ウィンドウへのドロップ。main.js）と、見出し・クリップ・ルーラーの
//    右クリックのメニュー（menus.js。issue #17）から 名前を変える（F2）・ガイド・元の位置に戻す・伴奏／ボーカル・外す。
//  - **見出しを上下にドラッグ → トラックの並び順**（DAW と同じ。issue #38）。ドラッグ中から行が入れ替わって見え、
//    離したら `set_track(index)`（取り消しの履歴に入る）。当たるまで見かけの並び（pendingOrder）を残す。
//  - **プラグイン（ARA。ara.js）**: 位置・名前・種類・外すは DAW が決める（クリップの下半分のドラッグ・メニューは無し）。トラック
//    1 行 = AudioModification。ソースの波形は薄く全体に出し、鳴る範囲（DAW のリージョン）だけ枠と普通の明るさで描く。
//    ルーラーのクリック・ドラッグは DAW の再生位置・ループ（ホストの再生の制御）へも送る。見出しの M・S・音量・パンは出さず、
//    はさみ・ミュートのツールはトラックビューでは働かない（クリップの分割・部分のミュートは DAW のリージョンの仕事）。
//  - **表示範囲はエディターと別**（issue #39）: 横ズーム（既定 Ctrl+Shift+ホイール）・横スクロール（Shift+ホイール）で
//    上だけ動く。既定（ズームしていない間）は全体表示で、曲の長さに合わせて広がる。表示 > ズームを戻すで全体表示に。
import { $, analyzeTake, call, onAbandon, status } from './engine.js';
import { beginBusy, laterBusy } from './busy.js';
import {
  COLORS, LAYOUT, S, audible, buttonReleased, clamp, clearProject, currentTrack, fmtTime, isMuted, offsetOf, setPlan,
  spanOf, timelineRange, totalSec,
} from './state.js';
import { onPlayhead, onRender, render, renderToolbar } from './draw.js';
import { adoptSession, guideSuffix, guideWhy, onSession, phonemeSuffix, setMix, setTrack } from './session.js';
import { enqueue, handleEngineError, refresh, setHistoryHandler, wake } from './edits.js';
import { refreshF0 } from './f0.js';
import { dropBuffers, play, setGains, stop } from './audio.js';
import { CUT_MIN_EDGE, covered, joinAt, normCuts, paintPiece, pieces } from './clipedit.js';
import { closeMenu, openClipMenu, openRulerMenu, openTrackMenu } from './menus.js';
import { wheelAction } from './commands.js';
import { G, currentDiv, snapStep, snapTime, tempo, ticks, timeSnapOn } from './grid.js';
import { ARA, araCacheOf, araExtent, araLoop, araLoopHold, araRegions, araScale, araSeek, araSig, araToRep } from './ara.js';
import {
  GAIN_MAX_DB, GAIN_MIN_DB, dbToPos, fmtDb, fmtPan, gainOf, knobSvg, panFromUi, panOf, panSpeech, panUi, posToDb,
} from './mixer.js';

// トラックの高さ（全トラック共通）。縦ズーム（既定 Ctrl+ホイール。issue #27）で 28〜96 px（v3 §9）
export const TRACK_H = { MIN: 28, MAX: 96, DEF: 44 };
let TH = TRACK_H.DEF;
// 見出しの幅（全体で 1 つ。曲ごとではなく表示の設定）。上限はトラックビューの幅の 40% まで
export const HEAD_W = { MIN: 140, MAX: 360, DEF: 160, NARROW: 150, RATIO: 0.4 };
let HW = HEAD_W.DEF;
const SHORT_H = 40;         // これ未満の高さでは 2 段目（音量・パン）を畳む
const MIX_DBL_MS = 400;     // 音量・パンの 2 回押し（既定値に戻す）の間隔
const RH = 20;              // ルーラーの高さ
const CLIP_T = 4;           // クリップの上端（行の中）
const clipH = () => TH - 7;
const { SCALE_H, LANE_H } = LAYOUT;
const { TAKE, GUIDE, SEL, INST, VOCAL } = COLORS;
const ICON_MUTE = '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>';
const ICON_GUIDE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 4 9 5-9 5-9-5z"/><path d="m3 14 9 5 9-5"/></svg>';
const SNAP_PX = 6;          // 元の位置（0）に吸い付く距離
const SNAP_MAX_SEC = 0.03;  // ただしこれより大きくは吸い付かない（曲全体の表示では 6 px が 1 秒近くになる）
const MIN_VIEW = 0.15;      // 下の表示範囲の最小（draw.js の zoom と同じ）
const f1 = (v) => (Math.round(v * 10) / 10).toString();
const baseName = (p) => String(p).split(/[\\/]/).pop();
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let root = null;
let tv = null;
let heads = null;
let lanes = null;
let ruler = null;
let split = null;
let tvBody = null;
let hsz = null;
let bub = null;
let saveView = () => {};
let onNewTake = () => false;  // 新しく足したテイクを編集対象にしたとき（最初の発声に寄せる。main.js）
let laneW = 1000;
let range = [0, 1];         // 上のタイムラインの表示範囲（秒）
let autoRange = [0, 1];     // 全体表示のときの範囲（タイムライン＋後ろに 4% の余白。曲の長さに合わせて広がる）
let rangeKey = '';
let dr = null;              // 上でのドラッグ
let userH = null;           // 境界をドラッグした高さ（null = 自動）
let collapsed = false;
let lastSig = '';
let lastHeads = '';
let renaming = null;        // 名前を入力しているトラックの id（右クリックの「名前を変える」）
let tvView = null;          // 上の表示範囲 { t0, span }（秒。issue #39）。null = 全体表示（自動）
let tvDir = null;           // tvView を決めたプロジェクト（別のプロジェクトを開いたら全体表示に戻す）
let pendingOrder = null;    // 見出しのドラッグで決めた並び（id の配列。当たるまでこの並びで描く。issue #38）
let hd = null;              // 見出しのドラッグ（並び替え）
let mixDrag = null;         // 音量・パンのドラッグ { id, kind }（見出しは描き直すので、window で追う）
let lastMix = { key: '', t: 0, x: 0, y: 0 };
let tvHover = null;         // はさみ・ミュートのホバー: { id, t: 切る位置（トラックの秒）, cut: 近い切れ目, piece: [a, b] }
let lastHit = null;         // 最後のポインタの当たり（ツールを替えたときにカーソルを付け直す）
let tvTool = 'main';        // 描いたときのツール（変わったらホバーを捨てる）
let lastCut = { id: null, t: 0, x: 0 };   // はさみ: 前のクリック（切れ目のダブルクリックでつなぐ）
let suppressClick = false;  // 並び替えのドラッグの後の click は名前のクリック（編集対象の切り替え）にしない
const overviews = new Map(); // トラック id → JSON メタと Int8 波形（セッションが変わったら捨てる）
const waveCache = new Map();
const ovLoading = new Set();
let ovDir = null;

/** ツールごとの説明（ステータス行。下のピアノロールと上のトラックビューの両方）。 */
export const TOOL_STATUS = {
  main: 'メインツール: 下はノートを選ぶ・動かす。上はクリックで下に出す（上半分 = 範囲・下半分 = 位置）',
  draw: '鉛筆: 下はピッチを描く。上は描くものが無いので、メインと同じに働く',
  cut: 'はさみ: 下はノートを分ける（境目をダブルクリックで結合）。上はクリップをクリックで分ける・切れ目をダブルクリックでつなぐ（Shift でグリッドに寄せない）',
  mute: 'ミュート: 下はノートを無音にする／戻す。上は部分をクリックで消す／戻す（横になぞるとまとめて）',
};
export function toolHint(tool) { return TOOL_STATUS[tool] || ''; }

/** 上に描く並び（並び替えのドラッグ中・確定待ちはその並び）。行の番号はこの並びの番号。 */
export function rows() {
  if (!pendingOrder) return S.tracks;
  const by = new Map(S.tracks.map((t) => [t.id, t]));
  const out = pendingOrder.map((id) => by.get(id)).filter(Boolean);
  for (const t of S.tracks) if (!pendingOrder.includes(t.id)) out.push(t);
  return out;
}

export function tvX(t) { return (t - range[0]) / (range[1] - range[0]) * laneW; }
export function tvT(x) { return range[0] + x / laneW * (range[1] - range[0]); }
const pps = () => laneW / (range[1] - range[0]);

// ---------------------------------------------------------------- 高さ（上下の比率）
function avail() {
  const tb = root.querySelector('.tb')?.getBoundingClientRect().height || 36;
  const st = $('#status')?.getBoundingClientRect().height || 18;
  return Math.max(root.getBoundingClientRect().height, 480) - tb - st - split.getBoundingClientRect().height;
}
function fitH() { return RH + S.tracks.length * TH + 1; }
function minH() { return RH + TH; }
function maxH() { return Math.max(minH(), avail() - SCALE_H - LANE_H - 120); }

/** 上の高さを決める（自動 = 全トラックが入る高さ、画面の 40% まで）。 */
export function layout() {
  if (!tv) return;
  const show = S.tracks.length > 0;
  tv.hidden = !show;
  split.hidden = !show;
  if (!show) return;
  let h = collapsed ? minH() : (userH ?? Math.min(fitH(), avail() * 0.4));
  h = clamp(Math.round(h), minH(), maxH());
  if (tv.style.height !== `${h}px`) tv.style.height = `${h}px`;
}
export function tvHeight() { return tv?.getBoundingClientRect().height || 0; }

// ---------------------------------------------------------------- 全体の波形
async function ensureOverviews() {
  const dir = S.session?.dir || null;
  if (dir !== ovDir) {                // 別のセッション: id が同じでも別のトラック
    overviews.clear();
    waveCache.clear();
    ovLoading.clear();
    dropBuffers();
    ovDir = dir;
  }
  const want = S.tracks.filter((t) => !overviews.has(t.id) && !ovLoading.has(t.id)).map((t) => t.id);
  if (!want.length) return;
  for (const id of want) ovLoading.add(id);
  try {
    const r = await call('track_overview', { track_ids: want });
    if ((S.session?.dir || null) !== dir) return;
    for (const x of r.tracks) {
      if (x.path) {
        const meta = await window.api.readJson(x.path);
        if (meta.version !== 2) throw new Error(`未対応の波形形式: ${meta.version}`);
        meta.data = new Int8Array(await window.api.readFile(x.binary_path));
        overviews.set(x.id, meta);
      }
    }
    renderTracks();
  } catch (err) {
    status(`トラックの波形を読めなかった: ${err.message}`);
  } finally {
    for (const id of want) ovLoading.delete(id);
  }
}

/** 1 行ぶんの波形の path（行の左上 = クリップの頭が原点）。 */
function wavePaths(t, shift = 0, scale = 1) {
  const ov = overviews.get(t.id);
  if (!ov) return [];
  const k = pps();
  // scale: 描くときに横へ掛ける倍率（プラグインで DAW が伸縮したリージョン）。見える範囲は掛ける前の座標で切る
  const x0 = tvX(offsetOf(t) + shift);
  const start = Math.max(0, Math.floor(-x0 / scale));
  const end = Math.min(Math.ceil((t.duration_sec || 0) * k), Math.ceil((laneW - x0) / scale));
  if (end < start) return [];
  const key = `${t.id}|${k.toFixed(5)}|${t.kind}|${TH}|${start}|${end}`;
  if (waveCache.has(key)) return waveCache.get(key);
  const spp = ov.sr / k;
  const level = [...ov.levels].reverse().find((v) => v.hop <= spp) || ov.levels[0];
  const fine = spp < level.hop;
  const chans = t.kind === 'inst' && ov.channels >= 2 ? [0, 1] : [null];
  const out = [];
  chans.forEach((ch, ci) => {
    const n = chans.length;
    const yc = CLIP_T + clipH() * (n === 1 ? 0.5 : (ci === 0 ? 0.27 : 0.73));
    const A = n === 1 ? (clipH() - 2) / 2 : clipH() * 0.2;
    const top = []; const bot = [];
    const add = (x, lo, hi) => {
      const silent = lo === 0 && hi === 0;
      top.push(`${f1(x)},${f1(yc - (silent ? 0.5 : hi / 127 * A))}`);
      bot.push(`${f1(x)},${f1(yc + (silent ? 0.5 : -lo / 127 * A))}`);
    };
    const first = ch === null ? 0 : ch;
    const last = ch === null ? ov.channels : ch + 1;
    if (fine) {
      // At close zoom, bins are wider than a pixel. Join their centres to avoid steps.
      const a = Math.max(0, Math.floor(start * spp / level.hop) - 1);
      const b = Math.min(level.length, Math.ceil(end * spp / level.hop) + 1);
      for (let i = a; i < b; i++) {
        const x = Math.min((i + 0.5) * level.hop / spp, (t.duration_sec || 0) * k);
        let lo = 127; let hi = -127;
        const base = level.offset + i * ov.channels * 2;
        for (let c = first; c < last; c++) {
          lo = Math.min(lo, ov.data[base + c * 2]);
          hi = Math.max(hi, ov.data[base + c * 2 + 1]);
        }
        add(x, lo, hi);
      }
    } else {
      for (let x = start; x <= end; x++) {
        const a = Math.min(level.length - 1, Math.floor(x * spp / level.hop));
        const b = Math.min(level.length, Math.max(a + 1, Math.ceil((x + 1) * spp / level.hop)));
        let lo = 127; let hi = -127;
        for (let i = a; i < b; i++) {
          const base = level.offset + i * ov.channels * 2;
          for (let c = first; c < last; c++) {
            lo = Math.min(lo, ov.data[base + c * 2]);
            hi = Math.max(hi, ov.data[base + c * 2 + 1]);
          }
        }
        add(x, lo, hi);
      }
    }
    if (top.length) out.push(`M${top.join('L')}L${bot.reverse().join('L')}Z`);
  });
  waveCache.set(key, out);
  if (waveCache.size > 64) waveCache.delete(waveCache.keys().next().value);
  return out;
}

/** クリックした時刻（タイムラインの秒）のまわりの、歌っているかたまり（無ければクリップ全体）。 */
export function soundRegion(t, tl) {
  const off = offsetOf(t);
  const dur = t.duration_sec || 0;
  const whole = [off, off + dur];
  const ov = overviews.get(t.id);
  if (!ov) return whole;
  const level = ov.levels.reduce((best, v) => Math.abs(v.hop / ov.sr - 0.02) < Math.abs(best.hop / ov.sr - 0.02) ? v : best);
  const bin = 0.02;
  const n = Math.ceil(ov.duration_sec / bin);
  const env = new Array(n);
  let mx = 0;
  for (let i = 0; i < n; i++) {
    let a = 0;
    const first = Math.floor(i * bin * ov.sr / level.hop);
    const last = Math.min(level.length, Math.ceil((i + 1) * bin * ov.sr / level.hop));
    for (let j = first; j < last; j++) {
      const base = level.offset + j * ov.channels * 2;
      for (let c = 0; c < ov.channels; c++) {
        a = Math.max(a, Math.abs(ov.data[base + c * 2]), Math.abs(ov.data[base + c * 2 + 1]));
      }
    }
    a /= 127;
    env[i] = a;
    mx = Math.max(mx, a);
  }
  const thr = Math.max(0.004, mx * 0.06);          // いちばん大きい音の −24 dB
  const runs = [];
  let s0 = -1;
  for (let i = 0; i <= n; i++) {
    const on = i < n && env[i] >= thr;
    if (on && s0 < 0) s0 = i;
    if (!on && s0 >= 0) {
      const a = s0 * bin; const b = i * bin;
      const last = runs[runs.length - 1];
      if (last && a - last[1] < 1.0) last[1] = b;  // 1 秒より短い切れ目はつなぐ
      else runs.push([a, b]);
      s0 = -1;
    }
  }
  const loc = tl - off;
  let best = null; let bd = Infinity;
  for (const [a, b] of runs) {
    if (b - a < 0.08) continue;
    const d = loc < a ? a - loc : loc > b ? loc - b : 0;
    if (d < bd) { bd = d; best = [a, b]; }
  }
  if (!best || bd > 1.0) return whole;
  return [off + Math.max(0, best[0] - 0.3), off + Math.min(dur, best[1] + 0.3)];
}

// ---------------------------------------------------------------- 描画
const TV_GRID_PX = 40;          // トラックビューの目盛り・自動のスナップの刻みの間隔の下限（エディターより粗く）
/** トラックビューの時間スナップの刻み（秒）。 */
export function tvStep() { return snapStep(pps(), TV_GRID_PX); }

/** 下で表示している範囲（タイムラインの秒）。 */
export function viewRange() {
  const cur = currentTrack();
  const off = cur ? offsetOf(cur) : S.off;
  return [off + S.view.t0, off + S.view.t0 + S.view.span];
}

function headsHtml() {
  return rows().map((t, i) => {
    const cur = t.id === S.session?.current;
    const cls = `th ${t.kind}${cur ? ' cur' : ''}${audible(t) ? '' : ' off'}`;
    const nm = esc(t.name);
    const g = t.kind === 'vocal'
      ? `<button class="g" data-act="guide" aria-pressed="${!!t.guide}" title="${t.guide ? 'ガイドを外す' : 'このトラックをガイドにする'}" aria-label="ガイド">${ICON_GUIDE}</button>`
      : '<span class="gx"></span>';
    const pp = t.kind === 'vocal' ? `<span class="pp" data-pp="${esc(t.id)}"></span>` : '';
    const db = gainOf(t); const pan = panOf(t);
    const dragging = (k) => (mixDrag && mixDrag.id === t.id && mixDrag.kind === k ? ' drag' : '');
    const pct = (dbToPos(db) * 100).toFixed(2);
    // 高さが小さくて 2 段目を畳んでいる間は、値を名前のツールチップに出す
    const tip = `${esc(t.path || t.name)} — 音量 ${fmtDb(db)} dB・パン ${fmtPan(pan)}`;
    const r1 = `<div class="r1"><span class="nm" title="${tip}">${nm}</span>${pp}${g}`
      + `<button data-act="m" aria-pressed="${!!t.mute}" title="ミュート" aria-label="${nm} のミュート">M</button>`
      + `<button data-act="s" aria-pressed="${!!t.solo}" title="ソロ" aria-label="${nm} のソロ">S</button></div>`;
    // 2 段目（音量・パン）は Gliss の再生だけに効く。プラグインは DAW が鳴らすので出さない（M・S と同じ）
    const r2 = ARA ? '' : '<div class="r2">'
      + `<div class="vol${dragging('vol')}" data-mix="vol" role="slider" tabindex="0" aria-label="${nm} の音量" aria-valuemin="${GAIN_MIN_DB}" aria-valuemax="${GAIN_MAX_DB}" aria-valuenow="${db}" aria-valuetext="${fmtDb(db)} dB" title="音量 ${fmtDb(db)} dB（ダブルクリックで 0 dB・Shift で細かく）">`
      + `<i class="tr"></i><i class="fi" style="width:${pct}%"></i><i class="z" style="left:80%"></i><i class="kn" style="left:${pct}%"></i></div>`
      + `<span class="vv${Math.abs(db) > 0.04 ? ' chg' : ''}">${fmtDb(db)}</span>`
      + `<div class="pan${dragging('pan')}" data-mix="pan" role="slider" tabindex="0" aria-label="${nm} のパン" aria-valuemin="-100" aria-valuemax="100" aria-valuenow="${panUi(pan)}" aria-valuetext="${panSpeech(pan)}" title="パン ${fmtPan(pan)}（上下にドラッグ・ダブルクリックで中央）">${knobSvg(pan)}</div></div>`;
    return `<div class="${cls}" data-i="${i}" data-id="${esc(t.id)}">${r1}${r2}</div>`;
  }).join('');
}

// ---------------------------------------------------------------- 見出しの幅
/** 見出しの幅の上限（360 px と、トラックビューの幅の 40% の小さいほう。下限の 140 px は割らない）。 */
function maxHeadW() {
  const w = tv?.getBoundingClientRect().width || 0;
  return Math.max(HEAD_W.MIN, w > 0 ? Math.min(HEAD_W.MAX, Math.floor(w * HEAD_W.RATIO)) : HEAD_W.MAX);
}
/** 今の見出しの幅（覚えている幅を、いまのトラックビューの幅に収めたもの）。 */
export function headWidth() { return Math.round(clamp(HW, HEAD_W.MIN, maxHeadW())); }
/** 覚えている幅（state.json の view に保存する値）。 */
export function savedHeadWidth() { return HW; }

function applyHeadW() {
  if (!tv) return;
  const w = headWidth();
  tv.style.setProperty('--hw', `${w}px`);
  tv.classList.toggle('narrow', w < HEAD_W.NARROW);
  tv.classList.toggle('short', TH < SHORT_H);
  if (hsz) {
    hsz.setAttribute('aria-valuemin', String(HEAD_W.MIN));
    hsz.setAttribute('aria-valuemax', String(maxHeadW()));
    hsz.setAttribute('aria-valuenow', String(w));
    hsz.setAttribute('aria-valuetext', `${w} px`);
  }
}

/** 見出しの幅を決める（140〜360 px・トラックビューの幅の 40% まで）。save: 表示の設定として覚える。 */
export function setHeadWidth(w, { save = true } = {}) {
  const v = Math.round(clamp(+w || HEAD_W.DEF, HEAD_W.MIN, HEAD_W.MAX));
  const changed = v !== HW;
  HW = v;
  lastSig = '';
  renderTracks();
  if (changed && save) saveView();
  return headWidth();
}

export function renderTracks() {
  if (!lanes) return;
  layout();
  if (!S.tracks.length) return;
  applyHeadW();                       // 見出しの幅を先に決める（レーンの幅はそのあとで測る）
  laneW = Math.max(50, lanes.getBoundingClientRect().width || 1000);
  // 上の目盛り: タイムライン（ドラッグ中の見かけの位置を含む）＋後ろに 4% の余白（DAW の曲の終わりの後の空き）。
  // 同じトラックの並びの間は広がるだけ（自動では縮めない）。ドラッグ中も同じ規則で決めるので、
  // 離した後に目盛りが伸び縮みしない（ドラッグ中の見た目 = 離した後）。トラックを足す・外すと付け直す
  const tl = ARA ? araExtent(timelineRange()) : timelineRange();   // プラグインは複製したリージョンの位置まで含める
  const want = [tl[0], tl[1] + (tl[1] - tl[0]) * 0.04];
  const dir = S.session?.dir || null;
  if (dir !== tvDir) { tvDir = dir; tvView = null; }      // 別のプロジェクト: 全体表示から
  const key = `${dir}|${S.tracks.map((t) => t.id).join(',')}`;
  if (key !== rangeKey) { autoRange = want; rangeKey = key; } else if (!dr || dr.type !== 'move') {
    // 位置のドラッグ中は目盛りを止める（手の動きとクリップを 1:1 に）。離した後、はみ出したときだけ広げる
    if (tl[0] < autoRange[0] || tl[1] > autoRange[1]) {
      autoRange = [Math.min(autoRange[0], want[0]), Math.max(autoRange[1], want[1])];
    }
  }
  // ズーム・スクロールした表示（issue #39）は全体の範囲の中に収める（位置のドラッグ中は止める = 1:1）
  if (tvView && (!dr || dr.type !== 'move')) tvView = clampTv(tvView);
  range = tvView ? [tvView.t0, tvView.t0 + tvView.span] : autoRange;
  if (tvTool !== S.tool) { tvTool = S.tool; tvHover = null; lastCut = { id: null, t: 0, x: 0 }; }
  const hh = headsHtml();
  // 名前の入力中は見出しを作り直さない（入力欄が消える）
  if (hh !== lastHeads && !renaming) {
    const keep = focusedMix();
    heads.innerHTML = hh;
    lastHeads = hh;
    paintPrep(true);
    if (keep) heads.querySelector(`.th[data-id="${CSS.escape(keep.id)}"] [data-mix="${keep.kind}"]`)?.focus({ preventScroll: true });
  }
  heads.style.setProperty('--th', `${TH}px`);
  const vr = viewRange();
  const sig = JSON.stringify([laneW, TH, range, vr, S.loop, S.session?.current, S.session?.guide,
    rows().map((t) => [t.id, offsetOf(t), t.kind, t.mute, t.solo, t.duration_sec, t.cuts, t.mutes]), S.tool,
    overviews.size, dr && [dr.type, dr.row, dr.a, dr.b, dr.moved, dr.off], S.vd ? mutedSpans() : null,
    tempo(), G.fmt, currentDiv(), araSig()]);
  if (ARA) paintPrep();
  if (sig !== lastSig) {
    lastSig = sig;
    drawLanes(vr);
    drawRuler();
  }
  moveHead();
  applyCursor();
}

/** 編集中のトラックの無音の区間（ノートの無音。秒 = そのトラックの音の中。つながった分は 1 つにまとめる）。
 * 無音にした直後（当たるまで）の見かけも含める。ほかのボーカルには出さない（エンジンが区間を返さない）。 */
function mutedSpans() {
  const out = [];
  for (const n of S.notes) {
    if (!isMuted(n)) continue;
    const [a, b] = spanOf(n);
    const last = out[out.length - 1];
    if (last && a <= last[1] + 1e-4) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}

function drawLanes(vr) {
  const R = rows();
  const n = R.length;
  const LH = n * TH;
  lanes.setAttribute('height', LH);
  lanes.setAttribute('viewBox', `0 0 ${laneW} ${LH}`);
  const curId = S.session?.current;
  let s = '';
  R.forEach((t, i) => {
    const y = i * TH;
    const cur = t.id === curId;
    const isG = !!t.guide && !cur;
    s += `<rect x="0" y="${y}" width="${laneW}" height="${TH}" fill="${cur ? '#161619' : '#111113'}"/>`
      + `<line x1="0" y1="${y + 0.5}" x2="${laneW}" y2="${y + 0.5}" stroke="#232326"/>`;
    const off = offsetOf(t);
    const x0 = tvX(off); const x1 = tvX(off + (t.duration_sec || 0));
    // 色相 = トラックの種類（issue #37。モック v4）: 編集中 = 黄、ガイド = エディターと同じ濃いグレー、
    // ほかのボーカル = 暗い黄、伴奏 = 背景に近い薄いグレー。聞こえないトラックは薄く
    const col = cur ? TAKE : isG ? GUIDE : t.kind === 'vocal' ? VOCAL : INST;
    const op = (cur ? 0.75 : 1) * (audible(t) ? 1 : 0.35);
    const rs = ARA ? araRegions(t) : null;
    if (rs && rs.length) {
      // プラグイン: ソースの波形は薄く全体に、鳴る範囲（DAW のリージョン）だけ枠と普通の明るさで
      s += `<rect x="${f1(x0)}" y="${y + CLIP_T}" width="${f1(Math.max(1, x1 - x0))}" height="${clipH()}" rx="2" fill="#151518"/>`;
      for (const d of wavePaths(t)) {
        s += `<path d="${d}" fill="${col}" opacity="${(op * 0.25).toFixed(3)}" transform="translate(${f1(x0)},${y})" pointer-events="none"/>`;
      }
      rs.forEach((r, ri) => {
        const rx0 = tvX(r.song_start); const rx1 = tvX(r.song_end);
        const rw = f1(Math.max(1, rx1 - rx0));
        const cid = `rc-${i}-${ri}`;
        const sc = araScale(r);                                      // DAW の伸縮（テンポに合わせて伸ばしたリージョン）
        const shift = (r.song_start - r.mod_start * sc) - off;     // 代表の位置からのずれ（複製・移動したリージョン）
        s += `<clipPath id="${cid}"><rect x="${f1(rx0)}" y="${y}" width="${rw}" height="${TH}"/></clipPath>`
          + `<rect data-clip="${esc(t.id)}" data-region="${esc(r.id)}" x="${f1(rx0)}" y="${y + CLIP_T}" width="${rw}" height="${clipH()}" rx="2" fill="${cur ? '#202024' : '#1b1b1e'}" stroke="${cur ? '#56565c' : '#2e2e33'}"/>`;
        // clip-path は要素の transform の後の座標で効くので、g に掛けて（ずらさない座標で）切る
        s += `<g clip-path="url(#${cid})" pointer-events="none">`;
        for (const d of wavePaths(t, shift, sc)) {
          const scl = Math.abs(sc - 1) > 1e-9 ? ` scale(${sc.toFixed(6)},1)` : '';
          s += `<path d="${d}" fill="${col}" opacity="${op.toFixed(3)}" transform="translate(${f1(tvX(off + shift))},${y})${scl}"/>`;
        }
        s += '</g>';
      });
      return;
    }
    s += `<rect data-clip="${esc(t.id)}" x="${f1(x0)}" y="${y + CLIP_T}" width="${f1(Math.max(1, x1 - x0))}" height="${clipH()}" rx="2" fill="${cur ? '#202024' : '#1b1b1e'}"/>`;
    for (const d of wavePaths(t)) {
      s += `<path d="${d}" fill="${col}" opacity="${op.toFixed(3)}" transform="translate(${f1(x0)},${y})" pointer-events="none"/>`;
    }
    // クリップの切れ目（部分の境目）と、消した部分（ミュートツール）: 点線の輪郭・薄い波形・スピーカー×
    const bg = cur ? '#161619' : '#111113';
    for (const [a, b] of t.mutes || []) {
      const mx0 = Math.max(x0, tvX(off + a)); const mx1 = Math.min(x1, tvX(off + b));
      if (mx1 < 0 || mx0 > laneW || mx1 <= mx0) continue;
      s += `<rect data-mute-range="${esc(t.id)}" data-a="${a}" data-b="${b}" x="${f1(mx0)}" y="${y + CLIP_T}" width="${f1(Math.max(1, mx1 - mx0))}" height="${clipH()}" rx="2" fill="${bg}" fill-opacity=".78" stroke="#8f8f94" stroke-opacity=".7" stroke-dasharray="3 2.5" pointer-events="none"/>`;
      if (mx1 - mx0 > 22 && clipH() >= 22) s += `<g transform="translate(${f1(mx0 + 4)},${y + CLIP_T + 3}) scale(.5)" fill="none" stroke="#8f8f94" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" pointer-events="none">${ICON_MUTE}</g>`;
    }
    for (const c of t.cuts || []) {
      const cx = tvX(off + c);
      if (cx < -2 || cx > laneW + 2) continue;
      s += `<line data-cut="${esc(t.id)}" data-sec="${c}" x1="${f1(cx)}" y1="${y + CLIP_T}" x2="${f1(cx)}" y2="${y + CLIP_T + clipH()}" stroke="${bg}" stroke-width="2" pointer-events="none"/>`;
    }
    // 無音のノート（ミュートツール・Del）: 波形を暗くして、点線の輪郭（ピアノロールの無音のノートと同じ見分け）
    if (cur && S.vd) {
      for (const [a, b] of mutedSpans()) {
        const mx0 = tvX(off + a); const mx1 = tvX(off + b);
        if (mx1 < 0 || mx0 > laneW) continue;
        s += `<rect data-muted-span="${f1(a)}" x="${f1(mx0)}" y="${y + CLIP_T}" width="${f1(Math.max(1, mx1 - mx0))}" height="${clipH()}" rx="2" fill="#161619" fill-opacity=".72" stroke="#8f8f94" stroke-opacity=".7" stroke-dasharray="3 2.5" pointer-events="none"/>`;
      }
    }
  });
  // 下で表示している範囲（レーンをドラッグ中はその範囲）
  const ci = R.findIndex((t) => t.id === curId);
  let fr = null;
  if (dr && dr.type === 'range' && dr.moved) fr = { row: dr.row, a: dr.a, b: dr.b };
  else if (ci >= 0 && S.vd) fr = { row: ci, a: vr[0], b: vr[1] };
  if (fr) {
    const bx0 = tvX(fr.a); const bx1 = Math.max(tvX(fr.b), bx0 + 2);
    s += `<rect id="tvFrame" x="${f1(bx0)}" y="${fr.row * TH + 2.5}" width="${f1(bx1 - bx0)}" height="${TH - 4}" fill="${SEL}" fill-opacity=".08" stroke="${SEL}" stroke-opacity=".55" pointer-events="none"/>`;
  }
  if (S.loop) {
    s += `<rect x="${f1(tvX(S.loop[0]))}" y="0" width="${f1(tvX(S.loop[1]) - tvX(S.loop[0]))}" height="${LH}" fill="${SEL}" opacity=".04" pointer-events="none"/>`;
  }
  // 位置をドラッグ中: ずらした量（元の位置からの秒）
  if (dr && dr.type === 'move' && dr.moved) {
    const t = R[dr.row];
    const v = t ? offsetOf(t) : 0;
    const txt = `${v > 0 ? '+' : v < 0 ? '−' : '±'}${Math.abs(v).toFixed(3)} s`;
    const w = txt.length * 6.6 + 10;
    const tx = clamp(tvX(v) + 4, 0, laneW - w);
    const ty = dr.row * TH + CLIP_T + 2;
    s += `<g id="tvTip" pointer-events="none"><rect x="${f1(tx)}" y="${ty}" width="${f1(w)}" height="17" rx="2" fill="#232326"/>`
      + `<text x="${f1(tx + 5)}" y="${ty + 12.5}" font-size="11" fill="${SEL}">${txt}</text></g>`;
  }
  s += '<g id="tvov" pointer-events="none"></g>';
  s += `<line id="tvph" x1="0" y1="0" x2="0" y2="${LH}" stroke="${SEL}" pointer-events="none"/>`;
  lanes.innerHTML = s;
  paintOverlay();
}

/** はさみの縦線・ミュートの部分の枠（ホバー。lanes を描き直さずに付け替える）。 */
function paintOverlay() {
  const g = lanes?.querySelector('#tvov');
  if (!g) return;
  const h = tvHover;
  const R = rows();
  const i = h ? R.findIndex((t) => t.id === h.id) : -1;
  if (i < 0 || (dr && dr.type !== 'paint')) { g.innerHTML = ''; return; }
  const t = R[i];
  const y = i * TH; const off = offsetOf(t);
  let s = '';
  if (S.tool === 'cut') {
    const x = tvX(off + (h.cut ?? h.t));
    s = `<line id="tvcut" x1="${f1(x)}" y1="${y + 2}" x2="${f1(x)}" y2="${y + TH - 2}" stroke="${SEL}" stroke-width="${h.cut != null ? 2 : 1}"/>`;
  } else if (S.tool === 'mute' && h.piece) {
    const xa = tvX(off + h.piece[0]); const xb = tvX(off + h.piece[1]);
    s = `<rect id="tvpiece" x="${f1(xa)}" y="${y + CLIP_T}" width="${f1(Math.max(1, xb - xa))}" height="${clipH()}" rx="2" fill="${SEL}" fill-opacity=".06" stroke="${SEL}" stroke-opacity=".6"/>`;
  }
  g.innerHTML = s;
}

function drawRuler() {
  const rw = ruler.getBoundingClientRect().width || laneW;
  ruler.setAttribute('viewBox', `0 0 ${rw} ${RH}`);
  // 目盛りはエディターと同じ読み方（小節・拍か秒。issue #18）。ラベルは 70 px 以上あける
  let s = `<rect width="${rw}" height="${RH}" fill="#111113"/>`;
  const tk = ticks(range[0], range[1], pps(), TV_GRID_PX, 70);
  for (const g of tk.out) {
    if (g.l === 2 || (!g.lab && g.l !== 0)) continue;
    const x = tvX(g.t);
    s += `<line x1="${f1(x)}" y1="${RH - (g.lab ? 8 : 4)}" x2="${f1(x)}" y2="${RH}" stroke="#3a3a3e"/>`;
    if (g.lab) s += `<text data-rlab="1" x="${f1(x + 3)}" y="12" font-size="10" fill="#8f8f94">${g.lab}</text>`;
  }
  if (S.loop) {
    s += `<rect x="${f1(tvX(S.loop[0]))}" y="${RH - 3}" width="${f1(tvX(S.loop[1]) - tvX(S.loop[0]))}" height="3" fill="${SEL}" opacity=".5"/>`;
  }
  s += `<path id="tvphr" d="M-4,${RH - 6} L4,${RH - 6} L0,${RH} Z" fill="${SEL}"/>`;
  ruler.innerHTML = s;
}

function moveHead() {
  // 再生位置に追従（issue #40）: ズームしている上の表示も、再生位置が出たら送る（全体表示なら要らない）
  if (S.playing && G.follow && tvView && !dr && !hd && (S.head < range[0] || S.head > range[1])) {
    const w = worldRange();
    if (S.head >= w[0] && S.head <= w[1]) {
      tvView = clampTv({ t0: S.head - tvView.span * 0.02, span: tvView.span });
      renderTracks();
      return;                       // renderTracks が再生位置も描く
    }
  }
  const x = f1(tvX(S.head));
  lanes?.querySelector('#tvph')?.setAttribute('transform', `translate(${x},0)`);
  ruler?.querySelector('#tvphr')?.setAttribute('transform', `translate(${x},0)`);
}

// ---------------------------------------------------------------- 表示範囲・編集対象
/** 下の表示範囲をタイムラインの [a, b] にする（編集中のトラックの中に丸める）。 */
export function setViewTimeline(a, b) {
  const total = totalSec();
  const span = clamp(b - a, 0.15, total);
  const t0 = clamp(a - S.off, 0, Math.max(0, total - span));
  S.view = { t0, span };
  saveView();
}

/** 上でドラッグしている（外部の変更の読み直しはこれが終わってから）。 */
export function isDragging() { return !!dr; }

/** 失敗したとき: エンジンの今の状態（トラック・編集対象・描画データ）に画面を合わせ直す。 */
async function resync(err = null) {
  if (err && await handleEngineError(err)) return;
  try {
    adoptSession(await call('list_tracks', {}));
    await refresh({ keepView: true });
  } catch { /* 読めなければそのまま */ }
}

/** 再生中にトラックの位置・並びが変わった: 今の再生位置から張り直す（見た目と音をそろえる）。 */
function replay() {
  if (!S.playing) return;
  stop();
  play();
}

/** 編集対象を切り替える。`view`（タイムラインの [a, b]）を省くと表示範囲はそのまま。
 * `first`: 最初の発声に寄せる（ドロップで切り替えたとき。新しく開いたテイクと同じ）
 *
 * 準備済みなら analyze_take はその場で返り、ポップアップは出さない（0.3 秒を超えたときだけクリップと上の細い線）。
 * 準備が終わっていなければ（analyze_take がジョブになって準備に合流する）、止める処理のポップアップで待たせる。
 * Esc で取り消すと前のトラックに戻す（裏の準備は続く。issue #63 の 4） */
export function selectTrack(id, { view = null, first = false } = {}) {
  const t = S.tracks.find((x) => x.id === id);
  if (!t || t.kind !== 'vocal') return Promise.resolve(false);
  const want = view || viewRange();
  if (id === S.session?.current && S.vd) {
    setViewTimeline(want[0], want[1]);
    render();
    return Promise.resolve(true);
  }
  return opening(enqueue(async () => {
    S.busy = true;
    S.opening = (S.opening || 0) + 1;
    renderToolbar();
    const back = backPoint();
    const line = beginBusy({ label: `${t.name} に切り替えている…`, clipId: id });
    const hold = laterBusy({ label: `${t.name} を準備している`, target: baseName(t.path || t.name) });
    try {
      status(`切り替えている… ${t.name}`);
      const r = await call('select_track', { track_id: id }, { busy: false });
      adoptSession(r.session);
      S.sel = [];
      if (r.analyzed) hold.set({ label: `${t.name} を読み込んでいる` });
      await analyzeTake({ hold, cancel: true, onProgress: (sec) => status(`解析している… ${sec} 秒`) });
      S.view = { t0: want[0] - offsetOf(currentTrack() || t), span: want[1] - want[0] };
      S.pv = null;                    // 縦（音程）は別のテイクの音域で作り直す
      await refresh({ keepView: true });
      if (first) { onNewTake(); render(); }
      status(`${t.name}${guideSuffix()} — ノート ${S.pitched.length}${phonemeSuffix()}`);
      return true;
    } catch (err) {
      if (err.cancelled) { await goBack(back, t); return false; }
      status(`切り替えられなかった: ${err.message}`);
      await resync(err);
      return false;
    } finally {
      hold.finish();
      line.finish();
      S.opening -= 1;
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }));
}

/** 切り替えを取り消したときに戻る先（今のトラックと表示）。 */
function backPoint() {
  return { id: S.session?.current || null, had: !!S.vd, view: { ...S.view }, pv: S.pv ? { ...S.pv } : null };
}

/** 準備の待ちを取り消した（Esc）: 前のトラックに戻す。合流を取り消しても、裏の準備は続く。 */
async function goBack(back, t) {
  const note = `${t.name} の準備は裏で続く`;
  try {
    if (!back.had || !back.id || back.id === t.id || !S.tracks.some((x) => x.id === back.id)) {
      // 前に開いていたトラックが無い: 下は空のまま（トラックを選ぶと続きから開く）
      clearProject();
      render();
      renderTracks();
      status(`切り替えを取り消した（${note}。トラックを選ぶと続きから開く）`);
      return;
    }
    const r = await call('select_track', { track_id: back.id }, { busy: false });
    adoptSession(r.session);
    const p = S.tracks.find((x) => x.id === back.id);
    await analyzeTake({ label: `${p?.name || ''} に戻している`, target: baseName(p?.path || p?.name || '') });
    S.view = back.view;
    S.pv = back.pv;
    await refresh({ keepView: true });
    status(`切り替えを取り消して ${p?.name} に戻した（${note}）`);
  } catch (err) {
    status(`前のトラックに戻せなかった: ${err.message}`);
    await resync(err);
  }
}

/** 順番待ちに入れたトラックの追加・切り替えが終わるまで、次のドロップを断る（main.js の openDropped）。 */
function opening(p) {
  S.openQueued += 1;
  p.finally(() => { S.openQueued -= 1; }).catch(() => {});
  return p;
}

// ---------------------------------------------------------------- 準備中の印（issue #63 の 4）
// 見出しの名前の右に小さく出す。準備中 = 進み具合の輪、待ち = 点線の輪、失敗 = 「!」（ガイドとの対応付けの失敗 #32 は
// 開いて編集できるので薄く）。準備済み（描画データを作っている段 view も）は何も出さない。段階名はツールチップ。
// エンジンは準備の進みを知らせてこないので、準備中・待ちのトラックがある間だけ prep_status（ロックを取らない）を読む
const PREP_POLL_MS = 1000;
const prep = new Map();         // トラック id → prep（session.tracks[].prep / prep_status の tracks[]）
let prepTimer = null;           // 次の prep_status の予約（**常に 1 本だけ**）
let prepGen = 0;                // セッションを取り込んだ回数（prep_status の応答・失敗が、今のセッションのものか）
let prepPolling = false;        // prep_status を待っている（その間は予約しない。応答・失敗のところで決める）
const RING = 2 * Math.PI * 4;
const FALLBACK = 'ガイドとの対応付けに失敗した（位置のままの対応で表示）';
const BANG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.5v4.5M6 9.2v.3"/></svg>';

const pending = (p) => !!p && (p.state === 'queued' || (p.state === 'preparing' && p.stage !== 'view'));

/** 印の中身: [状態（class）, ツールチップ, SVG]。無ければ null。 */
function prepMark(p) {
  if (!p || p.state === 'ready' || (p.state === 'preparing' && p.stage === 'view')) return null;
  if (p.state === 'preparing') {
    const v = Math.max(0.04, Math.min(1, p.progress || 0));
    const tip = `準備中: ${p.stage_label || '開いている'} ${Math.round((p.progress || 0) * 100)}%`
      + `${p.paused ? '（再生・書き出しの間は待っている）' : ''}。選ぶと終わるまで待つ`;
    return ['preparing', tip, '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" class="bg"/>'
      + `<circle cx="6" cy="6" r="4" class="fg" stroke-dasharray="${(v * RING).toFixed(2)} ${RING.toFixed(2)}" transform="rotate(-90 6 6)"/></svg>`];
  }
  if (p.state === 'queued') {
    return ['queued', '準備の順番待ち。選ぶと先に準備して、終わるまで待つ',
      '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" class="dot"/></svg>'];
  }
  const err = String(p.error || '');
  if (err.startsWith(FALLBACK)) {
    return ['fallback', `${FALLBACK}。開いて編集できる（ガイドに合わせるも、位置のままの対応で使える）`, BANG];
  }
  return ['failed', `準備に失敗した: ${err || '理由は不明'}。選ぶともう一度解析する`, BANG];
}

/** プラグイン: DAW の音の読み込み・編集の反映・照合の失敗の印（ara.js のキャッシュの状態）。無ければ null。 */
function araMark(id) {
  const c = araCacheOf(id);
  if (!c) return null;
  const ring = (v) => '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" class="bg"/>'
    + `<circle cx="6" cy="6" r="4" class="fg" stroke-dasharray="${(Math.max(0.04, Math.min(1, v)) * RING).toFixed(2)} ${RING.toFixed(2)}" transform="rotate(-90 6 6)"/></svg>`;
  if (c.state === 'reading') {
    const v = Number.isFinite(c.progress) ? c.progress : 0;
    return ['preparing', `DAW の音を読み込んでいる ${Math.round(v * 100)}%`, ring(v)];
  }
  if (c.state === 'syncing') return ['preparing', '編集を DAW の再生に反映している', ring(0.5)];
  if (c.state === 'waiting') {
    return ['queued', '解析の順番待ち（その間は原音が鳴る）', '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" class="dot"/></svg>'];
  }
  if (c.state === 'mismatch') return ['failed', 'DAW の音が変わったので、編集を当てていない（元の音に戻すと当たる）', BANG];
  if (c.state === 'failed') return ['failed', `準備に失敗した${c.error ? `: ${c.error}` : ''}`, BANG];
  return null;
}

/** プラグイン（ara.js）の札が使う: トラックの裏の準備の今の状態。 */
export const prepOf = (id) => prep.get(id) || null;

/** 見出しの印を今の状態に合わせる（見出しは作り直さずに中身だけ。ツールチップを出している間に消さない）。 */
function paintPrep(force = false) {
  if (!heads) return;
  for (const el of heads.querySelectorAll('.pp')) {
    const m = (ARA && araMark(el.dataset.pp)) || prepMark(prep.get(el.dataset.pp));
    if (!m) {
      if (!force && !el.dataset.state) continue;
      delete el.dataset.state;
      el.removeAttribute('title');
      el.removeAttribute('role');
      el.removeAttribute('aria-label');
      el.innerHTML = '';
      el._svg = null;
      continue;
    }
    const [state, tip, svg] = m;
    if (el.dataset.state !== state) el.dataset.state = state;
    if (el.title !== tip) { el.title = tip; el.setAttribute('aria-label', tip); el.setAttribute('role', 'img'); }
    if (force || el._svg !== svg) { el.innerHTML = svg; el._svg = svg; }
  }
}

/** 準備の状態を取り込む（[id, prep] の並び）。準備中・待ちが残っていれば 1 秒後にもう一度読む。
 * session: セッションを取り込んだ（世代を進める。前のセッションの prep_status の応答・失敗は捨てる）。 */
function adoptPrep(list, { session = false } = {}) {
  if (session) prepGen += 1;
  const ids = new Set(S.tracks.map((t) => t.id));
  for (const id of [...prep.keys()]) if (!ids.has(id)) prep.delete(id);
  for (const [id, p] of list) if (ids.has(id)) prep.set(id, p || null);
  paintPrep();
  watchAwaited();
  schedulePrep(PREP_POLL_MS);
}

/** 準備中・待ちのトラックがあれば、次の prep_status を 1 本だけ予約する（読んでいる途中なら、終わったところで決める）。 */
function schedulePrep(ms) {
  clearTimeout(prepTimer);
  prepTimer = null;
  if (prepPolling) return;
  if ([...prep.values()].some(pending)) prepTimer = setTimeout(pollPrep, ms);
}

const normDir = (d) => String(d || '').replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase();

const sameSession = (r) => !r.session_dir || !S.session?.dir || normDir(r.session_dir) === normDir(S.session.dir);

async function pollPrep() {
  prepTimer = null;
  if (prepPolling) return;
  const gen = prepGen;
  prepPolling = true;
  let r = null;
  try {
    r = await call('prep_status', {});
  } catch {
    r = null;
  } finally {
    prepPolling = false;
  }
  // 待つ間にセッションを取り込んだ（閉じた・別の曲）: この応答・失敗は使わず、今のセッションの状態で決める
  if (gen !== prepGen) { schedulePrep(PREP_POLL_MS); return; }
  if (!r) { schedulePrep(PREP_POLL_MS * 3); return; }   // エンジンが落ちている・閉じている間は間を空ける
  if (!sameSession(r)) return;
  adoptPrep((r.tracks || []).map((p) => [p.id, p]));
}

/** 今すぐ準備の状態を読み直す（待つのをやめたとき。予約は 1 本のまま）。 */
async function readPrepNow() {
  const gen = prepGen;
  try {
    const r = await call('prep_status', {});
    if (gen === prepGen && sameSession(r)) adoptPrep((r.tracks || []).map((p) => [p.id, p]));
  } catch { /* 読めなければ前の印のまま */ }
}

// ---------------------------------------------------------------- 待つのをやめた（issue #63）
// 取り消しを出さない待ち（ガイドの指定・Ctrl+Z・トラックを外す・保存・外部の変更・歌詞の後の解析）で、
// ポップアップの「待つのをやめる（Esc）」を押した（engine.callJob）。そのトラックの下の表示を空にし、編集は
// 「準備中」で断る（edits.js）。裏の準備が続いていれば、印のポーリングで準備が終わったところで描き直す。
onAbandon(async (err) => {
  const id = S.session?.current || null;
  const t = S.tracks.find((x) => x.id === id);
  const aw = id ? { id, name: t?.name || '', view: { ...S.view }, pv: S.pv ? { ...S.pv } : null, watch: false } : null;
  clearProject();
  S.awaitPrep = aw;
  render();
  renderTracks();
  renderToolbar();
  await readPrepNow();
  err.prepContinues = !!aw && S.awaitPrep === aw && pending(prep.get(aw.id));
  if (aw) aw.watch = err.prepContinues;
});

/** 待つのをやめたトラックの準備が終わった（準備中・待ちでなくなった）: 描き直す。 */
function watchAwaited() {
  const aw = S.awaitPrep;
  if (!aw?.watch || S.session?.current !== aw.id || pending(prep.get(aw.id))) return;
  aw.watch = false;
  redrawAwaited(aw);
}

function redrawAwaited(aw) {
  return enqueue(async () => {
    if (S.awaitPrep !== aw || S.session?.current !== aw.id) return false;
    const t = S.tracks.find((x) => x.id === aw.id);
    S.busy = true;
    S.opening = (S.opening || 0) + 1;
    renderToolbar();
    const hold = laterBusy({ label: `${aw.name} を読み込んでいる`, target: baseName(t?.path || aw.name) });
    try {
      await analyzeTake({ hold, cancel: true, onProgress: (sec) => status(`読み込んでいる… ${sec} 秒`) });
      S.view = aw.view;
      S.pv = aw.pv;
      await refresh({ keepView: aw.view.span > 0 });
      status(`${aw.name} の準備ができたので表示した`);
      return true;
    } catch (err) {
      if (err.cancelled) {
        S.awaitPrep = aw;               // Esc: 空のまま（トラックを選ぶともう一度待つ）
        status('待つのをやめた。トラックを選ぶと、もう一度解析する');
      } else if (!await handleEngineError(err)) {
        status(`${aw.name} を表示できなかった: ${err.message}`);
      }
      return false;
    } finally {
      hold.finish();
      S.opening -= 1;
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  });
}

// ---------------------------------------------------------------- 位置（音源全体をずらす。§4）
/** トラックの位置を確定する（離したとき・Ctrl+Z / Ctrl+Y）。確定するまで見かけの位置（S.trackOff）を残す
 * （ドラッグ中の見た目 = 離した後。エンジンの値が返ってから見かけを外す）。 */
export function commitOffset(id, from, to) {
  if (ARA) return Promise.resolve(false);          // プラグイン: 位置は DAW が決める
  S.trackOff.set(id, to);
  if (id === S.session?.current) S.off = to;
  renderTracks();
  // まだ当たっていない間に Ctrl+Z で外したら、見かけの位置を消す（この値のときだけ。後のドラッグは残す）
  const cancel = () => {
    if (S.trackOff.get(id) === to) S.trackOff.delete(id);
    const t = S.tracks.find((x) => x.id === id);
    if (t && id === S.session?.current) S.off = offsetOf(t);
    renderTracks();
  };
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    const t = S.tracks.find((x) => x.id === id);
    try {
      const r = await call('set_track', { track_id: id, offset_sec: to, author: 'human' });
      S.trackOff.delete(id);
      adoptSession(r.session);
      replay();
      if (r.reopened) await reanalyze();       // ガイドとの位置が変わった: ガイドを切り出し直して対応付け直す
      status(`${t?.name || id} の位置: ${to > 0 ? '+' : to < 0 ? '−' : '±'}${Math.abs(to).toFixed(3)} 秒`
        + (Math.abs(to) < 1e-9 ? '（元の位置）' : ''));
      return true;
    } catch (err) {
      S.trackOff.delete(id);
      if (!await handleEngineError(err)) {
        status(`位置を変えられなかった: ${err.message}`);
        try { adoptSession((await call('list_tracks', {}))); } catch { /* noop */ }
      }
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      renderTracks();
      wake();
    }
  }, { label: 'トラックの位置', cancel });
}

/** Ctrl+Z / Ctrl+Y の結果（エンジンの undo / redo）を画面に反映する（edits.js から。もう順番待ちの中）。
 * トラックの並び・位置を取り込み、別のトラックに切り替わったら表示範囲（タイムラインの秒）はそのままで
 * そのトラックを解析して出す。ガイドとの位置が変わったら対応付け直す。 */
// 解析がジョブになったら止める処理のポップアップを出し、描き直す（edits.js の historyStep）まで残す（返り値）
setHistoryHandler(async (r) => {
  const want = viewRange();
  const before = S.session?.current;
  if (r.session) adoptSession(r.session);
  let hold = null;
  try {
    if (r.switched_to && r.switched_to !== before) {
      S.sel = [];
      setPlan(null);
      S.stroke = null;
      const t = currentTrack();
      hold = laterBusy({ label: `${t?.name || ''} を準備している`, target: baseName(t?.path || t?.name || '') });
      await analyzeTake({ hold, onProgress: (sec) => status(`解析している… ${sec} 秒`) });
      if (t) S.view = { t0: want[0] - offsetOf(t), span: want[1] - want[0] };
      S.pv = null;                  // 縦（音程）は切り替えた先のテイクの音域で作り直す
    } else if (r.reopened) {
      hold = laterBusy({ label: 'ガイドと対応付けている', target: currentTrack()?.name || '' });
      await analyzeTake({ hold, onProgress: (sec) => status(`ガイドと対応付けている… ${sec} 秒`) });
    }
  } catch (err) {
    hold?.finish();                 // 返さないので、ここで外す（覆いを残さない）
    throw err;
  }
  replay();
  renderTracks();
  if ((r.undone || r.redone)?.kind === 'estimator') await refreshF0({ syncSaved: true });
  return hold;
});

// ---------------------------------------------------------------- トラックの追加・削除・種類（§5）

/** 音声ファイルをトラックとして足す。select: ボーカルなら編集対象にする。guide: ガイドに指定する
 * （もうトラックにあるファイルなら指定だけ）。種類はエンジンがファイル名から推す（伴奏は選ばない）。 */
export function addTrackFile(path, { select = false, guide = false } = {}) {
  const known = S.tracks.some((t) => String(t.path).toLowerCase() === String(path).toLowerCase());
  return opening(enqueue(async () => {
    S.busy = true;
    S.opening = (S.opening || 0) + 1;
    renderToolbar();
    const back = backPoint();
    let hold = null;
    let t = null;
    try {
      status(`トラックを足している… ${baseName(path)}`);
      const r = await call('add_track', { path, select, guide, author: 'human' });
      adoptSession(r.session);
      t = S.tracks.find((x) => x.id === r.track);
      if (r.selected) {
        S.sel = [];
        // 足したばかりのトラックは準備がまだ: 準備に合流して待つ（Esc で前のトラックに戻る。足したトラックは残る）
        hold = laterBusy({ label: `${t?.name || baseName(path)} を準備している`, target: baseName(path) });
        await analyzeTake({ hold, cancel: true, onProgress: (sec) => status(`解析している… ${sec} 秒`) });
        await refresh({ keepView: false });
        onNewTake();
        render();
        status(`${t?.name}${guideSuffix()} — ノート ${S.pitched.length}${phonemeSuffix()}`);
      } else {
        if (r.reopened) await reanalyze();
        status(guide ? `ガイド: ${t?.name}`
          : `トラックを足した: ${t?.name}（${t?.kind === 'inst' ? '伴奏' : 'ボーカル'}）`);
      }
      replay();
      return t;
    } catch (err) {
      if (err.cancelled && t) { await goBack(back, t); return t; }
      status(`トラックを足せなかった: ${err.message}`);
      await resync(err);
      return null;
    } finally {
      hold?.finish();
      S.opening -= 1;
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }, { label: known ? (guide ? 'ガイドの指定' : null) : 'トラックの追加' }));
}

/** トラックを外す（音声ファイルとそのトラックの編集は消さない）。編集中なら残りの最初のボーカルへ。 */
export function removeTrack(id) {
  return enqueue(async () => {
    const t = S.tracks.find((x) => x.id === id);
    if (!t) return false;
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('remove_track', { track_id: id, author: 'human' });
      adoptSession(r.session);
      if (r.switched_to) {
        S.sel = [];
        const nt = currentTrack();
        const hold = laterBusy({ label: `${nt?.name || ''} を準備している`, target: baseName(nt?.path || nt?.name || '') });
        try {
          await analyzeTake({ hold, onProgress: (sec) => status(`解析している… ${sec} 秒`) });
          S.pv = null;                // 縦（音程）は切り替えた先のテイクの音域で作り直す
          await refresh({ keepView: true });
        } finally {
          hold.finish();
        }
      } else if (r.reopened) {
        await reanalyze();
      }
      status(`トラックを外した: ${t.name}（ファイルと編集は残っている。Ctrl+Z で戻る）`);
      replay();
      return true;
    } catch (err) {
      status(`トラックを外せなかった: ${err.message}`);
      await resync(err);
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }, { label: 'トラックを外す' });
}

/** 伴奏として扱う／ボーカルとして扱う。 */
export function setKind(id, kind) {
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('set_track', { track_id: id, kind, author: 'human' });
      adoptSession(r.session);
      if (r.reopened) await reanalyze();
      return true;
    } catch (err) {
      if (!await handleEngineError(err)) status(`種類を変えられなかった: ${err.message}`);
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }, { label: '伴奏／ボーカルの扱い' });
}

/** トラックの名前を変える（取り消せる）。 */
export function renameTrack(id, name) {
  const v = String(name || '').trim();
  const t0 = S.tracks.find((x) => x.id === id);
  if (!t0 || !v || v === t0.name) return Promise.resolve(false);
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('set_track', { track_id: id, name: v, author: 'human' });
      adoptSession(r.session);
      return true;
    } catch (err) {
      if (!await handleEngineError(err)) status(`名前を変えられなかった: ${err.message}`);
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }, { label: 'トラックの名前' });
}

/** 見出しの名前を入力欄にする（右クリックの「名前を変える」・F2）。Enter で確定、Esc・外をクリックで取りやめ。 */
export function startRename(t) {
  const el = heads.querySelector(`.th[data-id="${CSS.escape(t.id)}"] .nm`);
  if (!el) return;
  const input = document.createElement('input');
  input.className = 'rn';
  input.value = t.name;
  input.setAttribute('aria-label', 'トラックの名前');
  el.replaceWith(input);
  renaming = t.id;
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    const v = input.value;
    renaming = null;
    lastHeads = '';                   // 見出しを作り直す（入力欄を名前に戻す）
    renderTracks();
    root.focus({ preventScroll: true });
    if (ok) renameTrack(t.id, v);
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(false));
  input.focus();
  input.select();
}

/** 編集中のプロジェクトを開き直した（ガイド・位置が変わった）: 解析し直して描き直す。
 * 一度組んだガイドならその場で返る。初めての組み合わせで対応付けが要るときだけ、止める処理のポップアップ。 */
async function reanalyze() {
  const hold = laterBusy({ label: 'ガイドと対応付けている', target: currentTrack()?.name || '' });
  try {
    await analyzeTake({ hold, onProgress: (sec) => status(`ガイドと対応付けている… ${sec} 秒`) });
    await refresh({ keepView: true });
  } finally {
    hold.finish();
  }
}

/** ガイドを指定する（null で外す）。 */
export function setGuide(id) {
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('set_guide_track', { track_id: id || null, author: 'human' });
      adoptSession(r.session);
      if (r.reopened) await reanalyze();
      const why = guideWhy();
      status(id ? `ガイド: ${S.tracks.find((t) => t.id === id)?.name}${why ? `（今のトラックには重ならない: ${why}）` : ''}`
        : 'ガイドを外した');
    } catch (err) {
      if (!await handleEngineError(err)) status(`ガイドを変えられなかった: ${err.message}`);
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      wake();
    }
  }, { label: 'ガイドの指定' });
}

// ---------------------------------------------------------------- ポインタ
function laneHit(e) { return hitAt(e.clientX, e.clientY); }

function hitAt(clientX, clientY) {
  const r = lanes.getBoundingClientRect();
  const x = clientX - r.left; const y = clientY - r.top;
  const row = Math.floor(y / TH);
  const t = rows()[row] || null;
  const tl = tvT(x);
  let inClip = false; let move = false;
  if (t) {
    const off = offsetOf(t);
    const rs = ARA ? araRegions(t) : null;
    inClip = rs && rs.length ? rs.some((r) => tl >= r.song_start && tl <= r.song_end)    // プラグイン: どのリージョンの枠でも同じトラック
      : tl >= off && tl <= off + (t.duration_sec || 0);
    const yy = y - row * TH;
    move = !ARA && inClip && yy >= CLIP_T + clipH() / 2 && yy <= CLIP_T + clipH();   // 下半分 = 位置（プラグインは DAW が決める）
  }
  return { x, y, row, t, tl, inClip, move };
}

function onLaneDown(e) {
  if (e.button !== 0 || !S.tracks.length) return;
  e.preventDefault();
  root.focus({ preventScroll: true });
  closeMenu();
  const h = laneHit(e);
  if (!h.t) return;
  // はさみ・ミュートは、クリップの上ではメインの操作（範囲・位置）の代わりに働く（クリップの外はメインと同じ）
  if (clipTool() === 'cut' && h.inClip) { cutDown(e, h); return; }
  if (clipTool() === 'mute' && h.inClip) { muteDown(e, h); return; }
  if (h.move) {
    // クリップの下半分: 位置をずらす（音源全体）。確定待ちの見かけの位置があれば、そこから
    dr = { type: 'move', row: h.row, id: h.t.id, x0: e.clientX, xl: e.clientX, t0: h.tl,
      off0: offsetOf(h.t), raw: offsetOf(h.t), moved: false,
      prev: S.trackOff.has(h.t.id) ? S.trackOff.get(h.t.id) : undefined };
    lanes.style.cursor = 'grabbing';
  } else {
    dr = { type: 'range', row: h.row, id: h.t.id, x0: e.clientX, t0: h.tl, a: h.tl, b: h.tl, moved: false };
  }
  lanes.setPointerCapture(e.pointerId);
}

function moveDrag(e) {
  const t = S.tracks.find((x) => x.id === dr.id);
  if (!t) return;
  if (e.altKey) window.api.consumeAlt?.();   // Alt（吸い付かない）を離したときメニューバーへ行かない
  if (!dr.moved && Math.abs(e.clientX - dr.x0) < 3) return;
  dr.moved = true;
  // Shift で細かく（1/10）。途中で押しても跳ばないように、前の位置からの差を積む
  dr.raw += (e.clientX - dr.xl) / pps() * (e.shiftKey ? 0.1 : 1);
  dr.xl = e.clientX;
  let v = Math.round(dr.raw * 1e6) / 1e6;
  if (timeSnapOn(e)) {
    // 時間スナップ: クリップの頭（音源の頭）をグリッドに寄せる（Shift で解除。issue #18）
    v = Math.round(snapTime(v, tvStep()) * 1e6) / 1e6;
  } else if (!e.altKey && !e.shiftKey && Math.abs(v) <= Math.min(SNAP_PX / pps(), SNAP_MAX_SEC)) {
    v = 0;               // 元の位置に吸い付く（6 px かつ 30 ms 以内。Shift・Alt の間は吸い付かない）
  }
  S.trackOff.set(t.id, v);
  dr.off = v;
  if (t.id === S.session?.current) {
    S.off = v;                // 下の目盛り・再生位置も、離した後と同じ位置に動かす
    render();
  } else {
    renderTracks();
  }
}

function endMove() {
  const d = dr;
  dr = null;
  lanes.style.cursor = '';
  const t = S.tracks.find((x) => x.id === d.id);
  if (!t) { renderTracks(); return; }
  const v = S.trackOff.has(t.id) ? S.trackOff.get(t.id) : d.off0;
  if (!d.moved || Math.abs(v - d.off0) < 1e-9) {
    // 動かさなかった: 見かけの位置はドラッグ前に戻す（確定待ちのものは残す）
    if (d.prev === undefined) S.trackOff.delete(t.id); else S.trackOff.set(t.id, d.prev);
    if (t.id === S.session?.current) S.off = offsetOf(t);
    if (!d.moved) { clickAt(t, d.t0); return; }
    render();
    renderTracks();
    return;
  }
  commitOffset(t.id, d.off0, v);
}

/** クリップ・レーンのクリック（上半分でも下半分でも）。 */
function clickAt(t, tl) {
  const off = offsetOf(t);
  const rs = ARA ? araRegions(t) : null;
  const inClip = rs && rs.length ? rs.some((r) => tl >= r.song_start && tl <= r.song_end)
    : tl >= off && tl <= off + (t.duration_sec || 0);
  if (t.kind === 'vocal' && inClip) {
    selectTrack(t.id, { view: soundRegion(t, ARA ? araToRep(t, tl) : tl) });
    return;
  }
  // 伴奏・クリップの外: 再生位置が動くだけ（プラグインは DAW の再生位置も動かす）
  S.head = tl;
  if (ARA) araSeek({ song_sec: tl });
  if (S.playing) stop();
  render();
}

/** 範囲ドラッグの範囲を、離した後の表示範囲と同じ規則で丸める（トラックの中・最小 0.15 秒）。 */
function clampRange(t, a, b) {
  const off = offsetOf(t);
  const dur = Math.max(MIN_VIEW, t.duration_sec || 0);
  let x0 = clamp(a, off, off + dur); let x1 = clamp(b, off, off + dur);
  if (x1 - x0 < MIN_VIEW) {
    x1 = Math.min(off + dur, x0 + MIN_VIEW);
    x0 = x1 - MIN_VIEW;
  }
  return [x0, x1];
}

function onLaneMove(e) {
  if (!dr) {
    const h = laneHit(e);
    hoverTool(h, e);
    return;
  }
  if (buttonReleased(e)) { onLaneLost(); return; }   // 離したことが届いていない（state.js）
  if (dr.type === 'paint') { paintMove(e); return; }
  if (dr.type === 'move') { moveDrag(e); return; }
  if (dr.type !== 'range') return;
  const t = rows()[dr.row];
  if (!t || t.kind !== 'vocal') return;
  if (!dr.moved && Math.abs(e.clientX - dr.x0) <= 3) return;
  dr.moved = true;
  const r = lanes.getBoundingClientRect();
  const t1 = clamp(tvT(e.clientX - r.left), range[0], range[1]);
  [dr.a, dr.b] = clampRange(t, Math.min(dr.t0, t1), Math.max(dr.t0, t1));
  if (t.id === S.session?.current) {        // 編集中のトラックなら下もドラッグ中に追従する
    setViewTimeline(dr.a, dr.b);
    render();
  } else {
    renderTracks();
  }
}

/** 離したこと（pointerup）が届かなかった: 動かしていれば最後に見せていた位置で離したことにする。
 * 動かしていなければ何もしない（クリック = 編集対象の切り替え・再生位置の移動にはしない）。 */
function onLaneLost() {
  if (!dr) return;
  if (dr.moved) { onLaneUp(); return; }
  if (dr.type === 'move') lanes.style.cursor = '';
  dr = null;
}

function onLaneUp() {
  if (dr && dr.type === 'move') { endMove(); return; }
  if (dr && dr.type === 'paint') { endPaint(); return; }
  const d = dr;
  if (!d || d.type !== 'range') return;
  dr = null;
  const t = rows()[d.row];
  if (!t) { renderTracks(); return; }
  if (d.moved && t.kind === 'vocal') {
    if (t.id !== S.session?.current) selectTrack(t.id, { view: [d.a, d.b] });
    else render();
    return;
  }
  clickAt(t, d.t0);
}

// ---------------------------------------------------------------- ツール（はさみ・ミュート。承認済み 2026-10-03）
const r6 = (v) => Math.round(v * 1e6) / 1e6;
const secText = (v) => `${v.toFixed(2)} 秒`;
const CUT_NEAR_PX = 5;      // 切れ目に乗っているとみなす距離
const DBL_MS = 450;         // 切れ目のダブルクリックの間隔

/** トラックビューで働くはさみ・ミュート（'cut'・'mute'・null）。プラグインはクリップを DAW が決めるので働かない（メインと同じ）。 */
const clipTool = () => (!ARA && (S.tool === 'cut' || S.tool === 'mute') ? S.tool : null);

/** ホバー: ツールのカーソル・ツールチップ・はさみの縦線／ミュートの部分の枠（メインと鉛筆は今までどおり）。 */
function hoverTool(h, e) {
  lastHit = { inClip: h.inClip, move: h.move, t: h.t };
  tvHover = null;
  let tip = '';
  const ct = clipTool();
  if (ct === 'cut' && h.t && h.inClip) {
    const off = offsetOf(h.t);
    let v = h.tl;
    if (timeSnapOn(e)) v = snapTime(v, tvStep());
    const cut = (h.t.cuts || []).find((c) => Math.abs(tvX(off + c) - h.x) <= CUT_NEAR_PX);
    tvHover = { id: h.t.id, t: r6(v - off), cut: cut ?? null };
    tip = cut != null ? '切れ目: ダブルクリックでつなぐ' : 'クリックでここを分ける（Shift: グリッドに寄せない）';
  } else if (ct === 'mute' && h.t && h.inClip) {
    const k = pieceAt(h.t, h.tl);
    const p = k >= 0 ? pieces(h.t.cuts || [], h.t.duration_sec || 0)[k] : null;
    tvHover = p ? { id: h.t.id, piece: p } : null;
    if (p) tip = `クリックでこの部分を${covered(h.t.mutes || [], p[0], p[1]) ? '戻す' : '消す'}（なぞるとまとめて）`;
  } else if (!ct) {
    if (h.move) tip = 'ドラッグで位置をずらす（Shift: 細かく / Alt: 吸い付かない）';
  }
  applyCursor(lastHit);
  if (tvBody.title !== tip) tvBody.title = tip;   // SVG の title 属性は出ないので、外側の HTML に付ける
  paintOverlay();
}

/** ツールのカーソル: はさみ・ミュートはクリップの上だけ（CSS の cur-cut / cur-mute。外は矢印）、鉛筆はメインと同じ動きで矢印。 */
function applyCursor(h = lastHit) {
  if (!lanes || (dr && dr.type !== 'paint')) return;       // 位置・範囲のドラッグ中は触らない（grabbing のまま）
  const inClip = !!h?.inClip;
  const ct = clipTool();
  lanes.classList.toggle('cur-cut', ct === 'cut' && inClip);
  lanes.classList.toggle('cur-mute', ct === 'mute' && inClip);
  if (ct) lanes.style.cursor = '';
  else if (S.tool === 'draw') lanes.style.cursor = 'default';
  else lanes.style.cursor = h?.move ? 'grab' : h?.t && h.t.kind === 'vocal' && h.inClip ? 'pointer' : 'default';
}

/** 時刻 tl（タイムラインの秒）が入っている部分の番号（クリップの外は -1）。 */
function pieceAt(t, tl) {
  const loc = tl - offsetOf(t);
  const ps = pieces(t.cuts || [], t.duration_sec || 0);
  return ps.findIndex(([a, b]) => loc >= a && loc <= b);
}

/** 切れ目・消した部分の操作 1 つ: 見かけをすぐ変えて（mutate）、エンジンの呼び出しを順番待ちに入れ、返り値で置き換える。
 * 順番待ちの間に Ctrl+Z で外されたら見かけを戻す。 */
function clipEdit(label, ts, mutate, calls, done) {
  const before = ts.map((t) => ({ t, cuts: (t.cuts || []).slice(), mutes: (t.mutes || []).map((m) => m.slice()) }));
  mutate();
  afterLocal(ts);
  const cancel = () => {
    for (const b of before) { b.t.cuts = b.cuts; b.t.mutes = b.mutes; }
    afterLocal(ts);
  };
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      let r = null;
      for (const c of calls) r = await call(c.tool, { ...c.args, author: 'human' });
      if (r?.session) adoptSession(r.session);
      status(done);
      return true;
    } catch (err) {
      if (!await handleEngineError(err)) status(`${label}できなかった: ${err.message}`);
      await resync();
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      renderTracks();
      wake();
    }
  }, { label, cancel });
}

/** 見かけを変えた後の描き直し（上・下の斜線・再生中の音）。 */
function afterLocal(ts) {
  setGains();                       // 再生中なら消した区間を今から先の音にも当てる
  renderTracks();
  if (ts.some((t) => t.id === S.session?.current)) render();
}

/** はさみ: クリックで分ける・切れ目の上のダブルクリックでつなぐ（時間スナップ。Shift で外す。クリップ全体が対象）。 */
function cutDown(e, h) {
  const t = h.t;
  const off = offsetOf(t);
  const dur = t.duration_sec || 0;
  const now = performance.now();
  const dbl = lastCut.id === t.id && now - lastCut.t < DBL_MS && Math.abs(h.x - lastCut.x) < 8;
  lastCut = { id: t.id, t: now, x: h.x };
  const cut = (t.cuts || []).find((c) => Math.abs(tvX(off + c) - h.x) <= CUT_NEAR_PX);
  if (cut != null) {
    if (!dbl) { status(`${t.name}: 切れ目（${secText(cut)}）。ダブルクリックでつなぐ`); return; }
    lastCut = { id: null, t: 0, x: 0 };
    const j = joinAt(t, cut);
    clipEdit('クリップをつなぐ', [t], () => { t.cuts = j.cuts; t.mutes = j.mutes; },
      [{ tool: 'join_track', args: { track_id: t.id, sec: cut } }], `${t.name}: 切れ目をつないだ（${secText(cut)}）`);
    return;
  }
  const v = timeSnapOn(e) ? snapTime(h.tl, tvStep()) : h.tl;
  const sec = r6(v - off);
  if (sec < CUT_MIN_EDGE || sec > dur - CUT_MIN_EDGE) {
    status('クリップの端に近すぎる（両端から 20 ms 以上内側で分ける）');
    return;
  }
  clipEdit('クリップを分ける', [t], () => { t.cuts = normCuts([...(t.cuts || []), sec], dur); },
    [{ tool: 'split_track', args: { track_id: t.id, sec } }], `${t.name}: ${secText(sec)} で分けた（切れ目をダブルクリックでつなぐ）`);
}

/** ミュート: 部分を押したら、その部分の今の状態の反対（消す⇔戻す）を、なぞった部分すべてに当てる向きにする（行をまたいでよい）。 */
function muteDown(e, h) {
  const k = pieceAt(h.t, h.tl);
  if (k < 0) return;
  const [a, b] = pieces(h.t.cuts || [], h.t.duration_sec || 0)[k];
  const to = !covered(h.t.mutes || [], a, b);
  dr = { type: 'paint', to, ops: [], seen: new Set(), before: new Map(), moved: true, at: { x: e.clientX, y: e.clientY }, group: `paint-${Date.now()}` };
  paintAt(h.t, k, to);
  lanes.setPointerCapture(e.pointerId);
  paintOverlay();
}

function paintAt(t, k, to) {
  const key = `${t.id}:${k}`;
  if (dr.seen.has(key)) return;
  dr.seen.add(key);
  const [a, b] = pieces(t.cuts || [], t.duration_sec || 0)[k];
  if (covered(t.mutes || [], a, b) === to) return;
  if (!dr.before.has(t.id)) dr.before.set(t.id, { t, cuts: (t.cuts || []).slice(), mutes: (t.mutes || []).map((m) => m.slice()) });
  t.mutes = paintPiece(t, k, to);
  dr.ops.push({ id: t.id, name: t.name, a, b, to });
  afterLocal([t]);
}

/** なぞっている間: 通った部分を押した部分と同じ向きにする（速く動かして飛ばした分は、前の点との間を細かく見る）。 */
function paintMove(e) {
  const from = dr.at;
  const steps = Math.max(1, Math.ceil(Math.hypot(e.clientX - from.x, e.clientY - from.y) / 4));
  let last = null;
  for (let i = 1; i <= steps; i++) {
    const h = hitAt(from.x + (e.clientX - from.x) * i / steps, from.y + (e.clientY - from.y) * i / steps);
    if (!h.t || !h.inClip) continue;
    const k = pieceAt(h.t, h.tl);
    if (k >= 0) { paintAt(h.t, k, dr.to); last = { t: h.t, k }; }
  }
  dr.at = { x: e.clientX, y: e.clientY };
  if (last) {
    tvHover = { id: last.t.id, piece: pieces(last.t.cuts || [], last.t.duration_sec || 0)[last.k] };
    paintOverlay();
  }
}

/** 離した: 変わった部分を 1 つのまとまり（group = 取り消し 1 回）としてエンジンに当てる。 */
function endPaint() {
  const d = dr;
  dr = null;
  tvHover = null;
  if (!d.ops.length) { renderTracks(); return; }
  const ts = [...d.before.values()].map((b) => b.t);
  const cancel = () => { for (const b of d.before.values()) { b.t.cuts = b.cuts; b.t.mutes = b.mutes; } afterLocal(ts); };
  const to = d.to;
  const names = [...new Set(d.ops.map((o) => o.name))].join('・');
  const done = d.ops.length === 1
    ? `${names}: ${secText(d.ops[0].a)}〜${secText(d.ops[0].b)} を${to ? '消した' : '戻した'}（Ctrl+Z で戻る）`
    : `${names}: ${d.ops.length} か所を${to ? '消した' : '戻した'}（Ctrl+Z で 1 回で戻る）`;
  enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      let r = null;
      for (const o of d.ops) {
        r = await call('mute_track_range', { track_id: o.id, start_sec: o.a, end_sec: o.b, mute: o.to, group: d.group, author: 'human' });
      }
      if (r?.session) adoptSession(r.session);
      status(done);
      return true;
    } catch (err) {
      if (!await handleEngineError(err)) status(`ミュートできなかった: ${err.message}`);
      await resync();
      return false;
    } finally {
      S.busy = false;
      renderToolbar();
      render();
      renderTracks();
      wake();
    }
  }, { label: to ? '部分のミュート' : '部分を戻す', cancel });
  renderTracks();
}

// ルーラー（上下共通）: クリックで再生位置、ドラッグでループ
function onRulerDown(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  root.focus({ preventScroll: true });
  const r = ruler.getBoundingClientRect();
  const toT = (ev) => clamp(tvT(ev.clientX - r.left), range[0], range[1]);
  const x0 = e.clientX; const t0 = toT(e);
  let moved = false;
  ruler.setPointerCapture(e.pointerId);
  const mv = (ev) => {
    if (buttonReleased(ev)) { lost(); return; }          // 離したことが届いていない（state.js）
    if (Math.abs(ev.clientX - x0) > 3) moved = true;
    if (moved) {
      const t1 = toT(ev);
      S.loop = [Math.min(t0, t1), Math.max(t0, t1)];
      if (ARA) araLoopHold();
      render();
    }
  };
  const off = () => {
    ruler.removeEventListener('pointermove', mv);
    ruler.removeEventListener('pointerup', up);
    ruler.removeEventListener('pointercancel', up);
    ruler.removeEventListener('lostpointercapture', lost);
  };
  // 離したことが届かなかった: ループはドラッグで見せたまま。動かしていなければクリック扱いにしない
  const lost = () => { off(); render(); };
  const up = () => {
    off();
    if (!moved) {
      if (!ARA) S.loop = null;                     // プラグインのループは DAW のもの（クリックでは解除しない。解除はルーラーのメニュー）
      S.head = t0;
      if (ARA) araSeek({ song_sec: t0 });
      if (S.playing) stop();
    } else if (ARA && S.loop) {
      araLoop({ a: S.loop[0], b: S.loop[1] });      // ソングの秒（トラックビューの時間軸）
    }
    render();
  };
  ruler.addEventListener('pointermove', mv);
  ruler.addEventListener('pointerup', up);
  ruler.addEventListener('pointercancel', up);
  ruler.addEventListener('lostpointercapture', lost);
}

// 見出し: 名前のクリックで編集対象（表示範囲はそのまま）、M・S・ガイドのアイコン
function onHeadsClick(e) {
  if (suppressClick) { suppressClick = false; return; }     // 並び替えのドラッグの後
  const th = e.target.closest('.th');
  if (!th) return;
  if (e.target.closest('.vol, .pan')) return;                // 音量・パン（押したときに値を決める。名前のクリックではない）
  const t = S.tracks.find((x) => x.id === th.dataset.id);
  if (!t) return;
  const b = e.target.closest('button');
  if (b) {
    const act = b.dataset.act;
    if (act === 'm') toggle(t, 'mute');
    else if (act === 's') toggle(t, 'solo');
    else if (act === 'guide') setGuide(t.guide ? null : t.id);
    root.focus({ preventScroll: true });
    return;
  }
  if (t.kind === 'vocal') selectTrack(t.id);
  root.focus({ preventScroll: true });
}

async function toggle(t, key) {
  try {
    await setTrack(t.id, { [key]: !t[key] });
  } catch (err) {
    // 競合の読み直しは順番待ちの中で（当てている途中の編集と重ねない）
    const handled = (err.conflict || err.preparing) && await enqueue(() => handleEngineError(err));
    if (!handled) status(`${key === 'mute' ? 'ミュート' : 'ソロ'}できなかった: ${err.message}`);
  }
}

// ---------------------------------------------------------------- 音量・パン（見出しの 2 段目）
// M／S と同じ聴き比べの操作: その場で音に当て（再生中も）、session に保存する（session.js setMix）。
// 値を変えると見出しを作り直すので、ドラッグは window で追う。2 回押しは押下の間隔で見る（dblclick は作り直しで届かない）。

/** 音量・パンを当てる（変わらなければ何もしない）。エンジンへの保存の失敗は状態行に出す。 */
function mix(t, patch) {
  if (Object.keys(patch).every((k) => t[k] === patch[k])) return Promise.resolve(false);
  return setMix(t.id, patch).catch(async (err) => {
    const handled = (err.conflict || err.preparing) && await enqueue(() => handleEngineError(err));
    if (!handled) status(`音量・パンを保存できなかった: ${err.message}`);
    return false;
  });
}

/** 描き直しでフォーカスが外れないように、フォーカスしているスライダーを覚える。 */
function focusedMix() {
  const a = document.activeElement;
  if (!a || !heads.contains(a) || !a.dataset?.mix) return null;
  const id = a.closest('.th')?.dataset.id;
  return id ? { id, kind: a.dataset.mix } : null;
}

const mixEl = (id, kind) => heads.querySelector(`.th[data-id="${CSS.escape(id)}"] [data-mix="${kind}"]`);

/** ドラッグ中の値の吹き出し（スライダーのつまみ・ノブの上。x/y はトラックビューの中の位置）。 */
function showBub(text, x, y) {
  if (!bub) return;
  bub.hidden = false;
  bub.textContent = text;
  const w = bub.offsetWidth; const tw = tv.getBoundingClientRect().width;
  bub.style.left = `${Math.round(clamp(x, w / 2 + 2, Math.max(w / 2 + 2, tw - w / 2 - 2)))}px`;
  bub.style.top = `${Math.round(y)}px`;
}
function hideBub() { if (bub) bub.hidden = true; }

function showMixBub(t, kind) {
  const el = mixEl(t.id, kind);
  if (!el) return;
  const r = el.getBoundingClientRect(); const tr = tv.getBoundingClientRect();
  if (kind === 'vol') {
    const db = gainOf(t);
    showBub(`${fmtDb(db)} dB`, r.left + dbToPos(db) * r.width - tr.left, r.top - tr.top - 3);
  } else {
    showBub(fmtPan(panOf(t)), r.left + r.width / 2 - tr.left, r.top - tr.top - 3);
  }
}

function resetMix(t, kind) {
  if (kind === 'vol') { mix(t, { gain_db: 0 }); status(`${t.name}: 音量を 0 dB に戻した`); }
  else { mix(t, { pan: 0 }); status(`${t.name}: パンを中央に戻した`); }
}

function onMixDown(e) {
  if (e.button !== 0) return;
  const el = e.target.closest('.vol, .pan');
  if (!el) return;
  const t = S.tracks.find((x) => x.id === el.closest('.th')?.dataset.id);
  if (!t || mixDrag) return;
  e.preventDefault();
  closeMenu();
  el.focus({ preventScroll: true });
  const kind = el.classList.contains('vol') ? 'vol' : 'pan';
  const now = performance.now();
  const key = `${kind}:${t.id}`;
  const dbl = lastMix.key === key && now - lastMix.t < MIX_DBL_MS && Math.hypot(e.clientX - lastMix.x, e.clientY - lastMix.y) < 8;
  lastMix = { key, t: dbl ? 0 : now, x: e.clientX, y: e.clientY };
  if (dbl) { resetMix(t, kind); return; }
  mixDrag = { id: t.id, kind };
  let apply;
  let jump = false;                                       // つまみ以外を押した（その位置の値にする）
  if (kind === 'vol') {
    const r = el.getBoundingClientRect();
    // 押した所へ跳ばない: つまみの上ならそこからの差で、つまみ以外ならそこへ移してそこからの差で動かす（Shift で 1/10）
    let pos = dbToPos(gainOf(t));
    if (Math.abs(e.clientX - (r.left + pos * r.width)) > 6) { pos = clamp((e.clientX - r.left) / r.width, 0, 1); jump = true; }
    let last = e.clientX;
    apply = (ev) => {
      if (ev) {
        pos = clamp(pos + (ev.clientX - last) / r.width * (ev.shiftKey ? 0.1 : 1), 0, 1);
        last = ev.clientX;
      }
      let db = posToDb(pos);
      if (!(ev && ev.shiftKey) && db > -0.15 && db < 0.15) db = 0;     // 0 dB に吸い付く（Shift の間は吸い付かない）
      mix(t, { gain_db: db });
    };
  } else {
    // 上下（上 = 右）にドラッグ。横に動かしても効く。1 px = 1。Shift で 1/10。中央の ±2 に吸い付く
    let val = panUi(panOf(t)); let ly = e.clientY; let lx = e.clientX;
    apply = (ev) => {
      if (!ev) return;                                    // 押しただけでは変えない
      val = clamp(val + ((ly - ev.clientY) + (ev.clientX - lx)) * (ev.shiftKey ? 0.1 : 1), -100, 100);
      ly = ev.clientY; lx = ev.clientX;
      const fine = ev.shiftKey;
      mix(t, { pan: Math.abs(val) < 2 && !fine ? 0 : panFromUi(fine ? Math.round(val * 10) / 10 : Math.round(val)) });
    };
  }
  const refresh = () => { renderTracks(); showMixBub(t, kind); };
  const mv = (ev) => {
    if (buttonReleased(ev)) { up(); return; }            // 離したことが届いていない（state.js）
    apply(ev);
    refresh();
  };
  const up = () => {
    window.removeEventListener('pointermove', mv);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', up);
    mixDrag = null;
    hideBub();
    lastHeads = '';                                       // つまみの「ドラッグ中」の見た目を外す
    renderTracks();
  };
  window.addEventListener('pointermove', mv);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
  if (jump) apply(null);
  lastHeads = '';
  refresh();
}

/** キー: 音量は ←→（↑↓）で 0.5 dB・Shift で 0.1 dB・Home で 0 dB・End で −∞。パンは 5・Shift で 1・Home で中央。 */
function onMixKey(e) {
  const el = e.target.closest?.('.vol, .pan');
  if (!el || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = S.tracks.find((x) => x.id === el.closest('.th')?.dataset.id);
  if (!t) return;
  const dir = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
  if (el.classList.contains('vol')) {
    if (dir) mix(t, { gain_db: clamp(Math.round((gainOf(t) + dir * (e.shiftKey ? 0.1 : 0.5)) * 10) / 10, GAIN_MIN_DB, GAIN_MAX_DB) });
    else if (e.key === 'Home') mix(t, { gain_db: 0 });
    else if (e.key === 'End') mix(t, { gain_db: GAIN_MIN_DB });
    else return;
  } else if (dir) {
    mix(t, { pan: panFromUi(clamp(panUi(panOf(t)) + dir * (e.shiftKey ? 1 : 5), -100, 100)) });
  } else if (e.key === 'Home') {
    mix(t, { pan: 0 });
  } else return;
  e.preventDefault();
  e.stopPropagation();                                   // ←→ はノートの移動・再生位置のコマンドに渡さない
}

// ---------------------------------------------------------------- 見出しの幅の境目
function installHeadSize() {
  hsz.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    closeMenu();
    hsz.classList.add('on');
    const x0 = e.clientX; const w0 = headWidth();
    const mv = (ev) => {
      if (buttonReleased(ev)) { up(); return; }
      const w = setHeadWidth(Math.min(w0 + ev.clientX - x0, maxHeadW()));
      showBub(`${w} px`, w, 16);
    };
    const up = () => {
      hsz.classList.remove('on');
      hideBub();
      window.removeEventListener('pointermove', mv);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
    };
    window.addEventListener('pointermove', mv);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    showBub(`${w0} px`, w0, 16);
  });
  hsz.addEventListener('dblclick', () => {
    setHeadWidth(HEAD_W.DEF);
    status(`見出しの幅を既定（${HEAD_W.DEF} px）に戻した`);
  });
  hsz.addEventListener('keydown', (e) => {
    const d = { ArrowLeft: -10, ArrowRight: 10 }[e.key];
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (d) setHeadWidth(headWidth() + d);
    else if (e.key === 'Home') setHeadWidth(HEAD_W.DEF);
    else return;
    e.preventDefault();
    e.stopPropagation();
  });
}

// ---------------------------------------------------------------- 並び順（見出しのドラッグ。issue #38）
const ORDER_PX = 4;         // これだけ縦に動いたら並び替えのドラッグ（それまではクリック）

/** order（id の配列）の中で id を to 番目に動かした並び。 */
function moved(order, id, to) {
  const rest = order.filter((x) => x !== id);
  const i = clamp(to, 0, rest.length);
  return [...rest.slice(0, i), id, ...rest.slice(i)];
}

function onHeadsDown(e) {
  if (e.button !== 0 || e.target.closest('button, input, .vol, .pan')) return;
  const th = e.target.closest('.th');
  if (!th) return;
  const R = rows();
  const from = R.findIndex((t) => t.id === th.dataset.id);
  if (from < 0) return;
  // キャプチャは動かし始めてから（動かさずに離したら、名前・見出しのクリックのまま）
  hd = { id: th.dataset.id, y0: e.clientY, from, to: from, moved: false, base: R.map((t) => t.id),
    prev: pendingOrder, pointer: e.pointerId };
}

function onHeadsMove(e) {
  if (!hd) return;
  if (buttonReleased(e)) { onHeadsUp(); return; }        // 離したことが届いていない（state.js）
  if (!hd.moved && Math.abs(e.clientY - hd.y0) < ORDER_PX) return;
  if (!hd.moved) {
    hd.moved = true;
    heads.classList.add('ordering');
    closeMenu();
    try { heads.setPointerCapture(hd.pointer); } catch { /* もう離している */ }
  }
  // 行の外（上下の端）に出たら、その向きへスクロールする（トラックが多くて入り切らないとき）
  const br = tvBody.getBoundingClientRect();
  if (e.clientY < br.top + RH + 6) tvBody.scrollTop -= 8;
  else if (e.clientY > br.bottom - 6) tvBody.scrollTop += 8;
  // ポインタの下の行へ（ドラッグ中から並びを入れ替えて見せる = 離した後と同じ）
  const r = heads.getBoundingClientRect();
  const to = clamp(Math.floor((e.clientY - r.top) / TH), 0, hd.base.length - 1);
  if (to === hd.to && pendingOrder) return;
  hd.to = to;
  pendingOrder = moved(hd.base, hd.id, to);
  renderTracks();
}

function onHeadsUp() {
  const d = hd;
  if (!d) return;
  hd = null;
  heads.classList.remove('ordering');
  if (!d.moved) return;                                   // クリック（名前・ボタン）は click で
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 0);        // click が来なかったとき（行の外で離した）
  if (d.to === d.from) {
    pendingOrder = d.prev;
    lastHeads = '';
    renderTracks();
    return;
  }
  commitOrder(d.id, d.to, pendingOrder);
}

/** 並び順を確定する（見出しのドラッグを離したとき）。当たるまで見かけの並び（pendingOrder）を残す。 */
export function commitOrder(id, index, order = null) {
  const want = order || moved(rows().map((t) => t.id), id, index);
  pendingOrder = want;
  lastHeads = '';
  renderTracks();
  // まだ当たっていない間に Ctrl+Z で外したら、見かけの並びを消す（この並びのときだけ。後のドラッグは残す）
  const cancel = () => {
    if (pendingOrder === want) pendingOrder = null;
    lastHeads = '';
    renderTracks();
  };
  return enqueue(async () => {
    S.busy = true;
    renderToolbar();
    try {
      const r = await call('set_track', { track_id: id, index: want.indexOf(id), author: 'human' });
      if (pendingOrder === want) pendingOrder = null;
      adoptSession(r.session);
      return true;
    } catch (err) {
      if (pendingOrder === want) pendingOrder = null;
      status(`並び順を変えられなかった: ${err.message}`);
      await resync(err);
      return false;
    } finally {
      S.busy = false;
      lastHeads = '';
      renderToolbar();
      render();
      renderTracks();
      wake();
    }
  }, { label: 'トラックの順番', cancel });
}

// ---------------------------------------------------------------- 右クリックのメニュー（中身は menus.js）
function onHeadsContext(e) {
  e.preventDefault();
  const th = e.target.closest('.th');
  const t = th && S.tracks.find((x) => x.id === th.dataset.id);
  if (t) openTrackMenu(e, t);
}
/** レーン: クリップの上 = クリップのメニュー、外 = そのトラックのメニュー（見出しと同じ）。 */
function onLanesContext(e) {
  e.preventDefault();
  if (dr) return;
  const h = laneHit(e);
  if (!h.t) return;
  if (h.inClip) openClipMenu(e, h.t, ARA ? araToRep(h.t, h.tl) : h.tl); else openTrackMenu(e, h.t);
}

// ---------------------------------------------------------------- トラックの高さ（v3 §9）
export function trackHeight() { return TH; }

/** 全トラックの高さを h px に（28〜96）。anchorY（tvBody の中の y）の所のトラックが動かないようにスクロールを合わせる。 */
export function setTrackHeight(h, anchorY = null) {
  const nh = Math.round(clamp(h, TRACK_H.MIN, TRACK_H.MAX));
  if (nh === TH) return false;
  const top = tvBody ? tvBody.scrollTop : 0;
  const ay = anchorY == null ? 0 : anchorY;
  const oh = TH;
  TH = nh;
  renderTracks();
  if (tvBody) {
    // 行の中の位置（ルーラーの下から）を高さの比で伸ばす
    const inRows = Math.max(0, top + ay - RH);
    tvBody.scrollTop = Math.max(0, RH + inRows * (nh / oh) - ay);
  }
  return true;
}

// ---------------------------------------------------------------- 横の表示範囲（エディターと別。issue #39）
const TV_MIN_SPAN = 0.5;    // 上の表示範囲の最小（秒）

/** 上で見られる範囲（全体表示のときの範囲 = タイムライン＋後ろに 4% の余白）。 */
function worldRange() {
  const tl = timelineRange();
  return [Math.min(autoRange[0], tl[0]), Math.max(autoRange[1], tl[1] + (tl[1] - tl[0]) * 0.04)];
}
/** 表示範囲を見られる範囲に収める。全体より広くしたら null（全体表示 = 曲の長さに合わせて広がる）。 */
function clampTv(v) {
  const [a, b] = worldRange();
  if (!v || v.span >= b - a - 1e-6) return null;
  const span = Math.max(TV_MIN_SPAN, v.span);
  return { t0: clamp(v.t0, a, b - span), span };
}
function curView() { return tvView || { t0: range[0], span: range[1] - range[0] }; }

/** 横ズーム: x（レーンの中の px）の所の時刻を動かさずに幅を factor 倍。 */
export function zoomTracks(factor, x = laneW / 2) {
  const v = curView();
  const tA = tvT(clamp(x, 0, laneW));
  const span = v.span * factor;
  tvView = clampTv({ t0: tA - (tA - v.t0) * (span / v.span), span });
  renderTracks();
}
/** 横スクロール: dx px だけ右へ（負なら左へ）。全体表示のときは何もしない。 */
export function panTracks(dx) {
  if (!tvView) return;
  tvView = clampTv({ t0: tvView.t0 + dx / pps(), span: tvView.span });
  renderTracks();
}
/** 上の表示範囲（前回の表示として覚える。null = 全体表示）。 */
export function tracksView() { return tvView ? { ...tvView } : null; }
export function setTracksView(v) {
  tvView = v && v.span > 0 ? clampTv({ t0: +v.t0 || 0, span: +v.span }) : null;
  lastSig = '';
  renderTracks();
}

/** トラックビューのホイール（割り当ては下のエディターと共通。issue #27・#39。ポインタのある側に効く）:
 * 縦ズーム（既定 Ctrl）= トラックの高さ（ポインタの所のトラックを動かさない）、縦スクロール（既定 ホイールだけ）=
 * トラックの縦スクロール、横ズーム（既定 Ctrl+Shift）= 上の時間の幅（ポインタの下の時刻を中心に）、
 * 横スクロール（既定 Shift）= 上の横スクロール。修飾キー無しの横の量（タッチパッド）は横スクロール。
 * Ctrl のときはブラウザの拡大も止める。 */
function onTvWheel(e) {
  const mods = e.ctrlKey || e.metaKey || e.shiftKey || e.altKey;
  const act = wheelAction(e);
  if (!mods && Math.abs(e.deltaX) > Math.abs(e.deltaY)) {  // タッチパッドの横スクロール
    e.preventDefault();
    panTracks(e.deltaX * 0.6);
    saveView();
    return;
  }
  if (!mods && act === 'scroll-v') return;          // ふつうのホイール: ブラウザのまま（縦のスクロール）
  e.preventDefault();
  const d = mods ? (e.deltaY || e.deltaX) : e.deltaY;
  if (!d) return;
  if (act && e.altKey) window.api?.consumeAlt?.();
  const lr = lanes.getBoundingClientRect();
  if (act === 'zoom-v') {
    const r = tvBody.getBoundingClientRect();
    if (setTrackHeight(TH * (d > 0 ? 1 / 1.15 : 1.15), e.clientY - r.top)) saveView();
  } else if (act === 'scroll-v') {
    tvBody.scrollTop += d;
  } else if (act === 'zoom-h') {
    zoomTracks(d > 0 ? 1.15 : 1 / 1.15, e.clientX - lr.left);
    saveView();
  } else if (act === 'scroll-h') {
    panTracks(d * 0.6);
    saveView();
  }
}

// ---------------------------------------------------------------- 境界
function installSplit() {
  split.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    split.setPointerCapture(e.pointerId);
    split.classList.add('on');
    const y0 = e.clientY; const h0 = tvHeight();
    const mv = (ev) => {
      if (buttonReleased(ev)) { up(); return; }          // 離したことが届いていない（state.js）
      collapsed = false;
      userH = clamp(h0 + ev.clientY - y0, minH(), maxH());
      layout();
    };
    const up = () => {
      split.classList.remove('on');
      split.removeEventListener('pointermove', mv);
      split.removeEventListener('pointerup', up);
      split.removeEventListener('pointercancel', up);
      split.removeEventListener('lostpointercapture', up);
    };
    split.addEventListener('pointermove', mv);
    split.addEventListener('pointerup', up);
    split.addEventListener('pointercancel', up);
    split.addEventListener('lostpointercapture', up);
  });
  // ダブルクリックで上を 1 トラック分に畳む／戻す
  split.addEventListener('dblclick', () => {
    if (collapsed || tvHeight() <= minH() + 4) { collapsed = false; userH = null; } else collapsed = true;
    layout();
  });
}

// ---------------------------------------------------------------- 取り付け
export function installTracks(rootEl, { onViewChanged, onNewTake: newTake } = {}) {
  root = rootEl;
  tv = $('#tv');
  heads = $('#heads');
  lanes = $('#lanes');
  ruler = $('#tvRuler');
  split = $('#split');
  tvBody = $('#tvBody');
  hsz = $('#hsz');
  bub = $('#tvBub');
  saveView = onViewChanged || (() => {});
  onNewTake = newTake || (() => false);
  lanes.addEventListener('pointerdown', onLaneDown);
  lanes.addEventListener('pointermove', onLaneMove);
  lanes.addEventListener('pointerup', onLaneUp);
  lanes.addEventListener('pointercancel', onLaneUp);
  lanes.addEventListener('pointerleave', () => { if (dr) return; tvHover = null; lastHit = null; paintOverlay(); applyCursor(); });
  lanes.addEventListener('lostpointercapture', onLaneLost);   // pointerup の後は dr が無いので何もしない
  ruler.addEventListener('pointerdown', onRulerDown);
  heads.addEventListener('click', onHeadsClick);
  heads.addEventListener('pointerdown', onMixDown);
  heads.addEventListener('keydown', onMixKey);
  heads.addEventListener('pointerdown', onHeadsDown);
  heads.addEventListener('pointermove', onHeadsMove);
  window.addEventListener('pointerup', onHeadsUp);
  window.addEventListener('pointercancel', onHeadsUp);
  heads.addEventListener('lostpointercapture', onHeadsUp);
  heads.addEventListener('contextmenu', onHeadsContext);
  lanes.addEventListener('contextmenu', onLanesContext);
  ruler.addEventListener('contextmenu', openRulerMenu);
  installSplit();
  installHeadSize();
  tvBody.addEventListener('wheel', onTvWheel, { passive: false });
  onRender(renderTracks);
  onPlayhead(moveHead);
  onSession((sess) => {
    ensureOverviews();
    renderTracks();
    renderToolbar();
    adoptPrep((sess?.tracks || []).map((t) => [t.id, t.prep]), { session: true });
  });
  window.addEventListener('resize', () => { lastSig = ''; renderTracks(); });
}

/** テスト用: 上に描いているもの。 */
export function tracksState() {
  const frame = lanes?.querySelector('#tvFrame');
  const fr = frame ? {
    x: +frame.getAttribute('x'), w: +frame.getAttribute('width'),
    row: Math.floor(+frame.getAttribute('y') / TH),
  } : null;
  return {
    range: [...range], laneW, height: tvHeight(), collapsed, fit: fitH(), frame: fr, trackH: TH,
    view: tracksView(), headW: headWidth(), savedHeadW: savedHeadWidth(), order: rows().map((t) => t.id), pendingOrder: pendingOrder ? [...pendingOrder] : null,
    scrollTop: tvBody?.scrollTop || 0,
    clips: rows().map((t, i) => {
      const c = lanes?.querySelector(`[data-clip="${CSS.escape(t.id)}"]`);
      return { id: t.id, row: i, x: c ? +c.getAttribute('x') : null, w: c ? +c.getAttribute('width') : null,
        cuts: [...(t.cuts || [])], mutes: (t.mutes || []).map((m) => [...m]) };
    }),
    heads: [...(heads?.querySelectorAll('.th') || [])].map((el) => ({
      id: el.dataset.id, cur: el.classList.contains('cur'), off: el.classList.contains('off'),
      gain: +el.querySelector('.vol')?.getAttribute('aria-valuenow'), pan: +el.querySelector('.pan')?.getAttribute('aria-valuenow'),
      guide: el.querySelector('.g')?.getAttribute('aria-pressed') === 'true',
      prep: el.querySelector('.pp')?.dataset.state || null,
      prepTip: el.querySelector('.pp')?.title || null,
    })),
    overviews: overviews.size,
    headX: +(lanes?.querySelector('#tvph')?.getAttribute('transform') || 'translate(0').match(/translate\(([-\d.]+)/)?.[1] || 0,
    clock: fmtTime(S.head),
  };
}
