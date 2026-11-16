// SFZ instruments as graph nodes, played by the sfizz library (third_party/sfizz, cmake/Sfizz.cmake).
#pragma once

#include "Nodes.h"

namespace tt
{
// Whether this build has sfizz. Without it SFZ tracks reach the engine as stems (tunetrack/live.py).
bool sfzAvailable();

class SfzNode final : public TtProcessor
{
public:
    // Loads the instrument (its samples' first frames; the rest stream in, or load as needed offline), at the
    // level TuneTrack's own SFZ player plays it. fullCC7: the file reads no CC7 itself, so sfizz's own CC7 volume
    // link starts at 127 instead of 100. nullptr with `error` set when it cannot be played.
    static std::unique_ptr<SfzNode> load (const juce::File& sfz, double sampleRate, int blockSize, int voices,
                                          bool fullCC7, bool offline, juce::String& error);
    // Every voice stopped, every controller and the random generator back where they started: a render then
    // starts the same way every time.
    void reset() override;
    ~SfzNode() override;

    bool acceptsMidi() const override { return true; }
    void prepareToPlay (double sampleRate, int blockSize) override;
    void processBlock (juce::AudioBuffer<float>&, juce::MidiBuffer&) override;

private:
    SfzNode();
    struct Impl;
    std::unique_ptr<Impl> impl;
};

}  // namespace tt
