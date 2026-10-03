#include "KeyForwarding.h"

#if JUCE_WINDOWS
 #ifndef NOMINMAX
  #define NOMINMAX
 #endif
 #ifndef WIN32_LEAN_AND_MEAN
  #define WIN32_LEAN_AND_MEAN
 #endif
 #include <windows.h>
#endif

namespace gliss::editor
{

std::optional<ForwardedKey> ForwardedKey::fromVar (const juce::var& payload)
{
    const auto type = payload.getProperty ("type", {}).toString();
    const auto virtualKey = (int) payload.getProperty ("keyCode", 0);

    if ((type != "keydown" && type != "keyup") || virtualKey <= 0 || virtualKey >= 255)
        return std::nullopt;

    ForwardedKey key;
    key.down = type == "keydown";
    key.virtualKey = virtualKey;
    key.code = payload.getProperty ("code", {}).toString();
    key.alt = (bool) payload.getProperty ("alt", false);
    key.ctrl = (bool) payload.getProperty ("ctrl", false);
    key.repeat = (bool) payload.getProperty ("repeat", false);
    return key;
}

bool postKeyToWindow (void* nativeWindowHandle, const ForwardedKey& key)
{
#if JUCE_WINDOWS
    auto* hwnd = static_cast<HWND> (nativeWindowHandle);

    if (hwnd == nullptr || ! IsWindow (hwnd))
        return false;

    // lParam: 繰り返し 1・スキャンコード・拡張キー・Alt（context code）・直前の状態・離した
    static const juce::StringArray extendedCodes { "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Insert", "Delete",
                                                   "Home", "End", "PageUp", "PageDown", "NumpadEnter", "NumpadDivide",
                                                   "ControlRight", "AltRight", "MetaLeft", "MetaRight" };
    const auto scanCode = (LPARAM) MapVirtualKeyW ((UINT) key.virtualKey, MAPVK_VK_TO_VSC) & 0xff;
    const bool system = key.alt && ! key.ctrl;

    LPARAM lParam = 1 | (scanCode << 16);

    if (extendedCodes.contains (key.code))
        lParam |= (LPARAM) 1 << 24;

    if (key.alt)
        lParam |= (LPARAM) 1 << 29;

    if (! key.down)
        lParam |= ((LPARAM) 1 << 30) | ((LPARAM) 1 << 31);
    else if (key.repeat)
        lParam |= (LPARAM) 1 << 30;

    const UINT message = key.down ? (system ? WM_SYSKEYDOWN : WM_KEYDOWN)
                                  : (system ? WM_SYSKEYUP : WM_KEYUP);

    return PostMessageW (hwnd, message, (WPARAM) key.virtualKey, lParam) != 0;
#else
    juce::ignoreUnused (nativeWindowHandle, key);
    return false;
#endif
}

} // namespace gliss::editor
