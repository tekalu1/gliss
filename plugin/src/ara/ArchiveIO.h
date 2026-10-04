#pragma once

#include <juce_core/juce_core.h>

#include <map>

namespace gliss
{

/** 1 つの AudioModification の保存の中身。 */
struct ModificationArchive
{
    juce::String name;
    juce::var archive;   // エンジンの Project.to_archive()（編集リスト）。まだ無ければ void（null で書く）
};

/** DAW のソングに保存するもの（ARA のアーカイブ。docs/ara-plugin.md の「編集の単位・保存」）。解析のキャッシュは入れない（作業場所に持つ）。

    { "format": "gliss-ara", "version": 1,
      "document": { "work_key": "<作業場所の鍵>", "guide": "<ガイドの修飾の persistentID>" | null },
      "modifications": { "<persistentID>": { "name": "…", "archive": { …Project.to_archive()… } | null } } } */
struct DocumentArchive
{
    juce::String workKey;
    juce::String guide;
    std::map<juce::String, ModificationArchive> modifications;
};

namespace archive
{
constexpr auto formatName = "gliss-ara";

/** 形の版。下位互換でなくなったら上げ、ARA_DOCUMENT_ARCHIVE_ID も上げる。 */
constexpr int formatVersion = 1;

juce::String write (const DocumentArchive&);

/** 読めなければ false と理由。形の違うもの・新しすぎる版は読まない。 */
bool read (const juce::String& json, DocumentArchive& out, juce::String& error);

/** 新しいドキュメントの作業場所の鍵（UUID。英数字と - だけ）。 */
juce::String makeWorkKey();

/** エンジンの ara_open が受ける鍵か（英数字・-・_ の 1〜64 字）。 */
bool isValidWorkKey (const juce::String&);

/** ソースの WAV のファイル名に使う鍵（persistentID の FNV-1a 64 bit を 16 桁の 16 進で。ファイル名に使えない文字を避ける）。 */
juce::String sourceKey (const juce::String& persistentId);
} // namespace archive

} // namespace gliss
