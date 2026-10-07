import { test, expect } from '@playwright/test';

// 再生位置への追従の規則（renderer/follow.js）。停止中は追従しない（DAW 側のロケートだけ 1 回寄せる）。
// 再生中に利用者が表示を動かしたら、再生位置が範囲に戻るか次の再生まで追従しない。
test('follower: stopped never follows except a locate; a manual move while playing holds until the head returns or play restarts', async () => {
  const { createFollower } = await import('../../renderer/follow.js');
  const f = createFollower();
  const view = { t0: 0, span: 10 };
  const check = (o) => f.check({ view: { ...view }, valid: true, ...o });

  // 停止中: 再生位置が表示の外でも、知らせが続いても動かさない
  for (let i = 0; i < 5; i++) expect(check({ playing: false, head: 50 })).toBe(false);
  // ロケート（再生位置が動いた知らせ）のときだけ寄せる。表示内なら寄せない
  expect(check({ playing: false, head: 50, locate: true })).toBe(true);
  expect(check({ playing: false, head: 5, locate: true })).toBe(false);
  // 編集中のトラックの外は寄せない
  expect(check({ playing: false, head: 50, locate: true, valid: false })).toBe(false);

  // 再生中: 表示の外に出たら寄せる
  expect(check({ playing: true, head: 5 })).toBe(false);
  expect(check({ playing: true, head: 11 })).toBe(true);
  view.t0 = 10; f.placed(view);                       // 追従が動かした（利用者の操作ではない）
  expect(check({ playing: true, head: 12 })).toBe(false);
  expect(check({ playing: true, head: 21 })).toBe(true);
  view.t0 = 20; f.placed(view);

  // 利用者が動かした（追従が置いた範囲と違う）: 再生位置が外でも寄せない。続けても寄せない
  view.t0 = 100;
  for (let i = 0; i < 5; i++) expect(check({ playing: true, head: 22 + i })).toBe(false);
  view.span = 4;                                      // ズームでも同じ
  expect(check({ playing: true, head: 30 })).toBe(false);
  // 再生位置が範囲に戻ってきたら再開: 次に出たら寄せる
  expect(check({ playing: true, head: 101 })).toBe(false);
  expect(check({ playing: true, head: 120 })).toBe(true);
  view.t0 = 119; f.placed(view);

  // 動かして止めた後、次の再生で再開する
  view.t0 = 300;
  expect(check({ playing: true, head: 121 })).toBe(false);          // 保持
  expect(check({ playing: false, head: 121 })).toBe(false);         // 停止
  expect(check({ playing: true, head: 121 })).toBe(true);           // 次の再生で再開（再生位置が外なら寄せる）
  view.t0 = 120; f.placed(view);
  expect(check({ playing: true, head: 122 })).toBe(false);
  // 全体表示に戻った（範囲が無くなった）
  f.forget();
  expect(check({ playing: true, head: 500 })).toBe(true);
});

// DAW の知らせ（ara.js の onPlayhead）→ 下のピアノロールの追従（draw.js の follow）。
test('ARA: the view stays where the user put it while stopped; a locate shows the new position once; a manual scroll while playing is not undone', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'performance');
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const oldRaf = globalThis.requestAnimationFrame;
  const oldCancel = globalThis.cancelAnimationFrame;
  let now = 0;
  Object.defineProperty(globalThis, 'performance', { configurable: true, value: { now: () => now } });
  globalThis.window = { api: { mode: 'ara', hostState: async () => ({ tracks: [{ track_id: 'vocal', regions: [
    { song_start: 0, song_end: 200, mod_start: 0, mod_end: 200 },
  ] }] }) }, dispatchEvent() {}, addEventListener() {} };
  globalThis.document = { documentElement: { dataset: {} }, querySelector: () => null, querySelectorAll: () => [], addEventListener() {} };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  try {
    const { onPlayhead, pullHostState, araSetHost } = await import('../../renderer/ara.js');
    const { S, invalidateWarp } = await import('../../renderer/state.js');
    const { G } = await import('../../renderer/grid.js');
    const draw = await import('../../renderer/draw.js');
    S.session = { current: 'vocal' };
    S.tracks = [{ id: 'vocal', kind: 'vocal', offset_sec: 0 }];
    S.off = 0; S.head = 0; S.playing = false; S.loop = null; S.drag = null;
    S.vd = { duration_sec: 200 };
    S.bounds = []; S.local.btime.clear(); invalidateWarp();   // 同じワーカーの前の試験の状態を残さない
    S.view = { t0: 0, span: 10 };
    G.follow = true;
    await pullHostState();
    araSetHost({ follow: (o) => { draw.follow(o); }, movePlayhead: () => {}, render: () => {}, renderToolbar: () => {} });
    let seq = 1_000_000;                              // 前の試験の通知の番号より後（古い番号の知らせは捨てられる）
    const send = (song, playing) => { now += 33; onPlayhead({ song_sec: song, sequence: ++seq, playing, stamp_ms: now, loop: null }); };
    const view = () => S.view.t0;
    // 再生中の位置は、通知の到着の遅れの補正（最大 80 ms）が入る
    const near = (x) => expect(Math.abs(view() - x)).toBeLessThan(0.2);

    // 停止中: 同じ位置の知らせが続いても、表示を動かしても、送り返されない
    send(5, false);
    expect(view()).toBe(0);
    send(50, false);                                  // DAW でロケート: 表示外なので 1 回だけ寄せる
    expect(view()).toBeCloseTo(49.8, 6);
    S.view = { t0: 0, span: 10 };                     // 利用者が表示を動かす
    for (let i = 0; i < 20; i++) send(50, false);     // 30 Hz の知らせ
    expect(view()).toBe(0);
    S.view = { t0: 120, span: 10 }; send(50, false);
    expect(view()).toBe(120);
    send(70, false);                                  // 別の位置へロケート: また 1 回寄せる
    expect(view()).toBeCloseTo(69.8, 6);
    send(75, false);                                  // 表示内へのロケートは動かさない
    expect(view()).toBeCloseTo(69.8, 6);
    G.follow = false;                                 // 追従がオフなら、ロケートでも動かさない
    send(150, false);
    expect(view()).toBeCloseTo(69.8, 6);
    G.follow = true;

    // 再生中: 表示の外へ出たら寄せる
    S.view = { t0: 70, span: 10 };
    send(78, true);
    expect(view()).toBe(70);
    send(80.5, true);
    near(80.3);
    // 利用者が動かしたら、再生位置が外でも戻されない
    S.view = { t0: 0, span: 10 };
    for (let i = 0; i < 10; i++) send(81 + i * 0.033, true);
    expect(view()).toBe(0);
    // 再生位置が表示に戻ってきたら再開（ループで戻った）
    send(5, true);
    expect(view()).toBe(0);
    send(12, true);
    near(11.8);

    // 動かして止め、次の再生で再開する
    S.view = { t0: 100, span: 10 };
    send(13, true);
    expect(view()).toBe(100);
    send(13.05, false);                               // 停止（見積もりと近い位置）: ロケートではない
    expect(view()).toBe(100);
    send(13.05, false);
    expect(view()).toBe(100);
    send(13.1, true);                                 // 次の再生
    near(12.9);
    // 再生から止まったとき、DAW が開始位置へ戻したら（ロケート）寄せる
    send(14, true);
    send(0.2, false);
    expect(view()).toBeCloseTo(0, 6);
  } finally {
    if (original) Object.defineProperty(globalThis, 'performance', original);
    else delete globalThis.performance;
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
    globalThis.requestAnimationFrame = oldRaf;
    globalThis.cancelAnimationFrame = oldCancel;
  }
});
