#include "EngineCalls.h"

namespace gliss
{

namespace tools
{

bool isForbidden (const juce::String& tool)
{
    static const juce::StringArray forbidden { "new_project", "load_project", "open_project", "save_project", "close_project",
                                               "add_track", "remove_track", "export_wav", "render_tracks" };
    return forbidden.contains (tool) || tool.startsWith ("ara_");
}

bool shouldSyncAfter (const juce::String& tool, const juce::var& result)
{
    if (isFailure (result))
        return false;

    if (tool == "get_job")
    {
        const auto status = result.getProperty ("status", {}).toString();
        return status == "done" || status == "finished" || status == "error" || status == "cancelled";
    }

    static const juce::StringArray readOnly { "export_view_data", "track_overview", "prep_status", "engine_info", "asr_status",
                                              "project_status", "plan_edit", "render_audition", "inspect_lyrics_score",
                                              "cancel_job", "render_region", "render_preview", "render_view", "remeasure",
                                              "transcribe", "pause_prep" };
    return ! (readOnly.contains (tool) || tool.startsWith ("list_") || tool.startsWith ("get_"));
}

} // namespace tools

std::optional<TestEdit> TestEdit::parse (const juce::String& text)
{
    const auto trimmed = text.trim();

    if (trimmed.isEmpty())
        return std::nullopt;

    if (trimmed.startsWithChar ('{'))
    {
        const auto json = juce::JSON::parse (trimmed);

        if (! json.isObject())
            return std::nullopt;

        if (json.hasProperty ("tool"))
        {
            TestEdit e { json.getProperty ("tool", {}).toString(), json.getProperty ("args", {}) };

            if (e.tool.isEmpty())
                return std::nullopt;

            if (! e.args.isObject())
                e.args = juce::var (new juce::DynamicObject());

            return e;
        }

        return TestEdit { "shift_pitch", json };
    }

    // "shift_pitch:<note_id>:<cents>"
    const auto parts = juce::StringArray::fromTokens (trimmed, ":", "");

    if (parts.size() == 3 && parts[0] == "shift_pitch" && parts[2].containsOnly ("+-.0123456789"))
    {
        auto* args = new juce::DynamicObject();
        args->setProperty ("note_id", parts[1]);
        args->setProperty ("cents", parts[2].getDoubleValue());
        return TestEdit { "shift_pitch", juce::var (args) };
    }

    return std::nullopt;
}

bool isFailure (const juce::var& result)
{
    if (! result.isObject())
        return true;

    const auto ok = result.getProperty ("ok", {});
    return ok.isBool() && ! (bool) ok;
}

juce::String failureReason (const juce::var& result)
{
    const auto error = result.getProperty ("error", {}).toString();
    return error.isNotEmpty() ? error : juce::String ("engine call failed");
}

bool DirtyUpdate::parse (const juce::var& result, DirtyUpdate& out, juce::String& error)
{
    if (isFailure (result))
    {
        error = failureReason (result);
        return false;
    }

    out = {};
    out.rev = result.getProperty ("rev", {}).toString();
    out.reset = (bool) result.getProperty ("reset", false);
    out.more = (bool) result.getProperty ("more", false);
    out.analysisPending = (bool) result.getProperty ("analysis_pending", false);
    out.sampleRate = (double) result.getProperty ("sr", 0.0);
    out.numChannels = (int) result.getProperty ("channels", 0);
    out.sourceFrames = (juce::int64) result.getProperty ("source_frames", 0);

    if (out.rev.isEmpty() || out.sampleRate <= 0.0 || out.numChannels <= 0)
    {
        error = "ara_render_dirty: missing rev, sr or channels";
        return false;
    }

    if (auto* restore = result.getProperty ("restore", {}).getArray())
    {
        for (const auto& item : *restore)
        {
            auto* pair = item.getArray();

            if (pair == nullptr || pair->size() != 2)
            {
                error = "ara_render_dirty: bad restore entry";
                return false;
            }

            const auto start = (juce::int64) (*pair)[0];
            out.restore.push_back (juce::Range<juce::int64>::withStartAndLength (start, (juce::int64) (*pair)[1]));
        }
    }

    if (auto* windows = result.getProperty ("windows", {}).getArray())
    {
        for (const auto& item : *windows)
        {
            WindowMeta w;
            w.startFrame = (juce::int64) item.getProperty ("start_frame", 0);
            w.frames = (int) item.getProperty ("frames", 0);
            w.byteOffset = (juce::int64) item.getProperty ("byte_offset", 0);

            if (w.frames < 0 || w.startFrame < 0 || w.byteOffset < 0)
            {
                error = "ara_render_dirty: bad window";
                return false;
            }

            out.windows.push_back (w);
        }
    }

    const auto path = result.getProperty ("path", {});

    if (! out.windows.empty())
    {
        if (! path.isString() || path.toString().isEmpty())
        {
            error = "ara_render_dirty: windows without path";
            return false;
        }

        out.path = juce::File (path.toString());
    }

    return true;
}

} // namespace gliss
