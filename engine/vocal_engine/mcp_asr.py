# -*- coding: utf-8 -*-
"""MCP: 区間の音声認識で歌詞を確かめる（issue #54。`asr/`）。

| ツール | 何をするか |
|---|---|
| `asr_status(model?)` | 使えるか（faster-whisper が入っているか）・理由・モデルの大きさ・ライセンス・保存先・入っているか |
| `prepare_asr_model(model?, background?)` | モデルを明示してダウンロードする（初回だけ。取り消せるジョブ） |
| `transcribe(start_sec, end_sec, source?, model?, background?)` | 区間を聞き取り、文字・かな・語ごとの時刻・確信度の**候補**を返す |

`transcribe` は確定の歌詞を書き換えない。取り込むのは `set_lyrics`（区間全体）か `set_note_syllable`（1 音節）。
重みが無いときに勝手にダウンロードしない（`code: "model_missing"` を返す）。

`mcp_server.py` の末尾から import される（`_tool` などはそちらのものを使う）。
"""
import threading

from . import asr
from . import mcp_server as _srv
from .asr import recognize
from .project import ProjectError

_ok = _srv._ok
_tool = _srv._tool
SYNC_MAX_SEC = 20.0              # 読み込み済みの GPU でこれ以下なら、その場で返す
_downloading = set()
_dl_lock = threading.Lock()


def _not_ready(m, root):
    """使えない・重みが無いときの返り値（無ければ None）。"""
    ok, reason = recognize.backend_available(m)
    if not ok:
        return {"ok": False, "code": "unavailable", "error": reason,
                "install_hint": recognize.INSTALL_HINT, "model": m.info(root)}
    if not m.installed(root):
        return {"ok": False, "code": "model_missing",
                "error": "音声認識のモデル %s が入っていない（%s）。prepare_asr_model で取得する"
                         % (m.id, asr.size_text(m.size)),
                "model": m.info(root), "next": "prepare_asr_model(model=%r)" % m.id}
    return None


@_tool(lock=False)
def asr_status(model: str = "auto", probe: bool = False) -> dict:
    """音声認識（聞き取り）が使えるか。理由・モデルの大きさ・ライセンス・保存先・入っているか。

    available=false のときは reason に理由（faster-whisper が入っていない など）。
    installed=false のときは prepare_asr_model で取得する（利用者に大きさとライセンスを示してから）。
    probe=true で GPU を使えるかも調べる（device: cuda / cpu、device_note: CPU になる理由。CPU は遅い）。
    """
    return _ok(**asr.status(model, probe=probe))


@_tool(lock=False)
def prepare_asr_model(model: str = "auto", background: bool = True) -> dict:
    """音声認識のモデルをダウンロードする（初回だけ。数 GB）。取り消せるジョブを返す。

    利用者が大きさ（asr_status の model.size_text）・保存先（model.path）・ライセンスを確かめてから呼ぶ。
    各ファイルの大きさと SHA-256 を確かめ、合わなければ使わない。入っていればすぐ返す。
    """
    m, root = asr.resolve(model)
    ok, reason = recognize.backend_available(m)
    if not ok:
        return {"ok": False, "code": "unavailable", "error": reason,
                "install_hint": recognize.INSTALL_HINT}
    if m.installed(root):
        return _ok(model=m.info(root), installed=True, path=m.path(root))
    with _dl_lock:
        if m.id in _downloading:
            return {"ok": False, "code": "busy", "error": "%s は取得中" % m.id}
        _downloading.add(m.id)

    def work(cancel=None, report=None, commit=None):
        try:
            path = asr.prepare(m, root, progress=report, cancel=cancel)
        except asr.download.DownloadCancelled:
            raise _srv.JobCancelled() from None
        finally:
            with _dl_lock:
                _downloading.discard(m.id)
        return {"model": m.info(root), "installed": True, "path": path}

    if background:
        return _srv._submit_job("prepare_asr_model", work, cancellable=True, locked=False)
    return _ok(**work())


@_tool
def transcribe(start_sec: float, end_sec: float, source: str = "take", model: str = "auto",
               background: bool = None) -> dict:
    """区間（編集前の秒）を音声認識し、歌詞の**候補**を返す。確定の歌詞は変えない。

    返り値: text（文字）・lyrics（set_lyrics に渡せる形）・kana（かなの読み）・
    words（語ごとの start_sec / end_sec / probability）・segments・
    confidence（語の確率の平均。calibrated=false = 正解率ではない）・warnings（要確認の理由）・
    current（その区間に今ある歌詞と候補の読みの違い: char_errors / char_error_rate / same）・
    device（cuda / cpu。cpu は遅い）。
    叫び・繰り返しは誤りが多い。候補を取り込むときは set_lyrics（区間全体）か set_note_syllable（1 音節）。
    モデルが無ければ code="model_missing"（prepare_asr_model で取得する。勝手には落とさない）。
    background: 省略時は、モデルの読み込みが要る・CPU・20 秒を超える区間ならジョブにする。
    """
    p = _srv._project()
    p.reload_if_changed()
    if source not in ("take", "guide"):
        raise ProjectError("source は take か guide")
    info = p.take if source == "take" else p.guide
    if info is None:
        raise ProjectError("%s の音声が無い" % source)
    dur = float(info.get("duration_sec") or p.duration_sec)
    t0 = max(0.0, float(start_sec))
    t1 = min(dur, float(end_sec))
    if t1 - t0 < 0.05:
        raise ProjectError("区間が短すぎる（start %.3f / end %.3f）" % (t0, t1))
    if t1 - t0 > asr.MAX_SEC:
        raise ProjectError("区間が長すぎる（%.1f 秒。%d 秒以下に分ける）" % (t1 - t0, asr.MAX_SEC))
    m, root = asr.resolve(model)
    bad = _not_ready(m, root)
    if bad:
        return bad
    x, sr = p.audio(source)
    seg = x[int(round(t0 * sr)):int(round(t1 * sr))].copy()
    current = [e for e in p.lyrics_entries(source)
               if e["start_sec"] is None or (e["start_sec"] < t1 and e["end_sec"] > t0)]

    def work(cancel=None, report=None, commit=None):
        segs, device, note, elapsed = recognize.run(m, m.path(root), seg, sr, progress=report)
        return asr.build_result(segs, t0, t1, m, device, note, elapsed, current)

    if background is None:
        ld = recognize.loaded(m.id)
        background = ld is None or ld["device"] != "cuda" or (t1 - t0) > SYNC_MAX_SEC
    if background:
        return _srv._submit_job("transcribe", work, cancellable=True, locked=False)
    return _ok(**work())


TOOLS = [asr_status, prepare_asr_model, transcribe]
