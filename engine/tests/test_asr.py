# -*- coding: utf-8 -*-
"""区間の音声認識（issue #54）: ダウンロード・候補の返り値・確定の歌詞を変えないこと。

認識器は偽物に差し替える（`GLISS_ASR_FAKE`・`set_recognizer_factory`）。実モデルを使う確認は
最後の 1 本だけで、faster-whisper かモデルが無ければ飛ばす。
"""
import hashlib
import json
import os
import threading
import time

import pytest

from vocal_engine import asr
from vocal_engine import mcp_server as m  # noqa: I001  先に読む（mcp_asr はその末尾から読まれる）
from vocal_engine import mcp_asr as ma
from vocal_engine.asr import catalog, download, recognize
from vocal_engine.config import asr_models_dir
from vocal_engine.project import Project

import materials as M
from conftest import TAKE, needs_clips


@pytest.fixture
def fake_asr(tmp_path, monkeypatch):
    """偽の認識器と、空の置き場。設定 JSON を書き換えれば動きを変えられる。"""
    cfg_path = tmp_path / "fake-asr.json"
    root = tmp_path / "asr-models"

    def configure(**cfg):
        cfg.setdefault("text", "さくらさくら")
        cfg.setdefault("delay_sec", 0.05)
        cfg.setdefault("download_sec", 0.1)
        cfg.setdefault("file_size", 200_000)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        recognize.set_recognizer_factory(None)
        return cfg

    monkeypatch.setenv("GLISS_ASR_FAKE", str(cfg_path))
    monkeypatch.setenv("GLISS_ASR_MODELS_DIR", str(root))
    configure()
    yield configure
    recognize.set_recognizer_factory(None)


@pytest.fixture
def opened(tmp_path):
    p = Project.open(TAKE, project_dir=str(tmp_path / "proj"))
    m._state["project"] = p
    yield p
    m._state["project"] = None


def _wait_job(r, timeout=30):
    assert r["ok"] and r["status"] == "running", r
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = m.get_job(r["job_id"])
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job が終わらない")


def test_models_dir_env_and_default(monkeypatch):
    monkeypatch.setenv("GLISS_ASR_MODELS_DIR", "X:/somewhere/asr")
    assert asr_models_dir() == os.path.normpath("X:/somewhere/asr")
    monkeypatch.delenv("GLISS_ASR_MODELS_DIR")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/u/AppData/Local")
    d = asr_models_dir()
    assert d.replace("\\", "/").endswith("Gliss/models/asr")


def test_catalog_pins_revision_size_and_license(monkeypatch):
    monkeypatch.delenv("GLISS_ASR_FAKE", raising=False)
    mdl = catalog.get("auto", "Z:/nowhere")
    assert mdl.id == catalog.DEFAULT_MODEL == "whisper-large-v3"
    assert mdl.license == "MIT" and len(mdl.revision) == 40
    assert all(f.sha256 and f.size > 0 for f in mdl.files)
    info = mdl.info("Z:/nowhere")
    assert info["installed"] is False and info["size_text"] == "3.09 GB"
    assert mdl.url(mdl.files[0]).startswith(
        "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/edaa852")
    with pytest.raises(KeyError):
        catalog.get("no-such-model")


def _local_model(tmp_path, monkeypatch, files):
    """file:// から落とす小さなモデル（大きさと SHA-256 を本物と同じ手順で確かめる）。"""
    src = tmp_path / "src"
    mdl_files = []
    for name, data in files.items():
        d = src / "test" / "tiny" / "resolve" / "r1"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        mdl_files.append(catalog.ModelFile(name, len(data), hashlib.sha256(data).hexdigest()))
    monkeypatch.setenv("GLISS_ASR_DOWNLOAD_BASE", src.as_uri())
    return catalog.AsrModel(id="tiny", label="tiny", backend="faster-whisper", repo="test/tiny",
                            revision="r1", files=tuple(mdl_files), license="MIT",
                            license_url="x", source_url="x")


