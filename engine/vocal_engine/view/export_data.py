# -*- coding: utf-8 -*-
"""`export_view_data` — 画面（Electron）の描画に必要な配列を JSON ファイルに書く。

**これは UI 向けであって LLM 向けではない。** MCP の結果には「生の数値列を入れない」
（MCP クライアントの制約）という決まりがあるので、ここでも配列は結果に載せず、
**JSON ファイルに書いてパスだけ返す**。読むのは Electron の main プロセス。
Claude Code などのエージェントがこのツールを呼ぶ理由は無い（要約が欲しいなら
`list_notes` / `get_pitch` / `list_deviations` を使うこと）。

書き出す中身:

| キー | 中身 |
|---|---|
| `waveform` | 波形のピーク列（既定 2 ms ごとの min / max） |
| `f0.take_midi` / `take_edited_midi` | テイクの F0（10 ms、半音単位。編集前と編集後） |
| `f0.take_edited_sec` | 編集後の時間軸に写した各フレームの秒 |
| `f0.guide_midi` / `guide_sec` | ガイドの F0（DTW でテイク時間に写像済み） |
| `notes` | ノート一覧（id・開始/終了・音名・編集前後の中心ピッチ・ピッチ編集対象外フラグ） |
| `boundaries` | 境界一覧（編集前の秒と編集後の秒） |
| `time_map` | 編集前の秒 → 編集後の秒 の区分線形写像（波形を引き伸ばして描くため） |
| `transitions` | つなぎ（接続された境目 `kind="boundary"` とノートの中の段差 `kind="step"`。なだらかさ・自動の幅・窓・移動量の段差）。`project/pitch.py` |
| `f0.draw_keep` | 鉛筆があるときだけ。フレームごとの Π(1 − w)（画面のプレビューが使う） |
| `f0.take_nodraw_midi` / `draws` | 鉛筆があるときだけ。鉛筆を当てる前の曲線と、線ごとの範囲（t0〜t1、つなぎ込み lo〜hi）。描き直しのプレビューが、覆われる前の線を外して描くのに使う |
| `notes[].edited` | 編集前と値が変わった区間がある（ずらし量が 0.1 セント以上か、編集後の位置・長さ・中の伸縮が変わった） |
| `f0.take_env` / `guide_env` | F0 のフレームごとの音量の包絡（RMS、0〜1）。ノートごとの小さな波形（blob）の太さ |
| `notes[].band_midi` / `guide_notes[].band_midi` | 帯（blob）を置く高さ = ノートの平均の音程（音量で重み付け、無声と子音は除く。v3 §3） |
| `notes[].muted` | 無音にした（`mute_notes`。mute が半分以上を覆う）。画面は帯を薄く描く |
| `notes[].fade_in_sec` / `fade_out_sec` | ノートのフェード（編集後の秒。ノートの長さに収めたもの。issue #20）。画面は帯をその分細く描く |
| `notes[].timing_corr` / `pitch_corr` | 補正の度合い（issue #37。`project/correction.py`）。補正が無ければ null。`{amount_ms / amount_cents, degree（0〜1）, manual（最後にかけたのが手動）}`。ピッチは `shape`（一定量のずらしでない = 鉛筆・曲線）も。基準は `correction_ref` |
| `phonemes.boundaries[].moved` | その音素の境目を `move_boundary` で動かした（`reset_to_original(boundary_ids=…)` で戻せる） |

ノートの中心（`edited_pitch_midi`・`edited_lo/hi_midi`・`cents`）は**基本の段**
（pitch_shift / pitch_curve）だけで測る。黄色の曲線（`take_edited_midi`）はつなぎ・鉛筆込み。

時間軸の作り方は `render/pipeline.py` の `Renderer.render_range` と同じ規則に揃えてある
（move は直前の隙間が吸収する／stretch はその区間の出力長を ratio 倍にする／
crop はその区間を出さない／silence はその時刻に無音を挟む）。編集後の秒は 1 µs に丸める
（画面が計画の節と突き合わせるため。0.1 ms に丸めると節の外に出てしまう）。
"""
import json
import os
import weakref

import numpy as np

from ..analysis.f0 import hz_to_midi, midi_to_name
from ..render.pipeline import MIN_GAP_MS, edits_to_segments
from ..project.correction import PITCH_REF_CENTS, TIMING_REF_MS

SCHEMA_VERSION = 1
DEFAULT_PEAK_MS = 2.0
PITCH_EDITABLE_KINDS = ("note",)
HATCH_KINDS = ("breath",)                  # 囁き・息 = ピッチ編集の対象外（斜線）


EDIT_EPS_CENTS = 0.1          # 「編集済み」の判定: これより小さいずらし量は変わっていない
EDIT_EPS_SEC = 1e-4

