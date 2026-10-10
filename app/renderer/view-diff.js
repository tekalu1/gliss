// 描画データの差分（`export_view_data(since=…)` の `ops`）を前の版に当てる。
// 正本はエンジンの `view_diff` / `apply_view_diff`（engine/vocal_engine/view/export_data.py。
// 形は engine/docs/MCP.md「描画データの差分」）。前の版は書き換えず、変わった道筋だけを写し、変わらない枝は共有する。

export function applyViewDiff(old, ops) {
  if (ops.$spans) {
    const out = old.slice();
    for (const [s, vals] of ops.$spans) {
      for (let i = 0; i < vals.length; i++) out[s + i] = vals[i];
    }
    return out;
  }
  if (ops.$ids) {
    const by = new Map(old.map((x) => [x.id, x]));
    for (const x of ops.$put) by.set(x.id, x);
    return ops.$ids.map((id) => by.get(id));
  }
  const out = { ...old };
  for (const k of ops.$del || []) delete out[k];
  Object.assign(out, ops.$set || {});
  for (const [k, sub] of Object.entries(ops.$sub || {})) out[k] = applyViewDiff(old[k], sub);
  return out;
}
