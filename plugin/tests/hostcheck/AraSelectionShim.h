#pragma once

// GlissHostCheck は ARA のホスト（JUCE_PLUGINHOST_ARA）としてビルドし、プラグイン側の ARA（JucePlugin_Enable_ARA）を持たない。
// そのため juce::ARAViewSelection（ARA::PlugIn::ViewSelection の別名）が宣言されず、plugin/src/ara/DocumentBridge.h を
// 読めない。DocumentBridge は ViewSelection を参照でしか受けないので、不完全な型の宣言だけを置く（中身は使わない）。
#include <juce_core/juce_core.h>

namespace ARA::PlugIn
{
class ViewSelection;
}

namespace juce
{
using ARAViewSelection = ::ARA::PlugIn::ViewSelection;
}