def _f(v, nd=4):
    """JSON に載せる float（NaN は None にする）。"""
    if v is None:
        return None
    v = float(v)
    if not np.isfinite(v):
        return None
    return round(v, nd)


def _arr(a, nd=3):
    a = np.asarray(a, dtype="float64")
    out = np.round(a, nd)
    if np.isfinite(out).all():
        return out.tolist()
    values = out.astype(object)
    values[~np.isfinite(out)] = None
    return values.tolist()


def build_time_map(segs, t0, t1):
    """編集前の秒 → 編集後の秒 の折れ点列。`Renderer.render_range` と同じ規則。"""
    segs = sorted([s for s in segs if not s.is_identity() and getattr(s, "fade", None) is None
                   and ((s.end_sec > t0 and s.start_sec < t1)
                        or (s.silence_sec > 0 and t0 <= s.start_sec <= t1))],
                  key=lambda s: (s.start_sec, s.end_sec))
    src = [t0]
    out = [t0]
    cursor = t0
    m_prev = 0.0
    pos = t0
    min_gap = MIN_GAP_MS / 1000.0
    for sg in segs:
        if sg.silence_sec > 0:
            # 無音の挿入: 同じ編集前の秒に 2 つの編集後の秒（左の極限／右の極限）
            t = sg.start_sec
            if t > cursor:
                pos += t - cursor
                cursor = t
            src.append(t)
            out.append(pos)
            pos += sg.silence_sec
            src.append(t)
            out.append(pos)
            continue
        s = max(t0, sg.start_sec)
        e = min(t1, sg.end_sec)
        if e <= s:
            continue
        gap_src = s - cursor
        gap_out = gap_src + (sg.move_ms - m_prev) / 1000.0
        if gap_src > 0:
            if gap_out < min_gap:
                gap_out = min(gap_src, min_gap)
            pos += gap_out
            src.append(s)
            out.append(pos)
        pos += (e - s) * sg.ratio
        src.append(e)
        out.append(pos)
        cursor = e
        m_prev = sg.move_ms
    tail_src = t1 - cursor
    if tail_src > 0:
        pos += max(tail_src - m_prev / 1000.0, 0.0)
        src.append(t1)
        out.append(pos)
    return np.asarray(src, dtype="float64"), np.asarray(out, dtype="float64")


def _map_time(src, out, t, side="right"):
    """編集前の秒 → 編集後の秒。

    無音を挟んだ時刻では値が 2 つある（挿入の前と後）。`side="left"` は手前
    （**ノートの尻**に使う）、`"right"` は後ろ（ノートの頭。np.interp の既定と同じ）。"""
    t = np.asarray(t, dtype="float64")
    v = np.interp(t, src, out)
    if side == "left":
        src = np.asarray(src, dtype="float64")
        idx = np.searchsorted(src, t, side="left")
        hit = (idx < len(src)) & (src[np.minimum(idx, len(src) - 1)] == t)
        v = np.where(hit, np.asarray(out, dtype="float64")[np.minimum(idx, len(src) - 1)], v)
    return v


def cents_offset(segs, times):
    """各フレームに乗るピッチのずらし量（セント）。"""
    times = np.asarray(times, dtype="float64")
    off = np.zeros(len(times))
    for sg in segs:
        if sg.is_identity():
            continue
        m = (times >= sg.start_sec) & (times < sg.end_sec)
        if not m.any():
            continue
        if sg.cents:
            off[m] += float(sg.cents)
        if sg.curve_points:
            pts = np.asarray(sg.curve_points, dtype="float64")
            off[m] += np.interp(times[m] - sg.start_sec, pts[:, 0], pts[:, 1])
    return off


def _center(n, off, hop):
    """ノートの中心の音程（編集後）= 解析の中心 + ノート内のずらし量の平均。

    画面の blob の中心線（`edited_pitch_midi`）と「ガイドに合わせる」の計画が
    同じ定義を使う（`current_note_pitches`）。"""
    if n.pitch_midi is None:
        return None
    a = max(0, int(round(n.start_sec / hop)))
    b = min(len(off), int(round(n.end_sec / hop)))
    return float(n.pitch_midi) + (float(np.mean(off[a:b])) if b > a else 0.0) / 100.0


