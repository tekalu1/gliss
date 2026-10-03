#include "FloatWavWriter.h"

namespace gliss
{

FloatWavWriter::~FloatWavWriter() = default;

bool FloatWavWriter::open (const juce::File& file, double rate, int channels)
{
    stream.reset();
    written = 0;

    if (channels <= 0 || channels > 64 || rate <= 0.0)
        return false;

    file.deleteFile();
    stream = std::make_unique<juce::FileOutputStream> (file);

    if (stream->failedToOpen())
    {
        stream.reset();
        return false;
    }

    numChannels = channels;
    sampleRate = rate;
    return writeHeader (0);
}

bool FloatWavWriter::writeHeader (juce::int64 numFrames)
{
    // RIFF / fmt（IEEE float）/ fact / data。長さは finish() で埋め直す。
    const auto blockAlign = (juce::uint32) (numChannels * 4);
    const auto dataBytes = (juce::uint32) juce::jmin<juce::int64> ((juce::int64) numFrames * blockAlign, 0xffffffffll - 64);
    const auto rate = (juce::uint32) juce::roundToInt (sampleRate);

    auto& out = *stream;
    bool ok = out.write ("RIFF", 4);
    ok = ok && out.writeInt ((int) (4 + (8 + 16) + (8 + 4) + (8 + dataBytes)));
    ok = ok && out.write ("WAVE", 4);
    ok = ok && out.write ("fmt ", 4);
    ok = ok && out.writeInt (16);
    ok = ok && out.writeShort (3);   // WAVE_FORMAT_IEEE_FLOAT
    ok = ok && out.writeShort ((short) numChannels);
    ok = ok && out.writeInt ((int) rate);
    ok = ok && out.writeInt ((int) (rate * blockAlign));
    ok = ok && out.writeShort ((short) blockAlign);
    ok = ok && out.writeShort (32);
    ok = ok && out.write ("fact", 4);
    ok = ok && out.writeInt (4);
    ok = ok && out.writeInt ((int) (juce::uint32) numFrames);
    ok = ok && out.write ("data", 4);
    ok = ok && out.writeInt ((int) dataBytes);
    return ok;
}

bool FloatWavWriter::write (const float* const* channels, int numSamples)
{
    if (stream == nullptr || numSamples < 0)
        return false;

    const auto needed = numSamples * numChannels;

    if (needed > interleavedCapacity)
    {
        interleaved.realloc ((size_t) needed);
        interleavedCapacity = needed;
    }

    for (int i = 0; i < numSamples; ++i)
        for (int c = 0; c < numChannels; ++c)
            interleaved[i * numChannels + c] = channels[c][i];

    // WAV はリトルエンディアン（Windows・x64 はそのまま）。
    if (! stream->write (interleaved.get(), (size_t) needed * sizeof (float)))
        return false;

    written += numSamples;
    return true;
}

bool FloatWavWriter::finish()
{
    if (stream == nullptr)
        return false;

    bool ok = stream->setPosition (0) && writeHeader (written);
    stream->flush();
    ok = ok && stream->getStatus().wasOk();
    stream.reset();
    return ok;
}

} // namespace gliss
