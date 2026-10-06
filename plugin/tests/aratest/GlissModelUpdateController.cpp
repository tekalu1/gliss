// SDK の TestHost の ARAModelUpdateController（ARAHostInterfaces/ARAModelUpdateController.cpp）の代わり。
// 同じクラス（ヘッダは SDK のもの）を、プラグインからの知らせを数える形で実装する（ModelUpdateCounts.h）。
// GlissARATest はホストとして notifyModelUpdates を自分で呼ぶので、SDK の版の「TestHost が問い合わせている間だけ」の確かめはしない。

#include "ARAHostInterfaces/ARAModelUpdateController.h"
#include "ModelUpdateCounts.h"

ModelUpdateCounts& modelUpdateCounts ()
{
    static ModelUpdateCounts counts;
    return counts;
}

void ARAModelUpdateController::notifyAudioSourceAnalysisProgress (ARA::ARAAudioSourceHostRef, ARA::ARAAnalysisProgressState, float) noexcept
{
    ++modelUpdateCounts ().analysisProgress;
}

void ARAModelUpdateController::notifyAudioSourceContentChanged (ARA::ARAAudioSourceHostRef, const ARA::ARAContentTimeRange*,
                                                               ARA::ContentUpdateScopes scopeFlags) noexcept
{
    ++modelUpdateCounts ().sourceContent;
    ARA_LOG ("host: audio source content changed, flags 0x%X", static_cast<unsigned> (scopeFlags));
}

void ARAModelUpdateController::notifyAudioModificationContentChanged (ARA::ARAAudioModificationHostRef, const ARA::ARAContentTimeRange*,
                                                                     ARA::ContentUpdateScopes scopeFlags) noexcept
{
    ++(scopeFlags.affectSamples () ? modelUpdateCounts ().modificationSamples : modelUpdateCounts ().modificationOther);
    ARA_LOG ("host: audio modification content changed, flags 0x%X", static_cast<unsigned> (scopeFlags));
}

void ARAModelUpdateController::notifyPlaybackRegionContentChanged (ARA::ARAPlaybackRegionHostRef, const ARA::ARAContentTimeRange*,
                                                                  ARA::ContentUpdateScopes scopeFlags) noexcept
{
    ++(scopeFlags.affectSamples () ? modelUpdateCounts ().regionSamples : modelUpdateCounts ().regionOther);
    ARA_LOG ("host: playback region content changed, flags 0x%X", static_cast<unsigned> (scopeFlags));
}

void ARAModelUpdateController::notifyDocumentDataChanged () noexcept
{
    ++modelUpdateCounts ().documentData;
    ARA_LOG ("host: document data changed");
}