def test_download_verifies_and_installs(tmp_path, monkeypatch):
    mdl = _local_model(tmp_path, monkeypatch, {"a.json": b"{}", "model.bin": os.urandom(3 << 20)})
    root = tmp_path / "root"
    seen = []
    path = download.download(mdl, str(root), progress=seen.append)
    assert path == mdl.path(str(root)) and mdl.installed(str(root))
    assert seen and seen[-1] == pytest.approx(1.0) and seen == sorted(seen)
    manifest = json.loads((root / "tiny" / download.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["revision"] == "r1" and manifest["license"] == "MIT"
    assert not (root / "tiny.partial").exists()
    # 2 回目は何もしない
    assert download.download(mdl, str(root), progress=lambda v: 1 / 0) == path


def test_download_rejects_wrong_hash(tmp_path, monkeypatch):
    mdl = _local_model(tmp_path, monkeypatch, {"model.bin": b"abc" * 1000})
    bad = catalog.AsrModel(**{**mdl.__dict__, "files": (catalog.ModelFile(
        "model.bin", 3000, "0" * 64),)})
    with pytest.raises(download.DownloadError):
        download.download(bad, str(tmp_path / "root"))
    assert not bad.installed(str(tmp_path / "root"))
    assert not (tmp_path / "root" / "tiny.partial" / "model.bin").exists()


def test_download_cancel_removes_partial(tmp_path, monkeypatch):
    mdl = _local_model(tmp_path, monkeypatch, {"a.json": b"{}", "model.bin": os.urandom(4 << 20)})
    cancel = threading.Event()

    def progress(v):
        if v > 0.3:
            cancel.set()
    with pytest.raises(download.DownloadCancelled):
        download.download(mdl, str(tmp_path / "root"), progress=progress, cancel=cancel)
    assert not (tmp_path / "root" / "tiny.partial").exists()
    assert not mdl.installed(str(tmp_path / "root"))


@needs_clips
def test_transcribe_needs_explicit_download_then_returns_candidate(fake_asr, opened):
    st = ma.asr_status()
    assert st["ok"] and st["available"] and not st["installed"]
    assert st["model"]["license"] == "MIT" and st["model"]["size_bytes"] == 200_000
    miss = ma.transcribe(0.2, 1.5)
    assert miss["ok"] is False and miss["code"] == "model_missing"
    assert "prepare_asr_model" in miss["next"]
    assert not os.path.exists(miss["model"]["path"])          # 勝手に落とさない

    job = _wait_job(ma.prepare_asr_model())
    assert job["status"] == "done" and job["installed"], job
    assert ma.asr_status()["installed"]

    m.set_lyrics("さくらさくろ", start_sec=0.1, end_sec=1.6, reanalyze=False, author="human")
    before = m.get_lyrics()["entries"]
    r = ma.transcribe(0.2, 1.5, background=False)
    assert r["ok"] and r["candidate"] is True, r
    assert r["text"] == "さくらさくら" and r["kana"] == "さくらさくら"
    assert r["lyrics"] == "さくらさくら"
    assert r["words"][0]["start_sec"] == pytest.approx(0.2, abs=1e-3)
    assert r["words"][-1]["end_sec"] == pytest.approx(1.5, abs=1e-3)
    assert r["confidence"]["calibrated"] is False and r["confidence"]["value"] == pytest.approx(0.9)
    assert r["current"]["kana"] == "さくらさくろ"
    assert r["current"]["char_errors"] == 1 and r["current"]["same"] is False
    assert m.get_lyrics()["entries"] == before                # 確定の歌詞は変えない


@needs_clips
def test_transcribe_job_progress_and_cancel(fake_asr, opened):
    fake_asr(delay_sec=1.0)
    _wait_job(ma.prepare_asr_model())
    r = ma.transcribe(0.2, 2.0)                                 # 読み込み前なのでジョブになる
    assert r["status"] == "running" and r["cancellable"]
    j = _wait_job(r)
    assert j["status"] == "done" and j["text"] == "さくらさくら" and j["progress"] == 1.0

    r = ma.transcribe(0.2, 2.0, background=True)
    time.sleep(0.25)
    assert m.cancel_job(r["job_id"])["ok"]
    assert _wait_job(r)["status"] == "canceled"


@needs_clips
def test_transcribe_does_not_block_other_tools(fake_asr, opened):
    fake_asr(delay_sec=1.5)
    _wait_job(ma.prepare_asr_model())
    r = ma.transcribe(0.2, 2.0, background=True)
    t0 = time.perf_counter()
    assert m.get_lyrics()["ok"]                                # エンジンのロックを握っていない
    assert time.perf_counter() - t0 < 0.5
    _wait_job(r)


@needs_clips
def test_download_job_can_be_cancelled(fake_asr, opened):
    fake_asr(download_sec=3.0)
    r = ma.prepare_asr_model()
    time.sleep(0.3)
    assert ma.prepare_asr_model()["code"] == "busy"
    assert m.cancel_job(r["job_id"])["ok"]
    assert _wait_job(r)["status"] == "canceled"
    st = ma.asr_status()
    assert not st["installed"]
    assert not os.path.exists(st["model"]["path"] + ".partial")


@needs_clips
def test_unavailable_backend_gives_reason(fake_asr, opened):
    fake_asr(unavailable="faster-whisper が入っていない（テスト）")
    st = ma.asr_status()
    assert st["available"] is False and "faster-whisper" in st["reason"]
    r = ma.transcribe(0.2, 1.0)
    assert r["ok"] is False and r["code"] == "unavailable"
    assert ma.prepare_asr_model()["code"] == "unavailable"


@needs_clips
def test_range_checks(fake_asr, opened):
    assert "短すぎる" in ma.transcribe(1.0, 1.01)["error"]
    assert "guide" in ma.transcribe(0.0, 1.0, source="guide")["error"]
    assert ma.transcribe(0.0, 1.0, source="both")["ok"] is False


def test_injected_recognizer_and_warnings(monkeypatch, tmp_path):
    """認識器の差し替え（set_recognizer_factory）と、要確認の知らせ。"""
    monkeypatch.delenv("GLISS_ASR_FAKE", raising=False)
    mdl = catalog.WHISPER_LARGE_V3

    class Rec:
        device = "cpu"
        device_note = "cuBLAS が無い"

        def __init__(self, *_):
            self.calls = 0

        def transcribe(self, y, progress=None):
            assert y.dtype.name == "float32" and abs(len(y) - 16000) <= 1
            progress(1.0)
            return [{"start": 0.0, "end": 1.0, "text": " どぱどぱどぱどぱ", "avg_logprob": -0.9,
                     "no_speech_prob": 0.7,
                     "words": [{"start": 0.0, "end": 0.5, "text": "どぱ", "probability": 0.3},
                               {"start": 0.5, "end": 1.0, "text": "どぱどぱどぱ",
                                "probability": 0.8}]}]

    recognize.set_recognizer_factory(Rec)
    try:
        import numpy as np
        segs, device, note, _ = recognize.run(mdl, str(tmp_path), np.zeros(44100), 44100,
                                              progress=lambda v: None)
        r = asr.build_result(segs, 10.0, 11.0, mdl, device, note, 0.1, [])
    finally:
        recognize.set_recognizer_factory(None)
    assert r["text"] == "どぱどぱどぱどぱ" and r["words"][0]["start_sec"] == 10.0
    joined = " / ".join(r["warnings"])
    assert "繰り返し" in joined and "幻覚" in joined and "確信の低い語: どぱ" in joined
    assert "CPU" in joined and "cuBLAS" in joined
    assert r["confidence"]["value"] == pytest.approx(0.55)
    assert r["current"]["entries"] == [] and r["current"]["same"] is None


def test_empty_result_warns():
    r = asr.build_result([], 0.0, 1.0, catalog.WHISPER_LARGE_V3, "cuda", None, 0.1)
    assert r["text"] == "" and r["kana"] is None and r["confidence"] is None
    assert any("聞き取れなかった" in w for w in r["warnings"])


def _real_model():
    try:
        import faster_whisper  # noqa: F401
    except Exception:          # noqa: BLE001
        return None
    mdl = catalog.WHISPER_LARGE_V3
    return mdl if mdl.installed() else None


@needs_clips
@pytest.mark.skipif(_real_model() is None or bool(os.environ.get("GLISS_ASR_FAKE")),
                    reason="faster-whisper か Whisper large-v3 の重みが無い（初回に画面から取得する）")
def test_real_model_reads_clip(tmp_path):
    """実モデル（Whisper large-v3）で C を聞き取る（正解のかなは materials.json の C.kana）。"""
    recognize.set_recognizer_factory(None)
    p = Project.open(TAKE, project_dir=str(tmp_path / "real"))
    m._state["project"] = p
    try:
        r = ma.transcribe(0.0, p.duration_sec, background=False)
    finally:
        m._state["project"] = None
    assert r["ok"], r
    truth = M.text("C.kana")
    errs = asr.distance(r["kana"], truth)
    assert errs / len(truth) < 0.45, (r["kana"], r["text"])
    assert r["words"] and all(0 <= w["start_sec"] <= w["end_sec"] <= p.duration_sec + 0.05
                              for w in r["words"])
    assert r["device"] in ("cuda", "cpu")
