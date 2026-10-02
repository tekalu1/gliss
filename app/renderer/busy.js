// 処理中の見せ方（issue #63 の 4）。出し方は 2 通り。
//
//  - **線**（既定）: 0.3 秒を超えたら、ヘッダーの下端の細い線と矢印・砂時計のカーソル。操作は続けられる。
//    clipId / anchor を渡すと、対象（クリップ・聞き取りの区間）の上にも細い線を出す。
//  - **止める処理**（modal: true。開く・新規・書き出し・準備が終わる前に開いたトラック…）: 始まった瞬間から
//    画面全体に透明な覆いを掛けて、後ろのクリック・ドラッグ・キーを止める（Esc の取り消しは通す）。
//    0.2 秒を超えたら、覆いを暗くしてポップアップ（何をしているか・対象・段階・進み具合・経過の秒）を出す。
//    一度出したら最低 0.6 秒は残す（終わっていれば「終わった」の状態で。その間の入力は通す）。
//    2 つ重なったら後から始まったものを出し、前のものはその後ろで待つ（両方終わるまで覆いは外さない）。
//    覆いの上をクリックしたら、ポップアップを小さく揺らす（まだ出ていなければ、すぐ出す）。
const LINE_MS = 300;
const POP_MS = 200;
const MIN_POP_MS = 600;
let nextId = 0;
const tasks = new Map();
let linger = null;          // 終わったが、出してから 0.6 秒たつまで残しているもの
let lingerTimer = null;
let ticker = null;          // 経過の秒を進める
let popShown = null;        // ポップアップに出しているもの
let returnFocus = null;     // ポップアップを出す前のフォーカス（終わったら戻す）

const $ = (s) => document.querySelector(s);
const pct = (p) => `${Math.round(p * 100)}%`;

/** 止める処理が走っている（入力を止めている）。 */
export function isBlocking() {
  for (const t of tasks.values()) if (t.modal) return true;
  return false;
}

/** 止める処理のうち、前に出すもの（後から始まったもの）。 */
function topModal() {
  let top = null;
  for (const t of tasks.values()) if (t.modal) top = t;
  return top;
}

function paint() {
  const host = $('#mock');
  if (!host) return;
  const now = performance.now();
  const lineTask = [...tasks.values()].filter((t) => t.visible && !t.modal).at(-1) || null;
  const top = topModal();
  const pop = top?.visible ? top : (linger || null);
  const blocking = !!top;
  host.classList.toggle('is-busy', !!lineTask);
  paintLine(lineTask && !pop ? lineTask : null);
  paintClip(lineTask);
  const scrim = $('#busyScrim');
  scrim.hidden = !blocking && !pop;
  scrim.classList.toggle('shown', !!pop && !pop.done);
  scrim.classList.toggle('passive', !blocking);
  const center = $('#busyCenter');
  center.hidden = !pop;
  if (pop) paintPop(pop, now);
  if (pop !== popShown) {
    if (pop && !popShown && blocking) {
      const a = document.activeElement;
      if (a && !center.contains(a)) returnFocus = a;
      center.focus({ preventScroll: true });
    }
    popShown = pop;
  }
  if (!blocking) restoreFocus();
  if (pop && !pop.done && !ticker) ticker = setInterval(paint, 250);
  if ((!pop || pop.done) && ticker) { clearInterval(ticker); ticker = null; }
}

function restoreFocus() {
  const center = $('#busyCenter');
  const a = document.activeElement;
  const lost = !a || a === document.body || center.contains(a);
  if (returnFocus && lost) {
    const el = returnFocus.isConnected ? returnFocus : $('#mock');
    el?.focus?.({ preventScroll: true });
  }
  returnFocus = null;
}

function paintLine(task) {
  const line = $('#busyLine');
  line.hidden = !task;
  if (!task) return;
  const known = Number.isFinite(task.progress);
  $('#busyFill').style.width = known ? pct(task.progress) : '32%';
  line.classList.toggle('unknown', !known);
  line.setAttribute('aria-label', task.label);
  if (known) line.setAttribute('aria-valuenow', String(Math.round(task.progress * 100)));
  else line.removeAttribute('aria-valuenow');
}