def band_center(midi, env, a, b, exclude=None, fallback=None):
    """帯（blob）を置く高さ = フレーム [a, b) の平均の音程（v3 §3）。

    音量（包絡）で重みを付け、無声（NaN）と `exclude`（子音のフレーム）は除く。
    子音を除くと何も残らないときは子音も入れ、それでも無ければ `fallback`。
    音量が全部 0 なら重みなしの平均。"""
    a = max(0, int(a))
    b = min(len(midi), int(b))
    if b <= a:
        return fallback
    m = np.asarray(midi[a:b], dtype="float64")
    w = (np.ones(b - a) if env is None
         else np.clip(np.nan_to_num(np.asarray(env[a:b], dtype="float64")), 0.0, None))
    ok = np.isfinite(m)
    if exclude is not None:
        keep = ok & ~np.asarray(exclude[a:b], dtype=bool)
        if keep.any():
            ok = keep
    if not ok.any():
        return fallback
    mw = w[ok]
    if mw.sum() <= 1e-12:
        return float(np.mean(m[ok]))
    return float(np.sum(m[ok] * mw) / mw.sum())


def consonant_mask(pres, times):
    """フレームごとに子音の区間か（歌詞が無ければ None）。"""
    if pres is None or not pres.phonemes:
        return None
    times = np.asarray(times, dtype="float64")
    mask = np.zeros(len(times), dtype=bool)
    for p in pres.phonemes:
        if p.label == "consonant":
            mask |= (times >= p.start_sec) & (times < p.end_sec)
    return mask


def current_note_pitches(project):
    """{note id: 編集後の中心の音程（MIDI）}。"""
    f0r = project.take_f0
    segs = edits_to_segments(project.edits, project.edit_span)
    off = cents_offset(segs, f0r.times)
    return {n.id: _center(n, off, f0r.hop_s) for n in project.take_notes}


def _peaks(x, sr, t0, t1, peak_ms):
    """[t0, t1) を peak_ms ごとに min / max に間引く。"""
    a = max(0, int(round(t0 * sr)))
    b = min(len(x), int(round(t1 * sr)))
    seg = np.asarray(x[a:b], dtype="float64")
    n = max(1, int(round(peak_ms / 1000.0 * sr)))
    k = len(seg) // n
    if k == 0:
        return [], [], n / sr
    trimmed = seg[:k * n].reshape(k, n)
    return (np.round(trimmed.min(axis=1), 4).tolist(),
            np.round(trimmed.max(axis=1), 4).tolist(), n / sr)


ENV_WIN_HOPS = 2.0          # 包絡の窓 = フレームの間隔 × これ（10 ms → 20 ms。つぶつぶにならない幅）
ENV_REF_PCT = 98.0          # 包絡の 1.0 = 鳴っているフレームの RMS のこの百分位（1 回の大きな音で全体が細くならない）
ENV_ACTIVE = 0.05           # 「鳴っている」= 最大の RMS のこの割合以上（曲の大半が無音でも基準が下がらない）
_env_cache = {}


def envelope(x, sr, times, hop_sec):
    """各時刻を中心にした RMS（0〜1。鳴っているフレームの 98 百分位を 1 とし、それを超える分は 1）。

    画面はこれを F0 のフレームごとの blob の太さに使う（Melodyne の blob と同じ見せ方）。
    素材ごとに正規化するので、テイクとガイドで録音レベルが違っても形を見比べられる。"""
    times = np.asarray(times, dtype="float64")
    # キャッシュは同じ音声の配列（project.audio のキャッシュ）のときだけ使う。配列は弱参照で持つ
    # （強く持つと、プロジェクトを切り替えても音声が解放されない）
    try:
        src = weakref.ref(x)
    except TypeError:
        src = None
    key = (id(x), len(x), int(sr), len(times),
           float(times[0]) if len(times) else 0.0, float(hop_sec))
    hit = _env_cache.get(key)
    if hit is not None and src is not None and hit[0]() is x:
        return hit[1]
    x = np.asarray(x, dtype="float64")
    c = np.concatenate([[0.0], np.cumsum(x * x)])
    half = max(1, int(round(ENV_WIN_HOPS * hop_sec * sr / 2)))
    mid = np.round(times * sr).astype(np.int64)
    a = np.clip(mid - half, 0, len(x))
    b = np.clip(mid + half, 0, len(x))
    n = np.maximum(b - a, 1)
    rms = np.sqrt(np.maximum(c[b] - c[a], 0.0) / n)
    rms[b <= a] = 0.0
    top = float(rms.max()) if len(rms) else 0.0
    active = rms[rms >= ENV_ACTIVE * top]
    ref = float(np.percentile(active, ENV_REF_PCT)) if len(active) else 0.0
    env = np.clip(rms / ref, 0.0, 1.0) if ref > 1e-9 else np.zeros_like(rms)
    if len(_env_cache) > 8:
        _env_cache.clear()
    if src is not None:
        _env_cache[key] = (src, env)
    return env


def _pct(vals, q):
    if len(vals) == 0:
        return None
    return float(np.percentile(vals, q))


