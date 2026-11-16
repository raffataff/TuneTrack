// Parameter automation for hosted plugins: each automated parameter is set at the start of every block, to its
// lane's value at the song time of the audio arriving at the device (the block's position less the latency of the
// devices before it, as the fader does). Built-in devices follow their own lanes every 32 samples (FaustNodes.h).
#pragma once

#include "Nodes.h"

#include <algorithm>

namespace tt
{
struct ParamAuto
{
    juce::AudioProcessorGraph::Node::Ptr node;  // keeps the plugin alive while a block may still use this plan
    juce::AudioProcessorParameter* param = nullptr;
    AutoPts pts;                                // song sample -> 0..1
    int latency = 0;
    mutable size_t hint = 0;
    mutable float last = -1.0f;
};

// One graph's automated plugin parameters (the session's graph, or a lane's).
struct AutoPlan
{
    std::vector<ParamAuto> params;
    // parameters that were automated and no longer are, with the value to go back to: set once when the plan is
    // picked up (every plan carries all of them, so one the audio thread never saw loses nothing)
    std::vector<std::pair<juce::AudioProcessorParameter*, float>> restore;
    std::vector<juce::AudioProcessorGraph::Node::Ptr> keep;
};

// The audio thread (or the message thread offline), right before the graph computes a block.
inline void applyPlan (Shared<AutoPlan>& shared, const Transport& t)
{
    const AutoPlan* plan = shared.get();
    if (plan == nullptr)
        return;
    if (shared.takeChanged())
        for (auto& [p, v] : plan->restore)
            p->setValue (v);
    for (auto& a : plan->params)
    {
        const float v = juce::jlimit (0.0f, 1.0f, autoValue (a.pts, t.blockPos - a.latency, a.hint));
        if (v != a.last)
        {
            a.param->setValue (v);
            a.last = v;
        }
    }
}

// Points as the spec writes them, [[sample, number], ...], in time order (two points at one sample are a step:
// their order is kept).
inline AutoPts numericPoints (const juce::var& v)
{
    AutoPts out;
    if (auto* a = v.getArray())
        for (auto& p : *a)
            if (auto* pair = p.getArray(); pair != nullptr && pair->size() >= 2 && ! (*pair)[1].isString())
                out.emplace_back ((juce::int64) (double) (*pair)[0], (float) (double) (*pair)[1]);
    std::stable_sort (out.begin(), out.end(), [] (auto& a, auto& b) { return a.first < b.first; });
    return out;
}

}  // namespace tt
