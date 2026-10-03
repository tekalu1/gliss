#include "PluginState.h"

namespace gliss::pluginstate
{

juce::File defaultFile()
{
    const auto overridden = juce::SystemStats::getEnvironmentVariable ("GLISS_PLUGIN_STATE_FILE", {});

    if (overridden.isNotEmpty())
        return juce::File (overridden);

    return juce::File::getSpecialLocation (juce::File::userApplicationDataDirectory)
        .getChildFile ("Gliss")
        .getChildFile ("plugin-state.json");
}

juce::var load (const juce::File& file)
{
    if (file.existsAsFile())
    {
        const auto parsed = juce::JSON::parse (file.loadFileAsString());

        if (parsed.isObject())
            return parsed;
    }

    return juce::var (new juce::DynamicObject());
}

juce::var merge (const juce::var& base, const juce::var& patch)
{
    auto* result = new juce::DynamicObject();

    if (auto* b = base.getDynamicObject())
        for (const auto& p : b->getProperties())
            result->setProperty (p.name, p.value);

    if (auto* p = patch.getDynamicObject())
    {
        for (const auto& property : p->getProperties())
        {
            if (property.value.isVoid() || property.value.isUndefined())
                result->removeProperty (property.name);
            else
                result->setProperty (property.name, property.value);
        }
    }

    return juce::var (result);
}

bool save (const juce::File& file, const juce::var& state)
{
    if (! file.getParentDirectory().createDirectory())
        return false;

    juce::TemporaryFile temp (file);

    if (! temp.getFile().replaceWithText (juce::JSON::toString (state, false), false, false, "\n"))
        return false;

    return temp.overwriteTargetFileWithTemporary();
}

} // namespace gliss::pluginstate