def _phoneme_block(project, pres, src_pts, out_pts, t0, t1):
    """音素レーン＋ピアノロールの境界線に要るもの（編集前と編集後の秒を両方）。"""
    if pres is None or not pres.phonemes:
        return None
    from ..phoneme.lyrics import hidden_estimated_syllable_indices
    hidden = hidden_estimated_syllable_indices(
        project.lyrics_entries("take"), pres.syllables, project.take_notes,
        project.estimated_excluded_note_ids())
    devs = []
    try:
        if project.guide is not None:
            devs, _ = project.boundary_deviations()
    except Exception:                          # noqa: BLE001
        devs = []
    dev_ms = {d.boundary_id: d.timing_ms for d in devs}
    moved = {e.params.get("boundary_id") for e in project.edits if e.kind == "move_boundary"}

    phs = []
    for p in pres.phonemes:
        if p.end_sec <= t0 or p.start_sec >= t1 or p.syllable_index in hidden:
            continue
        phs.append({
            "id": p.id, "index": p.index,
            "start_sec": _f(p.start_sec), "end_sec": _f(p.end_sec),
            "edited_start_sec": _f(float(_map_time(src_pts, out_pts, p.start_sec)), 6),
            "edited_end_sec": _f(float(_map_time(src_pts, out_pts, p.end_sec, "left")), 6),
            "text": p.text, "kana": p.kana, "romaji": p.romaji,
            "label": p.label, "detail": p.detail, "confidence": p.confidence,
            "stretchable": p.stretchable, "syllable_index": p.syllable_index,
            "flags": list(p.flags),
        })
    bds = []
    visible = {p.index for p in pres.phonemes if p.syllable_index not in hidden}
    for b in pres.boundaries:
        if (b.time_sec < t0 - 1e-9 or b.time_sec > t1 + 1e-9
                or (hidden and b.before_index is not None and b.before_index not in visible)
                or (hidden and b.after_index is not None and b.after_index not in visible)):
            continue
        bds.append({
            "id": b.id, "index": b.index, "sec": _f(b.time_sec),
            "edited_sec": _f(float(_map_time(src_pts, out_pts, b.time_sec)), 6),
            "kind": b.kind, "before_index": b.before_index, "after_index": b.after_index,
            "confidence": b.confidence, "movable": bool(b.movable),
            "deviation_ms": dev_ms.get(b.id),
            "moved": b.id in moved,           # move_boundary で動かした（右クリックで元に戻せる）
        })
    sylls = []
    for s in pres.syllables:
        if s["end_sec"] <= t0 or s["start_sec"] >= t1 or s["index"] in hidden:
            continue
        sylls.append(dict(
            s,
            edited_start_sec=_f(float(_map_time(src_pts, out_pts, s["start_sec"])), 6),
            edited_end_sec=_f(float(_map_time(src_pts, out_pts, s["end_sec"], "left")), 6)))
    return {
        "supported": True, "has_lyrics": True,
        "lyrics": pres.lyrics, "kana": pres.kana,
        "confidence": round(pres.confidence, 3),
        "aligner": pres.aligner, "g2p": pres.g2p,
        "phonemes": phs, "boundaries": bds, "syllables": sylls,
        "warnings": pres.warnings, "entries": list(pres.entries),
        "min_phoneme_ms": 20.0,
    }


def _no_phonemes(project, pres=None):
    """音素が無いときの `phonemes`。歌詞があるのにアラインが失敗したときは理由を入れる（issue #58）。

    全体の失敗（`phoneme_error`）と、すべての区間が失敗して音素が 1 つも無い結果の両方。"""
    err = project.phoneme_error("take") if project.has_lyrics("take") else None
    if err is None and pres is not None:
        fails = [w for w in pres.warnings if w.get("kind") == "align_failed"]
        if fails:
            err = fails[0].get("error") or fails[0].get("message")
    if err:
        return {"supported": True, "has_lyrics": False, "phonemes": [], "boundaries": [],
                "syllables": [], "error": err,
                "reason": "音素を切れなかった（音素なしで続けている）: %s" % err}
    return {"supported": True, "has_lyrics": False, "phonemes": [], "boundaries": [],
            "syllables": [],
            "reason": "歌詞がまだ無い。歌詞レーンをダブルクリックして入力すると"
                      "音素アラインメントが走る"}


MAX_PEAK_BINS = 20000           # 曲全体（158 秒）で JSON が太らないようにする上限
VIEW_KEEP = 8                   # cache/view/ に残す描画データの数（ガイドの付け外し・取り消しで戻る分）


VIEW_DATA_VERSION = 1  # 描画 JSON の形式・計算方法を変えたら上げる


