// 描画データの差分（app/renderer/view-diff.js・engine.js の viewData。engine/docs/MCP.md「描画データの差分」）
//   (D1) 数の列の区間・id の列・辞書のキーを当てる。前の版は書き換えず、変わらない枝は共有する
//   (D2) viewData は前の版を since で送り、差分を当てる。元が違う・覚えていない・範囲が違うときは全体を読む
import { test, expect } from '@playwright/test';
import { applyViewDiff } from '../../renderer/view-diff.js';

test('(D1) 区間・id の列・キーを当て、前の版を書き換えない', () => {
  const old = {
    f0: { midi: Array.from({ length: 100 }, (_, i) => 60 + i), n: 100 },
    notes: [{ id: 'a', m: 1 }, { id: 'b', m: 2 }, { id: 'c', m: 3 }],
    gone: 1, keep: { x: [1, 2] }, label: 'x',
  };
  const snapshot = JSON.parse(JSON.stringify(old));
  const ops = {
    $set: { label: 'y', added: [1] },
    $del: ['gone'],
    $sub: {
      f0: { $sub: { midi: { $spans: [[10, [null, null]], [99, [7]]] } } },
      notes: { $ids: ['c', 'a', 'd'], $put: [{ id: 'a', m: 9 }, { id: 'd', m: 4 }] },
    },
  };
  const out = applyViewDiff(old, ops);
  const want = JSON.parse(JSON.stringify(snapshot));
  want.f0.midi[10] = null; want.f0.midi[11] = null; want.f0.midi[99] = 7;
  want.notes = [{ id: 'c', m: 3 }, { id: 'a', m: 9 }, { id: 'd', m: 4 }];
  delete want.gone; want.label = 'y'; want.added = [1];
  expect(out).toEqual(want);
  expect(old).toEqual(snapshot);                        // 前の版はそのまま
  expect(out.keep).toBe(old.keep);                      // 変わらない枝は共有
  expect(out.notes[0]).toBe(old.notes[2]);
  expect(applyViewDiff(old, {})).toEqual(old);
});

test('(D2) viewData は since を送って差分を当て、合わなければ全体を読む', async () => {
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const files = new Map();
  const calls = [];
  let reply = null;
  globalThis.window = {
    api: {
      call: async (tool, args) => { calls.push({ tool, args }); return { ok: true, ...reply(args) }; },
      readJson: async (p) => {
        if (!files.has(p)) throw new Error(`no file ${p}`);
        return JSON.parse(JSON.stringify(files.get(p)));
      },
    },
    addEventListener: () => {},
  };
  globalThis.document = { documentElement: { dataset: {} }, querySelector: () => null, addEventListener: () => {} };
  try {
    const { viewData } = await import('../../renderer/engine.js');
    const v1 = { project_dir: 'P', notes: [{ id: 'a', m: 1 }, { id: 'b', m: 2 }], history: { can_undo: false } };
    const v2 = { ...v1, notes: [{ id: 'a', m: 5 }, { id: 'b', m: 2 }] };
    files.set('full1', v1).set('full2', v2);

    // 1 回目: 元が無いので since を送らない
    reply = () => ({ path: 'full1', view_rev: 'r1', diff: false, history: { can_undo: true } });
    const a = await viewData();
    expect(calls.at(-1).args).toEqual({});
    expect(a).toMatchObject({ ...v1, view_rev: 'r1', history: { can_undo: true } });

    // 2 回目: 差分を当てる。返した vd に足しても元は崩れない
    a.history.mutated = true; a._frameNote = 'cache';
    files.set('d12', { diff_from: 'r1', base_dir: 'P', ops: { $sub: { notes: { $ids: ['a', 'b'], $put: [{ id: 'a', m: 5 }] } } } });
    reply = () => ({ path: 'd12', full_path: 'full2', view_rev: 'r2', diff: true, since: 'r1' });
    const b = await viewData();
    expect(calls.at(-1).args).toEqual({ since: 'r1' });
    expect(b.notes).toEqual(v2.notes);
    expect(b._frameNote).toBeUndefined();
    expect(b.history).toEqual({ can_undo: false });

    // 元のプロジェクトが違う差分（別のトラックの同じ中身の版）は当てず、全体を読む
    files.set('dX', { diff_from: 'r2', base_dir: 'Q', ops: { $set: { notes: [] } } });
    files.set('fullQ', { ...v2, project_dir: 'Q' });
    reply = () => ({ path: 'dX', full_path: 'fullQ', view_rev: 'r2', diff: true, since: 'r2' });
    const c = await viewData();
    expect(c.project_dir).toBe('Q');
    expect(c.notes).toEqual(v2.notes);

    // 差分のファイルが読めなければ全体
    reply = () => ({ path: 'missing', full_path: 'full2', view_rev: 'r2', diff: true, since: 'r2' });
    expect((await viewData()).project_dir).toBe('P');

    // 範囲が違えば since を送らない
    reply = () => ({ path: 'full1', view_rev: 'r1', diff: false });
    await viewData({ start_sec: 1 });
    expect(calls.at(-1).args).toEqual({ start_sec: 1 });
  } finally {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
  }
});
