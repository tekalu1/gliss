#include "RegionReader.h"

#include <algorithm>
#include <cmath>

namespace gliss
{

namespace
{

// WindowedSincInterpolator の遅れ（ソースのサンプル）。出力は「最後に入れたサンプル − 100」の時刻になる
constexpr int interpolatorLatency = (int) juce::WindowedSincInterpolator::getBaseLatency();
// シークの後に変換器を満たす長さ（新しい位置の前後 100 サンプルずつ）
constexpr int primeLength = 2 * interpolatorLatency;
// process() が n 個を書くのに読むソースは ceil (n * 比) + 1 個まで
constexpr int interpolatorLookahead = 2;
// startInSource がこれ以上ずれていたら続きのブロックではない（シーク・ループ）とみなす
constexpr double continuityTolerance = 2.0;

/** 作業用のバッファの先頭 numSamples を指す AudioBuffer（確保しない。チャンネル数は maxChannels まで）。 */
juce::AudioBuffer<float> makeView (juce::AudioBuffer<float>& work, int numChannels, int numSamples) noexcept
{
    float* channels[RegionReader::maxChannels] = {};
    for (int ch = 0; ch < numChannels; ++ch)
        channels[ch] = work.getWritePointer (ch);
    return juce::AudioBuffer<float> (channels, numChannels, numSamples);
}

void clearDest (juce::AudioBuffer<float>& dest, int destStart, int numSamples) noexcept
{
    for (int ch = 0; ch < dest.getNumChannels(); ++ch)
        dest.clear (ch, destStart, numSamples);
}

void writeToDest (juce::AudioBuffer<float>& dest,
                  int destStart,
                  int numSamples,
                  const juce::AudioBuffer<float>& src,
                  bool add) noexcept
{
    const auto srcChans = src.getNumChannels();
    const auto destChans = dest.getNumChannels();

    if (destChans == 1 && srcChans > 1)
    {
        // 出力がモノラル: ソースの全チャンネルの平均（ステレオなら (L + R) / 2）
        const auto gain = 1.0f / (float) srcChans;
        auto* d = dest.getWritePointer (0, destStart);
        if (! add)
            juce::FloatVectorOperations::clear (d, numSamples);
        for (int ch = 0; ch < srcChans; ++ch)
            juce::FloatVectorOperations::addWithMultiply (d, src.getReadPointer (ch), gain, numSamples);
        return;
    }

    for (int ch = 0; ch < destChans; ++ch)
    {
        // ソースがモノラルなら全出力へ。そのほかはチャンネルごと（ソースに無い出力のチャンネルは無音）
        const auto srcCh = srcChans == 1 ? 0 : ch;
        if (srcCh >= srcChans)
        {
            if (! add)
                dest.clear (ch, destStart, numSamples);
            continue;
        }

        if (add)
            dest.addFrom (ch, destStart, src, srcCh, 0, numSamples);
        else
            dest.copyFrom (ch, destStart, src, srcCh, 0, numSamples);
    }
}

/** buffer（ソースの位置 startInSource から numSamples）に、重なる窓の PCM を上書きする。窓の列を二分探索してたどる。 */
void applyWindows (juce::AudioBuffer<float>& buffer,
                   juce::int64 startInSource,
                   int numSamples,
                   const EditedPcmSnapshot& snapshot) noexcept
{
    const auto& windows = snapshot.getWindows();
    const auto endInSource = startInSource + numSamples;
    const auto bufferChannels = buffer.getNumChannels();

    for (auto i = snapshot.findFirstWindowEndingAfter (startInSource);
         i < windows.size() && windows[i].startFrame < endInSource;
         ++i)
    {
        const auto& win = windows[i];
        const auto overlapStart = std::max (startInSource, win.startFrame);
        const auto overlapEnd = std::min (endInSource, win.getEndFrame());
        const auto count = (int) (overlapEnd - overlapStart);
        const auto winChannels = win.getNumChannels();
        if (count <= 0 || winChannels <= 0)
            continue;

        const auto destOffset = (int) (overlapStart - startInSource);
        const auto winOffset = (int) (overlapStart - win.startFrame);

        // 窓がモノラルなら全チャンネルへ。窓に無いチャンネルは原音のまま
        for (int ch = 0; ch < bufferChannels; ++ch)
        {
            const auto winCh = winChannels == 1 ? 0 : ch;
            if (winCh < winChannels)
                juce::FloatVectorOperations::copy (buffer.getWritePointer (ch, destOffset),
                                                   win.getReadPointer (winCh) + winOffset,
                                                   count);
        }
    }
}

} // namespace

//==============================================================================
/** ソースの周波数の列（原音の上に窓を重ねたもの）の読み出し。 */
struct RegionReader::Source
{
    SourceReader* reader = nullptr;
    const EditedPcmSnapshot* snapshot = nullptr;   // 当てる窓（無ければ原音だけ）
    int timeoutMs = 0;

