#pragma once

#include <juce_core/juce_core.h>
#include <set>
#include <vector>

namespace gliss
{

/** DAW（エディタの EditorView）の選択が変わったとき、Gliss のエディタの編集対象（修飾）を切り替えるかを決める。
    ARA の型を使わない（単体テストにも入る）。docs/ara-plugin.md の「DAW の選択への追従」。

    - 開いている（見えている）エディタが 1 つでもあれば、その中の EditorView の知らせだけを受ける（隠れている・別の
      インスタンスのエディタの選択が、ドキュメントで共有する選択を上書きしない）。見えているエディタが 1 つも無ければ全部受ける
    - 前の知らせと選択の中身（リージョン・リージョン列）が同じなら無視する
    - 空の選択は何も変えない
    - 今の修飾のリージョンが選択に残っていれば、それを保つ（ほかのリージョンも選ばれていても、修飾を切り替えない）
    - 残っていなければ、選択の最初のリージョン。リージョンを選んでいなければ、リージョン列の中の再生位置に近いリージョン */
class SelectionPolicy
{
public:
    struct Region
    {
        juce::String id;            // リージョンの識別（同じ文書の中で変わらない）
        juce::String modification;  // 修飾の persistentID（無ければ空 = 候補にしない）
        double songStart = 0.0, songEnd = 0.0;
    };
    struct Sequence
    {
        juce::String id;
        std::vector<Region> regions;
    };
    struct Input
    {
        const void* view = nullptr;
        std::vector<Region> regions;      // 選んだリージョン（DAW が渡した順）
        std::vector<Sequence> sequences;  // 選んだリージョン列（DAW が渡した順）
        double playheadSec = 0.0;
    };
    enum class Kind { ignore, adopt };
    struct Decision
    {
        Kind kind = Kind::ignore;
        Region region;         // adopt のとき: 採るリージョン
        juce::String reason;   // ログ用の理由（hidden-editor・unchanged・empty・no-modification・same-region・current-kept・first-region・nearest-in-sequence・first-selection）
        juce::String signature;
    };

    /** 開いているエディタ（EditorView の識別）の出入り。 */
    void setViewShowing (const void* view, bool showing);

    Decision decide (const Input&);

    bool hasSelection() const noexcept { return has; }
    const juce::String& currentModification() const noexcept { return currentMod; }
    const juce::String& currentRegionId() const noexcept { return currentRegion; }
    int showingCount() const noexcept { return (int) showing.size(); }

    /** 選択の中身の署名（ログ・同じ選択の判定用）。 */
    static juce::String signatureOf (const Input&);

private:
    std::set<const void*> showing;
    juce::String lastSignature, currentMod, currentRegion;
    bool hasLast = false, has = false;
};

} // namespace gliss
