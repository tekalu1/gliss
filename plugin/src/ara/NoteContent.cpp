#include "NoteContent.h"

#include <algorithm>
#include <cmath>

namespace gliss
{

namespace
{
constexpr double minimumLength = 1.0e-9;   // これより短く切れたノートは返さない

bool sameEvents (const std::vector<NoteEvent>& a, const std::vector<NoteEvent>& b) noexcept
{
    return std::equal (a.begin(), a.end(), b.begin(), b.end(), [] (const NoteEvent& x, const NoteEvent& y)
    {
        return juce::exactlyEqual (x.frequency, y.frequency) && x.pitchNumber == y.pitchNumber && juce::exactlyEqual (x.volume, y.volume)
               && juce::exactlyEqual (x.startPosition, y.startPosition) && juce::exactlyEqual (x.attackDuration, y.attackDuration)
               && juce::exactlyEqual (x.noteDuration, y.noteDuration) && juce::exactlyEqual (x.signalDuration, y.signalDuration);
    });
}

bool intersects (const NoteEvent& e, const std::optional<NoteTimeRange>& range) noexcept
{
    if (! range.has_value())
        return true;

    const auto end = e.startPosition + juce::jmax (e.noteDuration, e.signalDuration);

    if (range->duration <= 0.0)
        return e.startPosition <= range->start && end > range->start;

    return e.startPosition < range->start + range->duration && end > range->start;
}

std::vector<NoteEvent> filtered (const std::vector<NoteEvent>& events, const std::optional<NoteTimeRange>& range)
{
    std::vector<NoteEvent> out;
    out.reserve (events.size());

    for (const auto& e : events)
        if (intersects (e, range))
            out.push_back (e);

    return out;
}

std::vector<NoteEvent> parseEvents (const juce::var& list)
{
    std::vector<NoteEvent> out;

    if (const auto* array = list.getArray())
    {
        out.reserve ((size_t) array->size());

        for (const auto& n : *array)
        {
            const auto start = (double) n.getProperty ("start_sec", 0.0);
            const auto end = (double) n.getProperty ("end_sec", 0.0);
            const auto hz = (double) n.getProperty ("hz", 0.0);
            const auto midi = (double) n.getProperty ("midi", 0.0);

            if (! (end > start) || ! (hz > 0.0) || ! std::isfinite (start) || ! std::isfinite (end) || ! std::isfinite (midi))
                continue;

            NoteEvent e;
            e.frequency = (float) hz;
            e.pitchNumber = juce::roundToInt (midi);
            e.volume = (float) juce::jlimit (0.0, 1.0, (double) n.getProperty ("volume", 0.5));
            e.startPosition = start;
            e.noteDuration = end - start;
            e.signalDuration = end - start;
            out.push_back (e);
        }
    }

    std::stable_sort (out.begin(), out.end(), [] (const NoteEvent& a, const NoteEvent& b) { return a.startPosition < b.startPosition; });
    return out;
}
} // namespace

bool ModificationNotes::sameContent (const ModificationNotes& other) const noexcept
{
    return ready == other.ready && edited == other.edited && sameEvents (notes, other.notes);
}

bool ModificationNotes::sameSourceContent (const ModificationNotes& other) const noexcept
{
    return ready == other.ready && sameEvents (sourceNotes, other.sourceNotes);
}

namespace notes
{
std::map<juce::String, ModificationNotes> parse (const juce::var& result)
{
    std::map<juce::String, ModificationNotes> out;
    const auto* rows = result.getProperty ("notes", {}).getDynamicObject();

    if (rows == nullptr)
        return out;

    for (const auto& row : rows->getProperties())
    {
        if (! row.value.isObject())
            continue;

        ModificationNotes m;
        m.rev = row.value.getProperty ("rev", {}).toString();
        m.ready = row.value.getProperty ("state", {}).toString() == "ready";
        m.edited = (bool) row.value.getProperty ("edited", false);

        if (m.ready)
        {
            m.notes = parseEvents (row.value.getProperty ("notes", {}));
            m.sourceNotes = parseEvents (row.value.getProperty ("source_notes", {}));
        }

        out[row.name.toString()] = std::move (m);
    }

    return out;
}

NoteGrade grade (const ModificationNotes* m, bool forSource) noexcept
{
    if (m == nullptr || ! m->ready)
        return NoteGrade::initial;

    return (m->edited && ! forSource) ? NoteGrade::adjusted : NoteGrade::detected;
}

std::vector<NoteEvent> forModification (const ModificationNotes& m, const std::optional<NoteTimeRange>& range)
{
    return m.ready ? filtered (m.notes, range) : std::vector<NoteEvent> {};
}

std::vector<NoteEvent> forSource (const ModificationNotes& m, const std::optional<NoteTimeRange>& range)
{
    return m.ready ? filtered (m.sourceNotes, range) : std::vector<NoteEvent> {};
}

std::vector<NoteEvent> forRegion (const ModificationNotes& m, const RegionTimes& region, const std::optional<NoteTimeRange>& songRange)
{
    std::vector<NoteEvent> out;

    if (! m.ready)
        return out;

    const auto length = region.modLength();

    if (! (length > 0.0) || region.scale() <= 0.0)
        return out;

    const auto modStart = region.modStart;
    const auto modEnd = region.modStart + length;
    const auto scale = region.scale();
    const auto shift = region.songStart - region.modStart;

    for (const auto& e : m.notes)
    {
        const auto a = juce::jmax (e.startPosition, modStart);
        const auto noteEnd = juce::jmin (e.startPosition + e.noteDuration, modEnd);
        const auto signalEnd = juce::jmin (e.startPosition + e.signalDuration, modEnd);

        if (noteEnd - a < minimumLength)
            continue;

        NoteEvent r = e;
        if (! region.isStretched())
        {
            r.startPosition = a + shift;
            r.noteDuration = noteEnd - a;
            r.signalDuration = juce::jmax (signalEnd - a, r.noteDuration);
            r.attackDuration = juce::jlimit (0.0, r.noteDuration, e.attackDuration - (a - e.startPosition));
        }
        else
        {
            r.startPosition = region.songStart + (a - modStart) / scale;
            r.noteDuration = (noteEnd - a) / scale;
            r.signalDuration = juce::jmax ((signalEnd - a) / scale, r.noteDuration);
            r.attackDuration = juce::jlimit (0.0, r.noteDuration, (e.attackDuration - (a - e.startPosition)) / scale);
        }

        if (intersects (r, songRange))
            out.push_back (r);
    }

    return out;
}
} // namespace notes

} // namespace gliss
