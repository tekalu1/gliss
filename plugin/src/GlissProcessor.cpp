#include "GlissProcessor.h"

#include "Diagnostics.h"
#include "GlissDocumentController.h"
#include "GlissEditor.h"
#include "GlissEditorRenderer.h"
#include "GlissPlaybackRenderer.h"

namespace gliss
{

GlissProcessor::GlissProcessor()
    : AudioProcessor (BusesProperties()
                          .withInput ("Input", juce::AudioChannelSet::stereo(), true)
                          .withOutput ("Output", juce::AudioChannelSet::stereo(), true))
{
}

void GlissProcessor::prepareToPlay (double sampleRate, int samplesPerBlock)
{
    prepareToPlayForARA (sampleRate, samplesPerBlock, getMainBusNumOutputChannels(), getProcessingPrecision());
}

void GlissProcessor::releaseResources()
{
    releaseResourcesForARA();
}

bool GlissProcessor::isBusesLayoutSupported (const BusesLayout& layouts) const
{
    const auto output = layouts.getMainOutputChannelSet();

    if (output != juce::AudioChannelSet::mono() && output != juce::AudioChannelSet::stereo())
        return false;

    const auto input = layouts.getMainInputChannelSet();
    return input.isDisabled() || input == output;
}

void GlissProcessor::processBlock (juce::AudioBuffer<float>& buffer, juce::MidiBuffer&)
{
    juce::ScopedNoDenormals noDenormals;

    // DAW の再生位置を DocumentController に写す（どの役のインスタンスでも。原子変数に書くだけ。docs/ara-plugin.md の「再生」）。
    if (auto* documentController = ARA::PlugIn::PlugInExtension::getDocumentController())
        if (auto* hostPlayHead = getPlayHead())
            if (const auto position = hostPlayHead->getPosition())
                juce::ARADocumentControllerSpecialisation::getSpecialisedDocumentController<GlissDocumentController> (documentController)
                    ->getPlayheadState().write (*position, reinterpret_cast<std::uintptr_t> (this));

    // ARA に結び付いていれば PlaybackRenderer が buffer を置き換える。そうでなければ入力をそのまま通す。
    processBlockForARA (buffer, isRealtime(), getPlayHead());
}

double GlissProcessor::getTailLengthSeconds() const
{
    double tail = 0.0;
    return getTailLengthSecondsForARA (tail) ? tail : 0.0;
}

void GlissProcessor::didBindToARA() noexcept
{
    AudioProcessorARAExtension::didBindToARA();

    auto* playback = dynamic_cast<GlissPlaybackRenderer*> (getPlaybackRenderer());
    auto* editor = dynamic_cast<GlissEditorRenderer*> (getEditorRenderer());

    if (playback == nullptr || editor == nullptr)
        return;

    auto pair = std::make_shared<RendererPair>();
    playback->linkInstance (pair);
    editor->linkInstance (pair);
    diag::logAlways ("instance: editor renderer " + juce::String ((juce::int64) editor->getRendererId()) + " <-> playback renderer "
                     + juce::String::toHexString ((juce::pointer_sized_int) playback));
}

std::uint64_t GlissProcessor::getEditorRendererId() const
{
    if (const auto* renderer = dynamic_cast<const GlissEditorRenderer*> (getEditorRenderer()))
        return renderer->getRendererId();

    return 0;
}

juce::AudioProcessorEditor* GlissProcessor::createEditor()
{
    return new GlissEditor (*this);
}

} // namespace gliss

//==============================================================================
juce::AudioProcessor* JUCE_CALLTYPE createPluginFilter()
{
    return new gliss::GlissProcessor();
}

const ARA::ARAFactory* JUCE_CALLTYPE createARAFactory()
{
    return juce::ARADocumentControllerSpecialisation::createARAFactory<gliss::GlissDocumentController>();
}
