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

} // namespace gliss::diag