function paintClip(task) {
  const clip = $('#busyClip');
  clip.hidden = true;
  const bar = task && barRect(task);
  if (!bar || !(bar.width > 0)) return;
  const known = Number.isFinite(task.progress);
  const base = $('#mock').getBoundingClientRect();
  clip.style.left = `${bar.left - base.left}px`;
  clip.style.top = `${bar.top - base.top}px`;
  clip.style.width = `${bar.width}px`;
  clip.firstElementChild.style.width = known ? pct(task.progress) : '32%';
  clip.classList.toggle('unknown', !known);
  clip.hidden = false;
}

function paintPop(task, now) {
  const known = Number.isFinite(task.progress);
  const done = !!task.done;
  $('#busyLabel').textContent = task.label;
  $('#busyPercent').textContent = !done && known ? pct(task.progress) : '';
  $('#busyTarget').textContent = task.target || '';
  const sec = Math.floor(((done ? task.doneAt : now) - task.start) / 1000);
  $('#busyStage').textContent = [done ? null : task.stage, `${sec} 秒`].filter(Boolean).join(' · ');
  const rail = $('#busyCenter .rail');
  rail.firstElementChild.style.width = done ? '100%' : known ? pct(task.progress) : '32%';
  rail.classList.toggle('unknown', !done && !known);
  const note = $('#busyNote');
  const giveUp = !!task.cancelOpts.button;       // 取り消しではなく「待つのをやめる」（engine.callJob）
  note.textContent = done ? (task.canceling ? (giveUp ? 'やめた' : '取り消した') : '終わった')
    : task.canceling ? (task.cancelOpts.note || '取り消している…') : '終わるまで編集できません';
  note.classList.toggle('done', done && !task.canceling);
  const cancel = $('#busyCancel');
  cancel.hidden = done || !task.cancel;
  cancel.disabled = !!task.canceling;
  const text = task.cancelOpts.button || '取り消し（Esc）';
  if (cancel.textContent !== text) cancel.textContent = text;
}

/** 対象の上に出す細い進み具合の位置（画面の座標 { left, top, width }）。
 * clipId: そのクリップの下端（issue #36）。anchor: 呼び出し側が決める（聞き取りの区間の上。issue #54）。 */
function barRect(task) {
  if (task.anchor) return task.anchor();
  if (!task.clipId) return null;
  const rect = [...document.querySelectorAll('#lanes [data-clip]')]
    .find((el) => el.getAttribute('data-clip') === task.clipId)?.getBoundingClientRect();
  return rect ? { left: rect.left, top: rect.bottom - 2, width: rect.width } : null;
}

function show(task) {
  if (task.visible || !tasks.has(task.id)) return;
  task.visible = true;
  if (task.modal) task.shownAt = performance.now();
  paint();
}

/** 処理を始める。label: 何をしているか（止める処理ではポップアップの見出し、線では読み上げの名前）。
 * target: 対象（ファイル名・トラック名。ポップアップに出す）。 */
export function beginBusy({ label = '処理している…', modal = false, clipId = null, anchor = null,
  target = '', stage = null } = {}) {
  const id = ++nextId;
  const task = { id, label, modal, clipId, anchor, target, stage, visible: false, progress: null, cancel: null,
    cancelOpts: {}, start: performance.now(), shownAt: null, done: false };
  tasks.set(id, task);
  const timer = setTimeout(() => show(task), modal ? POP_MS : LINE_MS);
  if (modal) paint();                  // 覆い（透明）はすぐ掛ける（残している「終わった」は、次が出るまでそのまま）
  return {
    /** 進み具合（0〜1。分からなければ null）と段階の名前（省けば前のまま）。 */
    update(progress, stageLabel) {
      task.progress = Number.isFinite(progress) ? progress : null;
      if (stageLabel !== undefined) task.stage = stageLabel || null;
      if (task.visible) paint();
    },
    /** 見出し・対象を変える（開くファイルが決まった・準備か読み込みかが分かった）。 */
    set(patch) {
      for (const k of ['label', 'target', 'stage']) if (patch[k] !== undefined) task[k] = patch[k];
      if (task.visible) paint();
    },
    /** 取り消し（Esc・ボタン）の中身。opts.button: ボタンの文言（既定「取り消し（Esc）」）、opts.note: 押した後の文言。 */
    cancelWith(fn, opts = {}) { task.cancel = fn; task.cancelOpts = fn ? opts : {}; if (task.visible) paint(); },
    finish() {
      clearTimeout(timer);
      if (!tasks.delete(id)) return;
      const now = performance.now();
      if (task.modal && task.shownAt !== null && now - task.shownAt < MIN_POP_MS && !topModal()) {
        task.done = true;
        task.doneAt = now;
        if (linger) dropLinger();
        linger = task;
        lingerTimer = setTimeout(() => { if (linger === task) { linger = null; paint(); } },
          task.shownAt + MIN_POP_MS - now);
      }
      paint();
    },
  };
}

