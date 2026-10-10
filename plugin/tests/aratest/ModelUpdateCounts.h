// GlissARATest のホストが受けた「中身が変わった」の知らせ（ARAModelUpdateControllerInterface）を数える。
// GlissModelUpdateController.cpp が SDK の TestHost の ARAModelUpdateController.cpp の代わりに数え、GlissARATest.cpp が読む。
#pragma once

#include <string>

struct ModelUpdateCounts
{
    int sourceContent = 0;          // notifyAudioSourceContentChanged
    int modificationSamples = 0;    // notifyAudioModificationContentChanged（音が変わった）
    int modificationOther = 0;      //   〃（音は同じ。ノートなど）
    int modificationState = 0;      //   〃（音もノートも同じ。保存するものだけが変わった。modificationOther にも数える）
    int regionSamples = 0;          // notifyPlaybackRegionContentChanged（音が変わった）
    int regionOther = 0;            //   〃（音は同じ）
    int documentData = 0;           // notifyDocumentDataChanged
    int analysisProgress = 0;       // notifyAudioSourceAnalysisProgress（中身の変更ではない。数えるだけ）

    int changes () const noexcept
    {
        return sourceContent + modificationSamples + modificationOther + regionSamples + regionOther + documentData;
    }

    std::string toJson () const
    {
        return "{\"source_content\": " + std::to_string (sourceContent)
             + ", \"modification_samples\": " + std::to_string (modificationSamples)
             + ", \"modification_other\": " + std::to_string (modificationOther)
             + ", \"modification_state\": " + std::to_string (modificationState)
             + ", \"region_samples\": " + std::to_string (regionSamples)
             + ", \"region_other\": " + std::to_string (regionOther)
             + ", \"document_data\": " + std::to_string (documentData)
             + ", \"analysis_progress\": " + std::to_string (analysisProgress) + "}";
    }
};

/** このプロセスのホストが受けた数（GlissARATest は 1 本のスレッドでホストを回すので、ロックは要らない）。 */
ModelUpdateCounts& modelUpdateCounts ();
