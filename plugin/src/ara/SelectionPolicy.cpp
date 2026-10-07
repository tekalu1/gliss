#include "SelectionPolicy.h"

#include <limits>

namespace gliss
{

void SelectionPolicy::setViewShowing (const void* view, bool isShowing)
{
    if (isShowing)
        showing.insert (view);
    else
        showing.erase (view);
}

juce::String SelectionPolicy::signatureOf (const Input& in)
{
    juce::StringArray parts;

    for (const auto& r : in.regions)
        parts.add ("r:" + r.id);

    for (const auto& s : in.sequences)
        parts.add ("s:" + s.id);

    return parts.joinIntoString (",");
}

SelectionPolicy::Decision SelectionPolicy::decide (const Input& in)
{
    Decision d;
    d.signature = signatureOf (in);

    if (! showing.empty() && showing.find (in.view) == showing.end())
    {
        d.reason = "hidden-editor";
        return d;
    }

    if (d.signature.isEmpty())
    {
        d.reason = "empty";               // 空の選択は何も変えない（前の選択の記憶も、同じ選択かの判定も）
        return d;
    }

    if (hasLast && d.signature == lastSignature)
    {
        d.reason = "unchanged";
        return d;
    }

    hasLast = true;
    lastSignature = d.signature;

    const auto usable = [] (const Region& r) { return r.modification.isNotEmpty(); };
    const Region* chosen = nullptr;

    // 今の修飾のリージョンが選択（リージョン・リージョン列の中）に残っていれば、それを保つ
    if (has)
    {
        for (const auto& r : in.regions)
            if (usable (r) && r.modification == currentMod && (chosen == nullptr || r.id == currentRegion))
                chosen = &r;

        if (chosen == nullptr)
            for (const auto& s : in.sequences)
                for (const auto& r : s.regions)
                    if (usable (r) && r.modification == currentMod && (chosen == nullptr || r.id == currentRegion))
                        chosen = &r;

        if (chosen != nullptr)
            d.reason = "current-kept";
    }

    if (chosen == nullptr)
    {
        for (const auto& r : in.regions)
            if (usable (r)) { chosen = &r; d.reason = has ? "first-region" : "first-selection"; break; }
    }

    // リージョンを選んでいなければ、選んだ DAW のトラック（リージョン列）の、再生位置に近いリージョン
    if (chosen == nullptr)
    {
        for (const auto& s : in.sequences)
        {
            double best = std::numeric_limits<double>::max();

            for (const auto& r : s.regions)
            {
                if (! usable (r))
                    continue;

                const auto distance = in.playheadSec < r.songStart ? r.songStart - in.playheadSec
                                    : (in.playheadSec > r.songEnd ? in.playheadSec - r.songEnd : 0.0);

                if (distance < best)
                {
                    best = distance;
                    chosen = &r;
                }
            }

            if (chosen != nullptr)
            {
                d.reason = has ? "nearest-in-sequence" : "first-selection";
                break;
            }
        }
    }

    if (chosen == nullptr)
    {
        d.reason = "no-modification";
        return d;
    }

    if (has && chosen->modification == currentMod && chosen->id == currentRegion)
    {
        d.reason = "same-region";
        return d;
    }

    d.kind = Kind::adopt;
    d.region = *chosen;
    has = true;
    currentMod = chosen->modification;
    currentRegion = chosen->id;
    return d;
}

} // namespace gliss
