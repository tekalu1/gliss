#pragma once

#include <juce_audio_processors/juce_audio_processors.h>

#include <cstdint>

namespace gliss
{

/** Gliss の AudioProcessor。ARA の外（普通の VST3 として）では、入力をそのまま通す。
    ARA の中では、再生は PlaybackRenderer（GlissPlaybackRenderer）が行う。 */
class GlissProcessor final : public juce::AudioProcessor,
                             private juce::AudioProcessorARAExtension
{
public:
    GlissProcessor();
    ~GlissProcessor() override = default;

    void prepareToPlay (double sampleRate, int samplesPerBlock) override;
    void releaseResources() override;
    bool isBusesLayoutSupported (const BusesLayout& layouts) const override;
    void processBlock (juce::AudioBuffer<float>& buffer, juce::MidiBuffer& midiMessages) override;
    using AudioProcessor::processBlock;

    const juce::String getName() const override { return JucePlugin_Name; }
    bool acceptsMidi() const override { return false; }
    bool producesMidi() const override { return false; }
    double getTailLengthSeconds() const override;

    int getNumPrograms() override { return 1; }
    int getCurrentProgram() override { return 0; }
    void setCurrentProgram (int) override {}
    const juce::String getProgramName (int) override { return {}; }
    void changeProgramName (int, const juce::String&) override {}

    // 状態は ARA のドキュメントのアーカイブ（GlissDocumentController）に持つ。VST3 の状態には何も入れない。
    void getStateInformation (juce::MemoryBlock&) override {}
    void setStateInformation (const void*, int) override {}

    bool hasEditor() const override { return true; }
    juce::AudioProcessorEditor* createEditor() override;

    juce::AudioProcessorARAExtension* getARAClientExtensions() override { return this; }

    /** このインスタンスの EditorRenderer の id（ARA に結び付いていなければ 0）。試聴を求めたインスタンスを知るために、
        GlissEditor が画面の preview に添える（PreviewAudio::chooseEligible）。メッセージスレッドから呼ぶ。 */
    std::uint64_t getEditorRendererId() const;

private:
    JUCE_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR (GlissProcessor)
};

} // namespace gliss
