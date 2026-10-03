#include "EmbeddedAssets.h"

#include "GlissEditorData.h"
#include "GlissPageData.h"

namespace gliss::editor
{

std::optional<juce::MemoryBlock> embeddedPageAsset (const juce::String& fileName)
{
    for (int i = 0; i < GlissPageData::namedResourceListSize; ++i)
    {
        const auto* name = GlissPageData::namedResourceList[i];

        if (fileName == GlissPageData::getNamedResourceOriginalFilename (name))
        {
            int size = 0;
            const auto* data = GlissPageData::getNamedResource (name, size);
            return juce::MemoryBlock (data, (size_t) size);
        }
    }

    return std::nullopt;
}

juce::MemoryBlock embeddedJuceInterop()
{
    return { GlissEditorData::index_js, (size_t) GlissEditorData::index_jsSize };
}

juce::String embeddedKeyForwardScript()
{
    return juce::String::fromUTF8 (GlissEditorData::keyforward_js, GlissEditorData::keyforward_jsSize);
}

} // namespace gliss::editor