function dropLinger() {
  clearTimeout(lingerTimer);
  linger = null;
}

/** 止める処理になるかもしれないもの（ジョブになったときだけ出す。analyze_take がキャッシュで済めば何も出さない）。
 * start() で初めて止める処理として始まる。finish() は始まっていなくても呼べる。 */
export function laterBusy(opts = {}) {
  let task = null;
  let ended = false;
  return {
    start() {
      if (!task && !ended) task = beginBusy({ ...opts, modal: true });
      return task;
    },
    get started() { return !!task; },
    set(patch) { Object.assign(opts, patch); task?.set(patch); },
    finish() { ended = true; task?.finish(); },
  };
}

/** 止まっていることを伝える（覆いの上のクリック・キー）。まだ出ていなければすぐ出す。 */
function nudge() {
  const top = topModal();
  if (!top) return;
  if (!top.visible) { show(top); return; }
  const el = $('#busyCenter');
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.classList.remove('nudge');
    void el.offsetWidth;
    el.classList.add('nudge');
    return;
  }
  el.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(-6px)' }, { transform: 'translateX(5px)' },
    { transform: 'translateX(-3px)' }, { transform: 'translateX(0)' }], { duration: 240, easing: 'ease-out' });
}

const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'CapsLock']);

export function installBusy() {
  const scrim = $('#busyScrim');
  $('#busyCancel').addEventListener('click', () => cancelBusy());
  // 覆いの上のポインターは、ほかの（window・document の）処理に渡さない
  const swallow = (e) => {
    if (!isBlocking() || !scrim.contains(e.target) || e.target.closest('#busyCancel')) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.type === 'pointerdown' && !e.target.closest('#busyCenter')) nudge();
  };
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'contextmenu']) {
    window.addEventListener(type, swallow, true);
  }
  window.addEventListener('wheel', (e) => {
    if (isBlocking()) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, { capture: true, passive: false });
  // キーは Esc（取り消し）と、ポップアップの中のボタンの操作（Tab・Enter・Space）だけ通す
  const onKey = (e) => {
    if (!isBlocking()) return;
    const onButton = document.activeElement === $('#busyCancel') && !$('#busyCancel').hidden;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (e.type === 'keydown') cancelBusy();
      return;
    }
    if (onButton && (e.key === 'Enter' || e.key === ' ')) {
      e.stopImmediatePropagation();      // ボタンを押す既定の動きだけ残す（Space の再生などには渡さない）
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.type !== 'keydown') return;
    if (e.key === 'Tab') {
      const btn = $('#busyCancel');
      (btn.hidden || btn.disabled ? $('#busyCenter') : btn).focus({ preventScroll: true });
      return;
    }
    if (!MODIFIERS.has(e.key) && !e.repeat) nudge();
  };
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('keyup', onKey, true);
  window.addEventListener('keypress', onKey, true);
}

/** 止める処理の取り消し（Esc・「取り消し」）。取り消しを頼んだら true。 */
export function cancelBusy() {
  const task = topModal();
  if (!task || !task.cancel || task.canceling) return false;
  task.canceling = true;
  if (!task.visible) show(task);
  Promise.resolve(task.cancel()).catch(() => { task.canceling = false; paint(); });
  paint();
  return true;
}

/** 止まっていることを伝える（覆いの外から来た操作。メニュー・ファイルのドロップ）。止めていれば true。 */
export function refuseWhileBlocking() {
  if (!isBlocking()) return false;
  nudge();
  return true;
}

/** テスト用: いま出しているもの。 */
export function busyState() {
  const top = topModal();
  return {
    blocking: !!top,
    tasks: [...tasks.values()].map((t) => ({ label: t.label, modal: t.modal, visible: t.visible })),
    popup: $('#busyCenter').hidden ? null : {
      label: $('#busyLabel').textContent, percent: $('#busyPercent').textContent,
      target: $('#busyTarget').textContent, stage: $('#busyStage').textContent,
      note: $('#busyNote').textContent, cancel: !$('#busyCancel').hidden,
      cancelText: $('#busyCancel').textContent, done: !!(popShown && popShown.done),
    },
    line: !$('#busyLine').hidden,
  };
}