def export_view_data(project, start_sec=None, end_sec=None, peak_ms=None,
                     path=None):
    """画面描画用の JSON を書いてパスを返す（UI 向け。LLM 向けではない）。

    `peak_ms` を省くと**範囲の長さから決める**（2 ms 刻みだと 158 秒の素材で
    JSON が 1.3 MB になり、編集のたびに書き直すには重い）。

    path を省くと `cache/view/<鍵>.json` に書き、**入力（`Project.view_key`）が同じなら前に書いたものを
    そのまま返す**（`cached: true`。issue #63。トラックの切り替え・ガイドの付け外しのたびに作り直していた）。
    書き出しの既定のパス（`export_default_path`）はファイルの有無で変わるので、結果に毎回入れる。
    **ここで新しい入力（セッション・別のファイルなど）を読むようにしたら、`Project.view_key` にも足すこと**
    （足さないと、入力が変わっても前に作ったものを返す）。
    """
    project.ensure_analyzed()
    if path is None:
        args = [start_sec, end_sec, peak_ms]
        key = project.view_key(*args)
        vdir = project.sub(os.path.join("cache", "view"))
        hit = _cached_view(vdir, key)
        if hit is not None:
            return dict(hit, cached=True, export_default_path=_export_default(project))
        out = _build(project, start_sec, end_sec, peak_ms)
        # 作る途中で読んだもの（音素など）があれば鍵が変わる: 作った後の鍵で残す
        key = project.view_key(*args)
        path = os.path.join(vdir, key + ".json")
        _write_json(path, out.pop("_data"))
        _write_json(path[:-5] + ".meta.json", out)
        _prune_views(vdir, keep=path)
        return dict(out, path=os.path.abspath(path), bytes=os.path.getsize(path), cached=False,
                    export_default_path=_export_default(project))
    out = _build(project, start_sec, end_sec, peak_ms)
    _write_json(path, out.pop("_data"))
    return dict(out, path=os.path.abspath(path), bytes=os.path.getsize(path), cached=False,
                export_default_path=_export_default(project))


def _cached_view(vdir, key):
    path = os.path.join(vdir, key + ".json")
    meta = path[:-5] + ".meta.json"
    try:
        with open(meta, encoding="utf-8") as f:
            out = json.load(f)
        size = os.path.getsize(path)
    except (OSError, ValueError):
        return None
    for p in (path, meta):
        try:
            os.utime(p, None)                   # 最近使ったものを残す（_prune_views）
        except OSError:
            pass
    return dict(out, path=os.path.abspath(path), bytes=size)


def _write_json(path, data):
    from ..project.store import _tmp_name, replace_file
    tmp = _tmp_name(path)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    replace_file(tmp, path)


