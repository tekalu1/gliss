#include "cache/StretchReader.h"
#include "cache/SourceReader.h"
#include "AllocationCounter.h"

#include <juce_audio_basics/juce_audio_basics.h>
#include <cmath>

namespace gliss
{
class StretchTests final : public juce::UnitTest
{
public:
    StretchTests() : juce::UnitTest ("ARA time stretch", "Gliss") {}

    void runTest() override
    {
        constexpr int rate = 8000;
        juce::AudioBuffer<float> source (1, rate * 3);
        source.clear();
        for (int click = 1; click <= 8; ++click)
        {
            const int centre = click * rate / 4;
            for (int j = -12; j <= 12; ++j)
                source.setSample (0, centre + j, (float) (1.0 - std::abs (j) / 13.0));
        }
        BufferSourceReader reader (source, rate);

        for (double scale : { 0.75, 1.25 })
        {
            beginTest ("click positions at scale " + juce::String (scale));
            StretchReader stretch;
            stretch.prepare (rate, 512, 1, rate);
            juce::AudioBuffer<float> output (1, rate * 2);
            output.clear();
            bool complete = true;
            for (int p = 0; p < output.getNumSamples(); p += 512)
            {
                const auto n = juce::jmin (512, output.getNumSamples() - p);
                complete = stretch.readBlock (output, p, n, p, p * scale, scale, &reader, nullptr) && complete;
            }
            expect (complete);
            for (int click = 1; click <= 5; ++click)
            {
                const int expected = (int) std::round (click * rate / (4.0 * scale));
                int best = expected;
                float peak = 0;
                for (int p = expected - 160; p <= expected + 160; ++p)
                    if (std::abs (output.getSample (0, p)) > peak)
                    {
                        peak = std::abs (output.getSample (0, p));
                        best = p;
                    }
                expect (peak > 0.1f);
                expect (std::abs (best - expected) <= 40, "click " + juce::String (click) + " offset=" + juce::String (best - expected));
            }

            beginTest ("seek and block length at scale " + juce::String (scale));
            juce::AudioBuffer<float> seeked (1, 1024);
            seeked.clear();
            // prepare の上限が 512 なので、同じ 1024 サンプルを内部で 512 ずつ処理する。
            expect (stretch.readBlock (seeked, 0, 1024, 6000, 6000 * scale, scale, &reader, nullptr));
            StretchReader fresh;
            fresh.prepare (rate, 1024, 1, rate);
            juce::AudioBuffer<float> reference (1, 1024);
            reference.clear();
            bool seekOk = false;
            long long seekAllocations = -1;
            {
                test::ScopedAllocationCounter counter;
                seekOk = fresh.readBlock (reference, 0, 1024, 6000, 6000 * scale, scale, &reader, nullptr);
                seekAllocations = counter.getAllocations();
            }
            expect (seekOk);
            expectEquals (seekAllocations, 0LL);
            float difference = 0;
            for (int p = 0; p < 1024; ++p)
                difference = juce::jmax (difference, std::abs (reference.getSample (0, p) - seeked.getSample (0, p)));
            expect (difference < 0.05f, "seek difference=" + juce::String (difference));

            beginTest ("readBlock allocates nothing after prepare at scale " + juce::String (scale));
            test::ScopedAllocationCounter counter;
            const auto ok = fresh.readBlock (reference, 0, 512, 7024, 7024 * scale, scale, &reader, nullptr);
            const auto allocations = counter.getAllocations();
            expect (ok);
            expectEquals (allocations, 0LL);
        }
    }
};
static StretchTests stretchTests;
} // namespace gliss
