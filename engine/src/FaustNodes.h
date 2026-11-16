// TuneTrack's built-in Faust synths and effects as graph nodes (the DSP is generated: gen/tt_faust_gen.h).
#pragma once

#include "Nodes.h"

#include <set>

namespace tt
{
class FaustNode : public TtProcessor
{
public:
    using TtProcessor::TtProcessor;

    // Message thread. Unknown names are ignored (returns false). A control that follows automation keeps this
    // setting for when it no longer does.
    virtual bool setParam (const juce::String& name, float value) = 0;

    // Message thread: the controls that follow automation from now on ({label: points}, song samples -> the
    // control's own units); `latency` = how late the audio arriving here is. A control left out goes back to its
    // setting. Returns the labels this device has no control for.
    juce::StringArray automate (const std::map<juce::String, AutoPts>& lanes, int latency);

protected:
    // Automated controls move on a grid of 32 samples of song time, so a render does not depend on the block size.
    static constexpr int autoGrid = 32;

    struct Ctl
    {
        std::vector<float*> zones;  // one per voice for a synth
        AutoPts pts;
        mutable size_t hint = 0;
    };
    struct AutoState
    {
        std::vector<Ctl> ctls;
        std::vector<std::pair<std::vector<float*>, float>> restore;  // controls no longer automated -> their setting
        int latency = 0;
    };

    // Message thread: a control's zones (empty when there is none) and its setting.
    virtual std::vector<float*> zonesFor (const std::string& label) = 0;
    virtual bool settingFor (const std::string& label, float& value) = 0;
    bool isAutomated (const std::string& label) const { return automated.count (label) > 0; }

    // Audio thread, at the start of a block: nullptr when nothing is automated, else the lanes, with t0 = the song
    // time of the block's first sample at this device and moving = whether song time advances in this block.
    const AutoState* autoBegin (juce::int64& t0, bool& moving);
    static void autoSet (const AutoState&, juce::int64 t);  // every lane's value at the grid point at or before t
    static int autoSpan (juce::int64 t, int left)            // samples from t to the next grid point, at most left
    {
        const juce::int64 r = ((t % autoGrid) + autoGrid) % autoGrid;
        return (int) juce::jmin<juce::int64> (left, autoGrid - r);
    }

private:
    Shared<AutoState> plan;
    std::set<std::string> automated, ever;  // message thread
};

// nullptr when there is no such effect / template.
std::unique_ptr<FaustNode> createFaustFx (const juce::String& name, bool sidechain);
// perSample: compute one sample at a time, the way the offline renderer's Faust host does.
std::unique_ptr<FaustNode> createFaustSynth (const juce::String& templateName, int voices, bool perSample);

}  // namespace tt
