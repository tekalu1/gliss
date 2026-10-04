#pragma once

#include <juce_core/juce_core.h>

namespace gliss::editor
{

/** 画面（key-forward.js）が送ってきたキー 1 つ（イベント gliss-key の中身）。 */
struct ForwardedKey
{
    bool down = true;
    int virtualKey = 0;      // JS の keyCode（Windows の仮想キーの番号と同じ）
    juce::String code;       // KeyboardEvent.code（拡張キーの判定に使う）
    bool alt = false, ctrl = false, repeat = false;

    /** イベントの中身から作る。使えない（keyCode が 1〜254 でない・type が違う）なら nullopt。 */
    static std::optional<ForwardedKey> fromVar (const juce::var& payload);
};

/** キーをプラグインの窓（JUCE のピアの HWND）に WM_KEYDOWN / WM_KEYUP（Alt だけのときは WM_SYS…）として置く。
    JUCE のピアは自分が使わなかったキーを親の窓（DAW）へ PostMessage する（juce_Windowing_windows.cpp の
    forwardMessageToParent）ので、DAW から見ると JUCE の普通のプラグインの窓で押されたのと同じになる。
    Windows 以外では何もしない（false）。 */
bool postKeyToWindow (void* nativeWindowHandle, const ForwardedKey& key);

} // namespace gliss::editor
