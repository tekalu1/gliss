#pragma once

#include <juce_core/juce_core.h>

namespace gliss
{

/** One stdio MCP engine invocation. Paths may be overridden for tests. */
struct EngineConfig
{
    juce::File executable;
    juce::StringArray arguments;
    juce::File workingDirectory;
    juce::StringPairArray environment;

    bool isValid() const { return executable.existsAsFile() && workingDirectory.isDirectory(); }

    /** Finds development settings or the engine shipped beside the VST3. */
    static EngineConfig discover();
};

} // namespace gliss
