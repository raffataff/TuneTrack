// Lanes: source strips computed side by side on several cores.
//
// A source strip whose inserts listen to no other strip (no sidechain key) gets a graph of its own, a lane: the
// MIDI source or stem, the instrument and the inserts, up to the point before the fader. Before the session's
// graph runs a block, LanePool computes every lane's block, spread over worker threads, and the strip's LaneNode
// hands the result to the session's graph. Keyed strips, buses and the master stay in the session's graph, where
// JUCE lines up sidechain keys and plugin latency as before. Lanes depend on nothing but the transport, so the
// result is the same whichever thread computes them and in whatever order.
#pragma once

#include "Automation.h"

namespace tt
{
struct Lane
{
    using Graph = juce::AudioProcessorGraph;

    explicit Lane (Transport&);

    const juce::uint32 serial;  // tells a lane apart from an earlier one for the same track
    Graph graph;
    Graph::Node::Ptr outNode;
    std::vector<Graph::Connection> wired;  // message thread: what the graph is wired with now
    int weight = 1;                        // message thread: heavier lanes are started first
    Shared<AutoPlan> autos;                // its plugins' automated parameters, set before each block
    juce::String autoSig;                  // message thread: what `autos` was made from

    // Message thread, while the lane is not being computed. The lane's graph never runs in JUCE's non-realtime
    // mode (which waits for a render sequence and could block a worker on the message thread); its devices are
    // told instead.
    void prepare (double sampleRate, int blockSize);
    void setNonRealtime (bool);
    double preparedRate = 0.0;
    int preparedBlock = 0;

    // One thread at a time: compute this block (the strip before its fader) into `audio`.
    void process (int numSamples, juce::uint32 generation);
    juce::AudioBuffer<float> audio;
    juce::uint32 generation = 0;  // the LanePool block `audio` belongs to

private:
    Transport& transport;
    juce::MidiBuffer midi;
};

struct LaneSchedule
{
    std::vector<std::shared_ptr<Lane>> lanes;
};

class LanePool
{
public:
    LanePool();
    ~LanePool();

    // Message thread, while no audio runs. n = threads that compute lanes, the calling one included (1 = no workers).
    void setThreads (int n);
    int threads() const { return (int) workers.size() + 1; }

    // Audio thread (the message thread offline): compute every lane of the schedule (none = nullptr) for one block,
    // on this thread and the workers; returns once all are done.
    void run (const LaneSchedule*, int numSamples);
    juce::uint32 generation() const { return gen; }

private:
    class Worker;
    void work();

    std::vector<std::unique_ptr<Worker>> workers;
    // Which block is being computed (high 32 bits) and the next lane to take (low 32). A thread takes lane i by
    // moving the claim from (block, i) to (block, i + 1), so a worker that wakes late for a block already finished
    // cannot take a lane of the next one with the old block's details.
    std::atomic<juce::uint64> claim { 0 };
    std::atomic<int> done { 0 };
    std::atomic<const LaneSchedule*> job { nullptr };
    std::atomic<int> jobSamples { 0 }, jobCount { 0 };
    juce::uint32 gen = 0;
};

// A lane's output inside the session's graph: what the lane computed for this block.
class LaneNode : public TtProcessor
{
public:
    LaneNode (std::shared_ptr<Lane> l, const LanePool& p) : TtProcessor ("lane", 0, 2), lane (std::move (l)), pool (p) {}

    const std::shared_ptr<Lane> lane;

    // Message thread: report the lane's latency (its plugins'), so the session's graph lines the strip up.
    void syncLatency() { setLatencySamples (lane->graph.getLatencySamples()); }

    void prepareToPlay (double sr, int block) override;
    void setNonRealtime (bool) noexcept override;
    void reset() override { lane->graph.reset(); }
    void processBlock (juce::AudioBuffer<float>&, juce::MidiBuffer&) override;

private:
    const LanePool& pool;
};

// A stereo signal as one channel, (L + R) / 2: the key for an effect whose sidechain input is mono, as the
// offline worker feeds it.
class MonoNode : public TtProcessor
{
public:
    MonoNode() : TtProcessor ("mono", 2, 1) {}

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        const int n = buf.getNumSamples();
        if (buf.getNumChannels() < 2)
            return;
        buf.addFrom (0, 0, buf, 1, 0, n);
        buf.applyGain (0, 0, n, 0.5f);
    }
};

}  // namespace tt
