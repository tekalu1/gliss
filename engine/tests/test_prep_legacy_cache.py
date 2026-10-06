# -*- coding: utf-8 -*-
"""古い作業場所の音素のキャッシュでも、裏の準備が済む。

10-06 以前に作った作業場所は、今の写し `cache/take-phonemes.json` があり、鍵付きの保存
`cache/phonemes/take-<歌詞の鍵>.json` が無いことがある（印を書いた後に表が音素を取り直すと、次の準備が
「変わったキャッシュ」として鍵付きの保存を外していた）。裏の準備は今の写しを読むだけで鍵付きの保存を書かず、
準備済みの印（`write_stamp`）が「準備のキャッシュが欠けている」で失敗し続けていた。
"""
import glob
import os
import shutil

from test_prep import _ok, _open, _ready, _state, _until, env  # noqa: F401  env は fixture


def _fake_align(calls):
    from vocal_engine.phoneme.model import PhonemeResult

    def align(x, sr, entries, f0r=None, source="take", duration_sec=0.0, **_kw):
        calls.append([e["text"] for e in entries])
        text = "".join(e["text"] for e in entries)
        return PhonemeResult(source=source, lyrics=text, kana=text, phonemes=[], boundaries=[],
                             entries=list(entries), duration_sec=duration_sec)
    return align


def _prepared_with_lyrics(m, mt, prep, paths, sdir):
    """トラック b に歌詞を入れ、表で選んで解析した（今の写しと鍵付きの保存の両方がある）状態にする。"""
    r = _open(m, paths, sdir)
    t1 = r["session"]["current"]
    tb = _ok(mt.add_track(paths["b"]))["track"]
    _until(_ready(prep, t1, tb))
    _ok(mt.select_track(tb))
    _ok(m.set_lyrics("あ", start_sec=0.1, end_sec=0.5, reanalyze=False))
    _ok(m.analyze_take())
    _until(_ready(prep, tb))
    s = m._state["session"]
    pdir = s.project_dir_of(s.track(tb))
    assert os.path.exists(os.path.join(pdir, "cache", "take-phonemes.json"))
    assert glob.glob(os.path.join(pdir, "cache", "phonemes", "take-*.json"))
    _ok(mt.select_track(t1))
    return t1, tb, pdir


def _reopen(m, prep, paths, sdir):
    prep.reset()
    m._state.update(project=None, session=None, track=None)
    _open(m, paths, sdir)


def test_old_workdir_without_keyed_phonemes_gets_prepared(env, monkeypatch):
    """今の写しだけがあり鍵付きの保存が空: 裏の準備が今の写しから鍵付きの保存を作り、準備済みになる（計算し直さない）。"""
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.phoneme import analyze as PA
    calls = []
    monkeypatch.setattr(PA, "align_lyrics_ranges", _fake_align(calls))
    t1, tb, pdir = _prepared_with_lyrics(m, mt, prep, paths, sdir)
    n = len(calls)
    # 古い作業場所の形: cache/phonemes/ が空で、準備済みの印は前の版のファイルを指す
    shutil.rmtree(os.path.join(pdir, "cache", "phonemes"))
    os.mkdir(os.path.join(pdir, "cache", "phonemes"))
    _reopen(m, prep, paths, sdir)
    st = _until(lambda: (lambda x: x if x and x["state"] in ("ready", "failed") else None)(_state(prep, tb)))
    assert st["state"] == "ready", st.get("error")
    assert glob.glob(os.path.join(pdir, "cache", "phonemes", "take-*.json"))
    assert len(calls) == n                       # 音素は今の写しから（アラインし直さない）
    s = m._state["session"]
    assert prep.is_ready(s, s.track(tb))


def test_reprepare_keeps_keyed_phonemes_updated_by_front(env, monkeypatch):
    """印を書いた後に表が音素を取り直した（鍵付きの保存の中身が変わった）: 次の準備はそれを外さない。"""
    m, mt, prep, fake, paths, sdir = env
    from vocal_engine.phoneme import analyze as PA
    from vocal_engine.project.store import Project
    calls = []
    monkeypatch.setattr(PA, "align_lyrics_ranges", _fake_align(calls))
    t1, tb, pdir = _prepared_with_lyrics(m, mt, prep, paths, sdir)
    [keyed] = glob.glob(os.path.join(pdir, "cache", "phonemes", "take-*.json"))
    # 表（別の Project）が同じ歌詞で取り直す: 鍵付きの保存の中身が変わり、印の内容ハッシュと合わなくなる
    q = Project(pdir).load()
    q.analyze_phonemes("take", force=True)
    with open(keyed, "a", encoding="utf-8") as f:
        f.write(" ")                             # 同じ大きさで書き直されても見分けられるよう、大きさも変える
    s = m._state["session"]
    assert not prep.is_ready(s, s.track(tb))
    _reopen(m, prep, paths, sdir)
    st = _until(lambda: (lambda x: x if x and x["state"] in ("ready", "failed") else None)(_state(prep, tb)))
    assert st["state"] == "ready", st.get("error")
    with open(keyed, encoding="utf-8") as f:
        assert f.read().endswith(" ")            # 外して今の写しから作り直したのではなく、表の書いたもののまま
