#include "Lanes.h"

#include <immintrin.h>
#include <thread>

namespace tt
{
// ---- Lane ----------------------------------------------------------------------------------------------------

Lane::Lane (Transport& t) : serial ([] { static std::atomic<juce::uint32> n { 0 }; return ++n; }()), transport (t)
{
    graph.setPlayHead (&t);
    outNode = graph.addNode (std::make_unique<Graph::AudioGraphIOProcessor> (Graph::AudioGraphIOProcessor::audioOutputNode));
}

void Lane::prepare (double sr, int block)
{
    graph.releaseResources();
    graph.setPlayConfigDetails (0, 2, sr, block);
    graph.prepareToPlay (sr, block);
    audio.setSize (2, block, false, true, false);
    midi.ensureSize (4096);
    preparedRate = sr;
    preparedBlock = block;
}

void Lane::setNonRealtime (bool off)
{
    for (auto* n : graph.getNodes())
        n->getProcessor()->setNonRealtime (off);
}

void Lane::process (int n, juce::uint32 g)
{
    const juce::ScopedNoDenormals noDenormals;  // per thread: every lane computes the same on any of them
    n = juce::jmin (n, audio.getNumSamples());
    float* chans[2] = { audio.getWritePointer (0), audio.getWritePointer (1) };
    juce::AudioBuffer<float> view (chans, 2, n);
    view.clear();
    midi.clear();
    applyPlan (autos, transport);
    graph.processBlock (view, midi);
    generation = g;
}

// ---- LaneNode ------------------------------------------------------------------------------------------------

void LaneNode::prepareToPlay (double sr, int block)
{
    if (lane->preparedRate != sr || lane->preparedBlock != block)
        lane->prepare (sr, block);
    syncLatency();
}

void LaneNode::setNonRealtime (bool off) noexcept
{
    TtProcessor::setNonRealtime (off);
    lane->setNonRealtime (off);
}

void LaneNode::processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&)
{
    buf.clear();
    // a lane left out of this block (the schedule changed a block before the graph did) is silent, not stale
    if (lane->generation != pool.generation())
        return;
    const int n = juce::jmin (buf.getNumSamples(), lane->audio.getNumSamples());
    for (int c = 0; c < juce::jmin (2, buf.getNumChannels()); ++c)
        buf.copyFrom (c, 0, lane->audio, c, 0, n);
}

// ---- LanePool ------------------------------------------------------------------------------------------------

class LanePool::Worker final : public juce::Thread
{
public:
    Worker (LanePool& p, int i) : juce::Thread ("tt lane " + juce::String (i)), pool (p) {}

    void run() override
    {
        while (! threadShouldExit())
            if (wake.wait (200))
                pool.work();
    }

    juce::WaitableEvent wake;

private:
    LanePool& pool;
};

LanePool::LanePool() = default;

LanePool::~LanePool()
{
    setThreads (1);
}

void LanePool::setThreads (int n)
{
    n = juce::jlimit (1, 64, n);
    if (n == threads())
        return;
    for (auto& w : workers)
        w->signalThreadShouldExit();
    for (auto& w : workers)
    {
        w->wake.signal();
        w->stopThread (2000);
    }
    workers.clear();
    for (int i = 1; i < n; ++i)
    {
        auto w = std::make_unique<Worker> (*this, i);
        if (! w->startRealtimeThread (juce::Thread::RealtimeOptions{}.withPriority (9)))
            w->startThread (juce::Thread::Priority::highest);
        workers.push_back (std::move (w));
    }
}

void LanePool::run (const LaneSchedule* s, int n)
{
    ++gen;
    const int count = s != nullptr ? (int) s->lanes.size() : 0;
    if (count == 0)
        return;
    if (workers.empty() || count == 1)
    {
        for (auto& l : s->lanes)
            l->process (n, gen);
        return;
    }
    job.store (s, std::memory_order_relaxed);
    jobSamples.store (n, std::memory_order_relaxed);
    jobCount.store (count, std::memory_order_relaxed);
    done.store (0, std::memory_order_relaxed);
    claim.store ((juce::uint64) gen << 32, std::memory_order_release);
    const int wake = juce::jmin ((int) workers.size(), count - 1);
    for (int i = 0; i < wake; ++i)
        workers[(size_t) i]->wake.signal();
    work();
    // what is left is in the hands of workers already computing it: wait for them, never for one to wake up
    for (int spins = 0; done.load (std::memory_order_acquire) < count; ++spins)
    {
        if (spins < 2000)
            _mm_pause();
        else
            std::this_thread::yield();
    }
}

void LanePool::work()
{
    for (;;)
    {
        auto c = claim.load (std::memory_order_acquire);
        const auto block = (juce::uint32) (c >> 32);
        const int i = (int) (c & 0xffffffffu);
        // read before taking the lane; if the claim moved on meanwhile, the CAS fails and these are not used
        const auto* s = job.load (std::memory_order_relaxed);
        const int n = jobSamples.load (std::memory_order_relaxed);
        const int count = jobCount.load (std::memory_order_relaxed);
        if (s == nullptr || i >= count)
            return;
        if (! claim.compare_exchange_weak (c, c + 1, std::memory_order_acq_rel, std::memory_order_acquire))
            continue;
        s->lanes[(size_t) i]->process (n, block);
        done.fetch_add (1, std::memory_order_release);
    }
}

}  // namespace tt
