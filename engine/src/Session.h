// The playing session: builds and re-builds the audio graph from a spec written by the Python side
// (tunetrack/live.py), keeping every device that has not changed so edits land without reloading plugins.
#pragma once

#include "FaustNodes.h"
#include "Nodes.h"

#include <functional>

namespace tt
{
class Session
{
public:
    using Graph = juce::AudioProcessorGraph;

    explicit Session (Transport&);
    ~Session();

    // (Re)prepare for a sample rate and block size. offline = faster than real time.
    void prepare (double sampleRate, int blockSize, bool offline);

    // Message thread. Returns { warnings: [...], latency: samples, nodes: n, settle_ms: n }.
    juce::var apply (const juce::var& spec);

    // Plugins that were given a saved state finish loading on the message thread; parameter overrides for them
    // wait until then. Returns the ms still to wait (0 = nothing pending); finishSettle applies what waited.
    int settleRemainingMs() const;
    juce::StringArray finishSettle();

    juce::var peaks();
    void resetBuiltIns();  // clear the state of every built-in synth and effect (not the hosted plugins)
    int latency() const { return totalLatency; }

    // The "is this instrument ready" probe, offline only. A plugin instrument cannot say when it has finished
    // loading its samples, so each new one is played a note from its own part (the median pitch, at that note's
    // velocity) and measured after the instrument, before its inserts. probeBegin arms every instrument not yet
    // heard to settle and returns how many; the caller runs the graph, stopped, for `length` samples and a block
    // more; probeEnd measures. An instrument is ready once it sounds and is no louder than on the probe before
    // (still loading = getting louder). probeEnd returns how many are not ready; giveUpProbes marks them ready
    // and says why each was not.
    bool hasUnproven() const;
    int probeBegin (juce::int64 length);
    int probeEnd();
    juce::StringArray giveUpProbes();

    // Write strips' audio before the fader ({strip id: wav path}) during the next offline render, `length` samples
    // from song position 0. closeTaps finishes the files and returns {strip id: peak}.
    juce::String armTaps (const juce::var& taps, juce::int64 length);
    juce::var closeTaps();

    // ref: "<track id>/inst", "<track id>/fx/<n>", "master/fx/<n>". Empty string on success, else the problem.
    juce::String openEditor (const juce::String& ref);
    void closeEditors();
    juce::String saveState (const juce::String& ref, const juce::File& to);

    // Writes the plugins found in a VST3 file to an XML file. Run in a process of its own (see loadPlugin).
    static bool describe (const juce::String& pluginPath, const juce::File& out);

    Graph graph;
    double sampleRate = 48000.0;
    int blockSize = 512;
    bool offline = false;
    bool perSampleSynths = false;

private:
    struct Slot
    {
        Graph::Node::Ptr node;
        juce::String ref, applied, key;
        juce::var deferred;
        bool fresh = false, plugin = false;
        bool ready = false, probeSilent = true;  // the ready probe (instruments only)
        float probeDb = -1000.0f;
        int probes = 0;
    };
    struct Strip { Graph::Node::Ptr head, tap, fader; };
    struct ProbeWant { juce::String slot, ref; Graph::Node::Ptr midi, meter; int channel, note, velocity; };
    class EditorWindow;

    using Maker = std::function<std::unique_ptr<juce::AudioProcessor> (juce::String&)>;
    Slot* take (const juce::String& owner, const juce::String& identity, const juce::String& ref, const Maker&);
    Slot* device (const juce::String& owner, const juce::String& ref, const juce::var& dev, bool instrument, bool keyed);
    std::unique_ptr<juce::AudioPluginInstance> loadPlugin (const juce::var& dev, bool instrument, bool keyed, juce::String& error);
    void applyPluginParams (Slot&, const juce::var& params);
    void link (Graph::Node* from, Graph::Node* to);
    void linkKey (Graph::Node* from, Graph::Node* fx);
    void updateLatencies();
    Slot* findRef (const juce::String& ref);

    Transport& transport;
    juce::AudioPluginFormatManager formats;
    std::map<juce::String, std::unique_ptr<juce::OwnedArray<juce::PluginDescription>>> typeCache;
    Graph::Node::Ptr outNode;
    std::map<juce::String, Slot> slots, next;
    std::map<juce::String, int> seen;
    std::map<juce::String, Strip> strips;
    std::vector<ProbeWant> probeWants;
    juce::StringArray tapped;
    std::vector<Graph::Connection> wires, wired;
    std::map<juce::String, std::unique_ptr<EditorWindow>> editors;
    juce::StringArray warnings;
    juce::int64 settleUntil = 0;
    int totalLatency = 0, freshStates = 0;
    bool topologyChanged = false;
};

}  // namespace tt
