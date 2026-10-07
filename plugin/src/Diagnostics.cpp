#include "Diagnostics.h"

#include "ProcessUtils.h"

namespace gliss::diag
{

const juce::File& traceDir()
{
    static const juce::File dir = []
    {
        const auto value = juce::SystemStats::getEnvironmentVariable ("GLISS_ARA_TRACE_DIR", {});

        if (value.isEmpty())
            return juce::File();

        juce::File folder (value);
        folder.createDirectory();
        return folder;
    }();

    return dir;
}

bool traceEnabled()
{
    static const bool enabled = traceDir() != juce::File();
    return enabled;
}

int forcedReadTimeoutMs()
{
    static const int timeout = []
    {
        const auto value = juce::SystemStats::getEnvironmentVariable ("GLISS_ARA_READ_TIMEOUT_MS", {});
        return value.isEmpty() ? -1 : juce::jmax (0, value.getIntValue());
    }();

    return timeout;
}

void log (const juce::String& line)
{
    if (! traceEnabled())
        return;

    static juce::CriticalSection lock;
    static const auto file = traceDir().getChildFile ("gliss-ara-" + juce::String (currentProcessId()) + ".log");

    const juce::ScopedLock sl (lock);
    file.appendText (juce::Time::getCurrentTime().formatted ("%H:%M:%S") + " " + line + "\n");
}

namespace
{
juce::File alwaysFile()
{
    const auto direct = juce::SystemStats::getEnvironmentVariable ("GLISS_PLUGIN_LOG_FILE", {});

    if (direct.isNotEmpty())
        return juce::File (direct);

    const auto state = juce::SystemStats::getEnvironmentVariable ("GLISS_PLUGIN_STATE_FILE", {});

    if (state.isNotEmpty())
        return juce::File (state).getSiblingFile ("plugin.log");

    return juce::File::getSpecialLocation (juce::File::userApplicationDataDirectory)
        .getChildFile ("Gliss").getChildFile ("plugin.log");
}
} // namespace

void logAlways (const juce::String& line)
{
    log (line);

    static juce::CriticalSection lock;
    static juce::String last;
    static juce::uint32 lastMs = 0, windowStartMs = 0;
    static int suppressed = 0, inWindow = 0;
    const juce::ScopedLock sl (lock);
    const auto now = juce::Time::getMillisecondCounter();

    if (line == last && now - lastMs < 2000)
    {
        ++suppressed;
        return;
    }

    if (now - windowStartMs >= 1000)
    {
        windowStartMs = now;
        inWindow = 0;
    }

    if (++inWindow > 20)
    {
        ++suppressed;
        return;
    }

    static const auto file = alwaysFile();
    file.getParentDirectory().createDirectory();

    if (file.getSize() > 1024 * 1024)
    {
        const auto old = file.getSiblingFile (file.getFileName() + ".1");
        old.deleteFile();
        file.moveFileTo (old);
    }

    auto text = juce::Time::getCurrentTime().formatted ("%Y-%m-%d %H:%M:%S") + " pid " + juce::String (currentProcessId())
              + " " + line;

    if (suppressed > 0)
        text += "  (+" + juce::String (suppressed) + " similar lines omitted)";

    file.appendText (text + "\n");
    last = line;
    lastMs = now;
    suppressed = 0;
}

} // namespace gliss::diag