    bool fill (juce::AudioBuffer<float>& buffer, juce::int64 startInSource) const noexcept
    {
        bool complete = true;
        if (reader != nullptr)
            complete = reader->readSourceSamples (buffer, 0, buffer.getNumSamples(), startInSource, timeoutMs);
        else
            buffer.clear();

        if (snapshot != nullptr)
            applyWindows (buffer, startInSource, buffer.getNumSamples(), *snapshot);

        return complete;
    }
};

//==============================================================================
RegionReader::RegionReader() = default;
RegionReader::~RegionReader() = default;

void RegionReader::prepare (double hostSampleRateIn, int maxBlockSize, int maxSourceChannels, double sourceSampleRate)
{
    hostSampleRate = hostSampleRateIn > 0.0 ? hostSampleRateIn : (sourceSampleRate > 0.0 ? sourceSampleRate : 48000.0);
    blockCapacity = std::max (1, maxBlockSize);
    channelCapacity = juce::jlimit (1, maxChannels, maxSourceChannels);

    const auto ratio = sourceSampleRate > 0.0 ? std::max (1.0, sourceSampleRate / hostSampleRate) : 1.0;
    sourceCapacity = std::max (primeLength, (int) std::ceil ((double) blockCapacity * ratio) + interpolatorLookahead);

    sourceWork.setSize (channelCapacity, sourceCapacity);
    outputWork.setSize (channelCapacity, std::max (blockCapacity, primeLength));
    interpolators.reset (new juce::WindowedSincInterpolator[(size_t) channelCapacity]);
    reset();
}

void RegionReader::releaseResources()
{
    sourceCapacity = 0;
    blockCapacity = 0;
    channelCapacity = 0;
    sourceWork = juce::AudioBuffer<float>();
    outputWork = juce::AudioBuffer<float>();
    interpolators.reset();
    reset();
}

void RegionReader::reset() noexcept
{
    primed = false;
}

bool RegionReader::prime (juce::int64 startInSource, int numChannels, const Source& source) noexcept
{
    // 新しい位置の前後 100 サンプルを比 1 で入れる（出力は捨てる）。次の出力が startInSource の時刻になる
    auto in = makeView (sourceWork, numChannels, primeLength);
    auto out = makeView (outputWork, numChannels, primeLength);
    const auto complete = source.fill (in, startInSource - interpolatorLatency);

    for (int ch = 0; ch < numChannels; ++ch)
    {
        interpolators[ch].reset();
        interpolators[ch].process (1.0, in.getReadPointer (ch), out.getWritePointer (ch), primeLength);
    }

    nextReadPosition = startInSource - interpolatorLatency + primeLength;
    expectedStartInSource = (double) startInSource;
    primed = true;
    primedChannels = numChannels;
    return complete;
}

bool RegionReader::readBlock (juce::AudioBuffer<float>& destBuffer,
                              int destStartSample,
                              int numDestSamples,
                              juce::int64 startInSource,
                              SourceReader* sourceReader,
                              const EditedPcm* editedPcm,
                              const RegionReadOptions& options) noexcept
{
    if (numDestSamples <= 0)
        return true;

    if (destStartSample < 0 || destBuffer.getNumChannels() <= 0)
        return false;

    numDestSamples = std::min (numDestSamples, destBuffer.getNumSamples() - destStartSample);
    if (numDestSamples <= 0)
        return false;

    if (! isPrepared())
    {
        if (! options.addToDestBuffer)
            clearDest (destBuffer, destStartSample, numDestSamples);
        return false;
    }

    // スナップショットを取得（tryLock。取れなければ原音）。写しと破棄は参照カウントの増減だけ
    std::shared_ptr<const EditedPcmSnapshot> snapshot;
    if (! options.compareMode && editedPcm != nullptr)
        snapshot = editedPcm->tryGetSnapshot();

    // ソースのフォーマット
    double sourceSampleRate = 0.0;
    int sourceChannels = 0;
    if (sourceReader != nullptr)
    {
        sourceSampleRate = sourceReader->getSampleRate();
        sourceChannels = sourceReader->getNumChannels();
    }
    else if (snapshot != nullptr)
    {
        sourceSampleRate = snapshot->getSampleRate();
        sourceChannels = snapshot->getNumChannels();
    }

    if (sourceSampleRate <= 0.0)
        sourceSampleRate = hostSampleRate;
    if (sourceChannels <= 0)
        sourceChannels = destBuffer.getNumChannels();
    sourceChannels = std::min (sourceChannels, channelCapacity);

    Source source;
    source.reader = sourceReader;
    source.timeoutMs = options.timeoutMs;
    // 窓の位置はソースの周波数のサンプルなので、周波数の違うスナップショット（設定の食い違い）は当てない
    if (snapshot != nullptr && snapshot->hasWindows() && std::abs (snapshot->getSampleRate() - sourceSampleRate) < 1.0)
        source.snapshot = snapshot.get();

    bool complete = true;

    //--------------------------------------------------------------------------
    // サンプリング周波数が同じ場合（素通し）
    //--------------------------------------------------------------------------
    if (std::abs (sourceSampleRate - hostSampleRate) < 1.0)
    {
        primed = false;

        for (int done = 0; done < numDestSamples;)
        {
            const auto chunk = std::min (numDestSamples - done, sourceCapacity);
            auto in = makeView (sourceWork, sourceChannels, chunk);
            complete = source.fill (in, startInSource + done) && complete;
            writeToDest (destBuffer, destStartSample + done, chunk, in, options.addToDestBuffer);
            done += chunk;
        }

        return complete;
    }

    //--------------------------------------------------------------------------
    // サンプリング周波数が異なる場合（流しの変換）
    //--------------------------------------------------------------------------
    const double ratio = sourceSampleRate / hostSampleRate;
    const auto maxChunk = std::min (blockCapacity, (int) ((double) (sourceCapacity - interpolatorLookahead) / ratio));
    if (maxChunk <= 0)
    {
        if (! options.addToDestBuffer)
            clearDest (destBuffer, destStartSample, numDestSamples);
        return false;
    }

    if (! primed
        || ! juce::exactlyEqual (ratio, primedRatio)
        || sourceChannels != primedChannels
        || std::abs ((double) startInSource - expectedStartInSource) > continuityTolerance)
    {
        complete = prime (startInSource, sourceChannels, source);
        primedRatio = ratio;
    }

    for (int done = 0; done < numDestSamples;)
    {
        const auto chunk = std::min (numDestSamples - done, maxChunk);
        const auto needed = std::min (sourceCapacity, (int) std::ceil ((double) chunk * ratio) + interpolatorLookahead);

        // 原音と窓を合わせてから変換するので、窓の継ぎ目に段差が出ない
        auto in = makeView (sourceWork, sourceChannels, needed);
        auto out = makeView (outputWork, sourceChannels, chunk);
        complete = source.fill (in, nextReadPosition) && complete;

        int used = 0;
        for (int ch = 0; ch < sourceChannels; ++ch)
            used = interpolators[ch].process (ratio, in.getReadPointer (ch), out.getWritePointer (ch), chunk);

        // 変換器が読んだ分だけ進める（読み残しは次に読み直す）
        nextReadPosition += used;
        expectedStartInSource += (double) chunk * ratio;

        writeToDest (destBuffer, destStartSample + done, chunk, out, options.addToDestBuffer);
        done += chunk;
    }

    return complete;
}

} // namespace gliss