def _prune_views(vdir, keep):
    try:
        names = [n for n in os.listdir(vdir) if n.endswith(".json") and not n.endswith(".meta.json")]
        paths = sorted((os.path.join(vdir, n) for n in names), key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for p in paths[VIEW_KEEP:]:
        if os.path.normcase(p) == os.path.normcase(keep):
            continue
        for q in (p, p[:-5] + ".meta.json"):
            try:
                os.remove(q)
            except OSError:
                pass


def _build(project, start_sec, end_sec, peak_ms):
    """描画データを作る。返り値の `_data` が JSON に書く中身、残りが結果の要約。"""
    dur = project.duration_sec
    t0 = 0.0 if start_sec is None else max(0.0, float(start_sec))
    t1 = dur if end_sec is None else min(dur, float(end_sec))
    if t1 <= t0:
        t1 = min(dur, t0 + 1.0)
    if peak_ms is None:
        peak_ms = max(DEFAULT_PEAK_MS, (t1 - t0) * 1000.0 / MAX_PEAK_BINS)

    f0r = project.take_f0
    notes = project.take_notes
    # 基本の段（pitch_shift / pitch_curve）と、つなぎ・鉛筆まで当てたもの（project/pitch.py）
    from ..project.pitch import pitch_model
    base_segs, segs, lay, trs = pitch_model(project)
    src_pts, out_pts = build_time_map(base_segs, 0.0, dur)

    times = f0r.times
    sel = (times >= t0) & (times < t1)
    midi = hz_to_midi(f0r.f0)
    midi = np.where(np.asarray(f0r.voiced, dtype=bool) & (np.asarray(f0r.f0) > 0),
                    midi, np.nan)
    off_base = cents_offset(base_segs, times)      # ノートの中心・高さはこちら
    off = cents_offset(segs, times)                # 描く曲線（つなぎ・鉛筆込み）
    edited_midi = midi + off / 100.0
    keep = None
    nodraw = None
    if lay.drs:
        keep = np.array([lay.keep(float(t)) for t in times[sel]])
        # 鉛筆を当てる前の曲線（基本の段＋つなぎ）。画面が描き直しのプレビューで、新しい線に
        # 覆われる前の線を外して描くのに使う（エンジンの draw_specs と同じ。issue #6）
        nodraw = midi[sel] + np.array([lay.at(float(t), "right", with_draws=False)
                                       for t in times[sel]]) / 100.0
    edited_sec = _map_time(src_pts, out_pts, times)

    x, sr = project.audio("take")
    wmin, wmax, bin_sec = _peaks(x, sr, t0, t1, peak_ms)

    # ---- ノート
    hop = f0r.hop_s
    take_env = envelope(x, sr, times, hop)
    # 音素（歌詞があるときだけ）。帯の高さから子音を除くのにも使う
    pres = project.phonemes("take") if project.has_lyrics("take") else None
    cons = consonant_mask(pres, times)
    base_midi = midi + off_base / 100.0
    out_notes = []
    mutes = [(float(e.target.start_sec), float(e.target.end_sec)) for e in project.edits
             if e.kind == "mute"]
    from ..project.fades import note_fades
    fades = note_fades(project, notes, (src_pts, out_pts))
    # 補正の度合いと手動／自動（issue #37）。画面は帯（タイミング）と線（ピッチ）の色に使う
    from ..project.correction import note_corrections
    corr = note_corrections(project, [n for n in notes if n.end_sec > t0 and n.start_sec < t1],
                            src_pts, out_pts, off_base, off, hop)
    for n in notes:
        if n.end_sec <= t0 or n.start_sec >= t1:
            continue
        a = max(0, int(round(n.start_sec / hop)))
        b = min(len(midi), int(round(n.end_sec / hop)))
        vals = midi[a:b]
        vals = vals[np.isfinite(vals)]
        evals = base_midi[a:b]
        evals = evals[np.isfinite(evals)]
        d = {
            "id": n.id,
            "start_sec": _f(n.start_sec),
            "end_sec": _f(n.end_sec),
            "edited_start_sec": _f(float(_map_time(src_pts, out_pts, n.start_sec)), 6),
            "edited_end_sec": _f(float(_map_time(src_pts, out_pts, n.end_sec, "left")), 6),
            "kind": n.kind,
            "label": n.label,
            "text": n.text,                    # 歌詞・音素（段階1では常に null）
            "note": n.note_name,
            "pitch_midi": _f(n.pitch_midi, 3),
            "edited_pitch_midi": _f(_center(n, off_base, hop), 3),
            "lo_midi": _f(_pct(vals, 10), 3),
            "hi_midi": _f(_pct(vals, 90), 3),
            "edited_lo_midi": _f(_pct(evals, 10), 3),
            "edited_hi_midi": _f(_pct(evals, 90), 3),
            # 帯（blob）の高さ: 平均の音程（音量で重み付け、無声・子音を除く。基本の段で測る）
            "band_midi": _f(band_center(base_midi, take_env, a, b, cons,
                                        _center(n, off_base, hop)), 3),
            "pitch_editable": n.kind in PITCH_EDITABLE_KINDS,
            "hatched": n.kind in HATCH_KINDS,
            "confidence": n.confidence,
        }
        # 編集済み = **編集前と値が変わった区間がある**ノート（ずらし量か、編集後の位置・長さ・中の伸縮）。
        # 以前は重なる Segment があるだけで立てていて、つなぎの窓の Segment がかかった隣も編集済みになった
        pv = off[a:b]
        pitch_changed = bool(np.any(np.abs(pv[np.isfinite(pv)]) > EDIT_EPS_CENTS)) if b > a else False
        es = float(_map_time(src_pts, out_pts, n.start_sec))
        ee = float(_map_time(src_pts, out_pts, n.end_sec, "left"))
        warp = edited_sec[a:b] - (times[a:b] + (es - n.start_sec))
        time_changed = (abs(es - n.start_sec) > EDIT_EPS_SEC or abs(ee - n.end_sec) > EDIT_EPS_SEC
                        or (b > a and float(np.max(np.abs(warp))) > EDIT_EPS_SEC))
        fd = fades.get(n.id)
        d["fade_in_sec"] = _f(fd[0], 6) if fd else 0.0
        d["fade_out_sec"] = _f(fd[1], 6) if fd else 0.0
        d["edited"] = bool(pitch_changed or time_changed or fd)
        # タイミングの編集で区間が細切れになっても二重に数えない（フレームの平均）
        d["cents"] = _f(float(np.mean(off_base[a:b])) if b > a else 0.0, 1)
        # 無音にした（mute が半分以上を覆う）: 画面は帯を薄く描く
        ln = n.end_sec - n.start_sec
        cov = sum(max(0.0, min(mb, n.end_sec) - max(ma, n.start_sec)) for ma, mb in mutes)
        d["muted"] = bool(ln > 0 and cov >= 0.5 * ln)
        c = corr.get(n.id) or {}
        d["timing_corr"] = c.get("timing")
        d["pitch_corr"] = c.get("pitch")
        out_notes.append(d)

    # ---- 隣との接続（画面はカーソルの形だけに使う。色や枠は足さない）
    try:
        from ..project.timing import block_connections
        by = {d["id"]: d for d in out_notes}
        for a, b, c, dflt in block_connections(project):      # 隣り合う区間（子音・息も。種類によらない）
            if a.id in by:
                by[a.id]["connected_next"] = bool(c)
            if b.id in by:
                by[b.id]["connected_prev"] = bool(c)
    except Exception:                          # noqa: BLE001
        pass

    # ---- 音素（段階2）。歌詞があればここに入る
    ph_block = _phoneme_block(project, pres, src_pts, out_pts, t0, t1)

    # ---- ノートに重なる音素を持たせる
    if pres is not None:
        from ..phoneme.lyrics import hidden_estimated_syllable_indices
        hidden = hidden_estimated_syllable_indices(
            project.lyrics_entries("take"), pres.syllables, notes,
            project.estimated_excluded_note_ids())
        for d in out_notes:
            ov = pres.overlapping(d["start_sec"], d["end_sec"])
            ov = [p for p in ov if p.syllable_index not in hidden]
            d["phonemes"] = [p.id for p in ov]
            d["text"] = "".join(p.kana for p in ov if p.kana) or None

    # ---- 境界。**音素があれば音素境界を優先**（音符境界とずれるときも音素が正）
    if ph_block and ph_block["boundaries"]:
        boundaries = [{"sec": b["sec"], "edited_sec": b["edited_sec"],
                       "kind": b["kind"], "id": b["id"]}
                      for b in ph_block["boundaries"]]
    else:
        edges = []
        for n in notes:
            if n.end_sec <= t0 or n.start_sec >= t1:
                continue
            edges.append(n.start_sec)
            edges.append(n.end_sec)
        edges = sorted(set(round(float(e), 4) for e in edges))
        boundaries = [{"sec": _f(e), "edited_sec": _f(float(_map_time(src_pts, out_pts, e)), 6),
                       "kind": "note", "id": None} for e in edges]

    data = {
        "schema_version": SCHEMA_VERSION,
        "for": "ui",
        "note": "UI 向け。LLM が読む想定ではない（要約は list_notes / get_pitch / list_deviations）",
        "project_dir": project.dir,
        "range_sec": [_f(t0), _f(t1)],
        "duration_sec": _f(dur),
        "sr": int(sr),
        "take": {"path": project.take["path"],
                 "name": os.path.basename(project.take["path"])},
        "guide": ({"path": project.guide["path"],
                   "name": os.path.basename(project.guide["path"])}
                  if project.guide else None),
        "waveform": {"t0_sec": _f(t0), "bin_sec": _f(bin_sec, 6),
                     "min": wmin, "max": wmax, "n": len(wmin)},
        "f0": {
            "hop_sec": _f(hop, 6),
            "t0_sec": _f(float(times[sel][0]) if sel.any() else t0),
            "take_midi": _arr(midi[sel]),
            "take_edited_midi": _arr(edited_midi[sel]),
            "take_edited_sec": _arr(edited_sec[sel], 5),
            "cents_offset": _arr(off[sel], 1),
            "take_env": _arr(take_env[sel]),
            **({"take_nodraw_midi": _arr(nodraw)} if nodraw is not None else {}),
        },
        # 鉛筆の線（範囲とつなぎの長さ）。画面の描き直しのプレビューが、覆われる線を知るのに使う
        "draws": [{"id": dr.id, "t0": _f(dr.t0, 6), "t1": _f(dr.t1, 6), "lo": _f(dr.lo, 6),
                   "hi": _f(dr.hi, 6)} for dr in lay.drs],
        # 接続された境目のつなぎ（画面がノートのドラッグ中・なだらかさのスライダー中に同じ式で描く）
        "transitions": [t.to_json() for t in trs if t.hi >= t0 and t.lo <= t1],
        "notes": out_notes,
        # 補正の度合いの基準（度合い 1 = この量。画面がドラッグ中に同じ式で色を決める。project/correction.py）
        "correction_ref": {"timing_ms": TIMING_REF_MS, "pitch_cents": PITCH_REF_CENTS},
        "boundaries": boundaries,
        "time_map": {"src_sec": _arr(src_pts, 6), "out_sec": _arr(out_pts, 6)},
        "lyrics": project.lyrics_text("take") or None,
        # 作ったときの値。ファイルの有無で変わるので、画面はツールの結果の値（毎回取り直す）を使う
        "export_default_path": _export_default(project),
        "lyrics_entries": project.lyrics_entries("take"),
        "phonemes": ph_block or _no_phonemes(project, pres),
        "edits": [dict(e.to_json(), describe=e.describe()) for e in project.edits],
        "history": {
            "changesets": [c.summary() for c in project.changesets],
            "can_undo": any(not c.undone for c in project.changesets),
            "can_redo": project.can_redo(),
        },
    }

    # ---- ガイド（テイクの時間に置いたもの。置き方は guide_basis）
    gf0 = project.guide_f0
    al = project.alignment
    if gf0 is not None and al is not None:
        # 描く位置は「ガイドの時刻 + 全体のずれ」（DTW で写した位置ではない。issue #12:
        # 写した位置はテイク自身のリズムに沿うので、リズムのずれが見えない）。発音の頭の組が
        # 作れない素材・ガイドの音声が読めないときは DTW の写像（`Project.guide_to_take`）
        g2t, basis = project.guide_to_take()
        data["guide_basis"] = basis
        info = getattr(al, "info", None) or {}
        if info.get("stage") == "fallback":
            # 対応付けに失敗して位置のまま重ねている（issue #32）。画面はこれをステータス行に出す
            data["guide_warning"] = info.get("note") or "ガイドとの対応付けに失敗した"
        gm = hz_to_midi(gf0.f0)
        gm = np.where(np.asarray(gf0.voiced, dtype=bool) & (np.asarray(gf0.f0) > 0),
                      gm, np.nan)
        gt = np.asarray(g2t(gf0.times), dtype="float64")
        gsel = (gt >= t0) & (gt < t1)
        data["f0"]["guide_midi"] = _arr(gm[gsel])
        data["f0"]["guide_sec"] = _arr(gt[gsel], 4)
        genv = None
        try:
            gx, gsr = project.audio("guide")
            genv = envelope(gx, gsr, gf0.times, gf0.hop_s)
            data["f0"]["guide_env"] = _arr(genv[gsel])
        except Exception:                      # noqa: BLE001  音声が読めなくても曲線は出す
            pass
        gnotes = []
        ghop = gf0.hop_s
        for g in project.guide_notes:
            gs = float(g2t(g.start_sec))
            ge = float(g2t(g.end_sec))
            if ge <= t0 or gs >= t1:
                continue
            a = max(0, int(round(g.start_sec / ghop)))
            b = min(len(gm), int(round(g.end_sec / ghop)))
            vals = gm[a:b]
            vals = vals[np.isfinite(vals)]
            gnotes.append({"id": g.id, "start_sec": _f(gs), "end_sec": _f(ge),
                           "guide_start_sec": _f(g.start_sec), "guide_end_sec": _f(g.end_sec),
                           "kind": g.kind, "note": g.note_name,
                           "pitch_midi": _f(g.pitch_midi, 3),
                           "lo_midi": _f(_pct(vals, 10), 3),
                           "hi_midi": _f(_pct(vals, 90), 3),
                           # 帯の高さ（テイクと同じ定義。ガイドは歌詞の音素を持たないので子音は除かない）
                           "band_midi": _f(band_center(gm, genv, a, b, None, g.pitch_midi), 3)})
        data["guide_notes"] = gnotes
        try:
            devs, meta = project.deviations()
            dmap = {d.note_id: d for d in devs}
            for d in out_notes:
                dv = dmap.get(d["id"])
                if dv is not None:
                    d["guide_note_id"] = dv.guide_note_id
                    d["guide_note"] = dv.guide_note
                    d["deviation_cents"] = dv.pitch_cents
                    d["deviation_ms"] = dv.timing_ms
            data["timing_detrend_ms"] = meta["timing_detrend_ms"]
        except Exception:                      # noqa: BLE001
            pass
    else:
        data["guide_notes"] = []

    if keep is not None:
        data["f0"]["draw_keep"] = _arr(keep, 4)

    return {
        "_data": data,
        "range_sec": [_f(t0), _f(t1)],
        "notes": len(out_notes),
        "boundaries": len(boundaries),
        "f0_frames": int(sel.sum()),
        "waveform_bins": len(wmin),
        "phonemes": len(ph_block["phonemes"]) if ph_block else 0,
        "guide": bool(gf0 is not None and al is not None),
        "hint": "UI 向けの JSON。配列は結果に入れていないので、このパスを画面側で読むこと。",
    }


def _export_default(project):
    """画面の「書き出し…」ダイアログの既定パス（命名の規則はエンジン側に 1 つだけ置く）。"""
    try:
        from ..render.export import default_path
        return default_path(project)
    except Exception:                          # noqa: BLE001
        return None


def note_name(m):
    return midi_to_name(m)
