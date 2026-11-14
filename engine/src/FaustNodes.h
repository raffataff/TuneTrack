// TuneTrack's built-in Faust synths and effects as graph nodes (the DSP is generated: gen/tt_faust_gen.h).
#pragma once

#include "Nodes.h"

namespace tt
{
class FaustNode : public TtProcessor
{
public:
    using TtProcessor::TtProcessor;
    // Any thread. Unknown names are ignored (returns false).
    virtual bool setParam (const juce::String& name, float value) = 0;
};

// nullptr when there is no such effect / template.
std::unique_ptr<FaustNode> createFaustFx (const juce::String& name, bool sidechain);
// perSample: compute one sample at a time, the way the offline renderer's Faust host does.
std::unique_ptr<FaustNode> createFaustSynth (const juce::String& templateName, int voices, bool perSample);

}  // namespace tt
