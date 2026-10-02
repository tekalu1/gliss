# -*- coding: utf-8 -*-
"""音声認識の重みのダウンロード（利用者が確かめてから 1 回だけ）。

`<置き場>/<id>.partial/` に落とし、各ファイルの大きさと SHA-256 を確かめてから
`<置き場>/<id>/` へ名前を変える（途中のものを「入っている」と見なさない）。
取り消したら途中のファイルは消す。通信が切れたときは、確かめ終わったファイルだけ残して次に続きから。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request

from .. import __version__, log
from .catalog import AsrModel

CHUNK = 1 << 20
MANIFEST = "gliss-model.json"


class DownloadCancelled(Exception):
    """利用者が取り消した。"""


class DownloadError(RuntimeError):
    pass


def _open_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Gliss/%s" % __version__})
    return urllib.request.urlopen(req, timeout=60)


class _FakeStream:
    """偽の認識器（テスト）の重み: 0 のバイト列を、決めた時間をかけて返す。"""

    def __init__(self, size, seconds):
        self.left = size
        self.step = max(1, size // 50)
        self.delay = max(0.0, float(seconds)) / 50

    def read(self, n):
        if self.left <= 0:
            return b""
        k = min(n, self.step, self.left)
        self.left -= k
        time.sleep(self.delay)
        return b"\0" * k

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_for(model):
    fake = model.extra.get("fake")
    if fake is not None:
        if fake.get("download_error"):
            def fail(_url):
                raise DownloadError(fake["download_error"])
            return fail
        return lambda _url: _FakeStream(model.files[0].size, fake.get("download_sec", 1.0))
    return _open_url


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK), b""):
            h.update(buf)
    return h.hexdigest()


def download(model: AsrModel, root, progress=None, cancel=None, opener=None):
    """モデルを置き場に入れて、そのディレクトリを返す。入っていれば何もしない。

    progress(割合 0〜1): 例外を投げると中断する（ジョブの取り消し）。
    cancel: threading.Event。立ったら DownloadCancelled。
    """
    final = model.path(root)
    if model.installed(root):
        return final
    opener = opener or _opener_for(model)
    part = final + ".partial"
    os.makedirs(part, exist_ok=True)
    total = max(1, model.size)
    done = 0
    t0 = time.perf_counter()
    log.get("asr").info("音声認識のモデルを取得する: %s（%d バイト）→ %s", model.id, model.size, final)
    try:
        for f in model.files:
            target = os.path.join(part, f.name)
            if os.path.isfile(target) and os.path.getsize(target) == f.size:
                done += f.size                      # 前回確かめ終わったファイル（続きから）
                continue
            tmp = target + ".tmp"
            h = hashlib.sha256()
            got = 0
            try:
                with opener(model.url(f)) as r, open(tmp, "wb") as out:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled()
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        out.write(buf)
                        h.update(buf)
                        got += len(buf)
                        if progress is not None:
                            progress((done + got) / total)
            except BaseException:
                _remove(tmp)
                raise
            if got != f.size or (f.sha256 and h.hexdigest() != f.sha256):
                _remove(tmp)
                raise DownloadError("%s の中身が合わない（大きさ %d / %d バイト）。もう一度取得する"
                                    % (f.name, got, f.size))
            os.replace(tmp, target)
            done += f.size
        with open(os.path.join(part, MANIFEST), "w", encoding="utf-8") as fp:
            json.dump({"id": model.id, "repo": model.repo, "revision": model.revision,
                       "license": model.license, "license_url": model.license_url,
                       "source_url": model.source_url, "upstream": model.upstream,
                       "files": [{"name": f.name, "size": f.size, "sha256": f.sha256}
                                 for f in model.files],
                       "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      fp, ensure_ascii=False, indent=2)
        if os.path.isdir(final):
            shutil.rmtree(final, ignore_errors=True)
        os.replace(part, final)
    except BaseException as e:
        if _is_cancel(e):
            shutil.rmtree(part, ignore_errors=True)
            log.get("asr").info("取得を取り消した: %s", model.id)
        raise
    log.get("asr").info("取得した: %s（%.1f 秒）", model.id, time.perf_counter() - t0)
    return final


def _is_cancel(e):
    # ジョブの取り消し（mcp_server.JobCancelled）も同じ扱い。名前で見る（循環 import を避ける）
    return isinstance(e, DownloadCancelled) or type(e).__name__ == "JobCancelled"


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def remove(model: AsrModel, root):
    """入れたモデルを消す（テスト・やり直し用）。"""
    for d in (model.path(root), model.path(root) + ".partial"):
        shutil.rmtree(d, ignore_errors=True)
