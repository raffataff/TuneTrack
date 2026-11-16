#include "SfzNode.h"

#if TT_HAVE_SFIZZ
 #include <sfizz.hpp>

namespace tt_sfizz
{
std::uint32_t swapRandom (std::uint32_t state);  // SfizzRandom.cpp, compiled into sfizz
}
#endif

namespace tt
{
#if TT_HAVE_SFIZZ

bool sfzAvailable() { return true; }

namespace
{
// The node's own random generator in this thread's place for as long as it lives (see SfizzRandom.cpp).
struct OwnRandom
{
    explicit OwnRandom (std::uint32_t& s) : state (s), saved (tt_sfizz::swapRandom (s)) {}
    ~OwnRandom() { state = tt_sfizz::swapRandom (saved); }
    std::uint32_t& state;
    std::uint32_t saved;
};

constexpr std::uint32_t seed = 7;  // the same start for every SFZ node, as TuneTrack's own player
}  // namespace

struct SfzNode::Impl
{
    sfz::Sfizz synth;
    std::uint32_t random = seed;
    double sampleRate = 0.0;
    int maxBlock = 0;
    bool freewheel = false;
    bool fullVolume = false;

    // sfizz links CC7 (channel volume) to the amplitude of an instrument that reads no CC7 itself, and starts it
    // at 100 of 127, the General MIDI default: -4.2 dB. TuneTrack's faders are the volume and its own SFZ player
    // has no such link, so there CC7 starts at 127 unless the file sets its own start (set_cc7). The track's own
    // CC7 events still apply.
    void startControllers()
    {
        synth.cc (0, 121, 0);  // reset all controllers: every CC and the pitch wheel back where the instrument starts them
        if (fullVolume)
            synth.hdcc (0, 7, 1.0f);
    }

    float defaultCC (int cc)
    {
        float value = -1.0f;
        auto client = sfz::Sfizz::createClient (&value);
        sfz::Sfizz::setReceiveCallback (*client, +[] (void* data, int, const char*, const char* sig, const sfizz_arg_t* args)
        {
            if (sig != nullptr && sig[0] == 'f' && args != nullptr)
                *static_cast<float*> (data) = args[0].f;
        });
        const auto path = "/cc" + std::to_string (cc) + "/default";
        synth.sendMessage (*client, 0, path.c_str(), "", nullptr);
        return value;
    }

    void prepare (double sr, int block)
    {
        if (sr != sampleRate)
            synth.setSampleRate ((float) sr);
        if (block != maxBlock)
            synth.setSamplesPerBlock (block);
        sampleRate = sr;
        maxBlock = block;
    }

    void send (int at, const juce::MidiMessage& m)
    {
        if (m.isNoteOn())
            synth.noteOn (at, m.getNoteNumber(), m.getVelocity());
        else if (m.isNoteOff())
            synth.noteOff (at, m.getNoteNumber(), m.getVelocity());
        else if (m.isController())  // all notes / sound off (123, 120) are handled by sfizz as CCs
            synth.cc (at, m.getControllerNumber(), m.getControllerValue());
        else if (m.isPitchWheel())
            synth.pitchWheel (at, m.getPitchWheelValue() - 8192);
        else if (m.isChannelPressure())
            synth.channelAftertouch (at, m.getChannelPressureValue());
        else if (m.isAftertouch())
            synth.polyAftertouch (at, m.getNoteNumber(), m.getAfterTouchValue());
    }
};

SfzNode::SfzNode() : TtProcessor ("sfz", 0, 2), impl (std::make_unique<Impl>()) {}
SfzNode::~SfzNode() = default;

std::unique_ptr<SfzNode> SfzNode::load (const juce::File& file, double sampleRate, int blockSize, int voices,
                                        bool fullCC7, bool offline, juce::String& error)
{
    if (! file.existsAsFile())
    {
        error = "SFZ file not found: " + file.getFullPathName();
        return nullptr;
    }
    std::unique_ptr<SfzNode> node (new SfzNode());
    auto& im = *node->impl;
    const OwnRandom own (im.random);  // voices made while loading take their seeds from the node's generator
    im.synth.setNumVoices (juce::jlimit (1, 256, voices));
    im.synth.setVolume (0.0f);  // sfizz starts at -7.35 dB for headroom; the mixer has its own
    im.prepare (sampleRate, blockSize);
    // a node added to a graph is not told the graph runs offline (Session::loadPlugin does the same for plugins)
    node->setNonRealtime (offline);
    if (offline)
        im.synth.enableFreeWheeling();
    im.freewheel = offline;
    trace ("sfz load " + file.getFullPathName());
    if (! im.synth.loadSfzFile (file.getFullPathName().toStdString()))
    {
        error = "sfizz could not load " + file.getFullPathName();
        return nullptr;
    }
    if (im.synth.getNumRegions() == 0)
    {
        error = file.getFileName() + " has no regions sfizz can play";
        return nullptr;
    }
    im.fullVolume = fullCC7 && std::abs (im.defaultCC (7) - 100.0f / 127.0f) < 1.0e-4f;  // a set_cc7 of its own stays
    im.startControllers();
    trace ("sfz loaded: " + juce::String (im.synth.getNumRegions()) + " regions, "
           + juce::String ((int) im.synth.getNumPreloadedSamples()) + " samples");
    return node;
}

void SfzNode::prepareToPlay (double sr, int block)
{
    const OwnRandom own (impl->random);
    impl->prepare (sr, block);
}

void SfzNode::reset()
{
    {
        const OwnRandom own (impl->random);
        impl->synth.allSoundOff();
        impl->startControllers();
    }
    impl->random = seed;
}

void SfzNode::processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer& midi)
{
    auto& im = *impl;
    const int n = buf.getNumSamples();
    buf.clear();
    if (n == 0 || buf.getNumChannels() < 2 || im.maxBlock <= 0)
    {
        midi.clear();
        return;
    }
    const OwnRandom own (im.random);
    // offline, a note waits for its sample to load instead of starting late or silent
    if (const bool off = isNonRealtime(); off != im.freewheel)
    {
        off ? im.synth.enableFreeWheeling() : im.synth.disableFreeWheeling();
        im.freewheel = off;
    }
    if (auto* head = getPlayHead())
        if (const auto pos = head->getPosition())
        {
            const auto ts = pos->getTimeSignature().orFallback (juce::AudioPlayHead::TimeSignature {});
            const double ppq = pos->getPpqPosition().orFallback (0.0);
            const double barStart = pos->getPpqPositionOfLastBarStart().orFallback (0.0);
            const double perBeat = 4.0 / juce::jmax (1, ts.denominator);
            im.synth.bpmTempo (0, (float) pos->getBpm().orFallback (120.0));
            im.synth.timeSignature (0, ts.numerator, ts.denominator);
            im.synth.timePosition (0, (int) pos->getBarCount().orFallback (0), (ppq - barStart) / perBeat);
            im.synth.playbackState (0, pos->getIsPlaying() ? 1 : 0);
        }
    auto ev = midi.cbegin();
    for (int done = 0; done < n;)
    {
        const int len = juce::jmin (n - done, im.maxBlock);
        for (; ev != midi.cend() && (*ev).samplePosition < done + len; ++ev)
        {
            const auto meta = *ev;
            im.send (juce::jlimit (0, len - 1, meta.samplePosition - done), meta.getMessage());
        }
        float* out[2] = { buf.getWritePointer (0, done), buf.getWritePointer (1, done) };
        im.synth.renderBlock (out, (size_t) len, 1);
        done += len;
    }
    midi.clear();
}

#else  // built without sfizz

bool sfzAvailable() { return false; }

struct SfzNode::Impl {};
SfzNode::SfzNode() : TtProcessor ("sfz", 0, 2) {}
SfzNode::~SfzNode() = default;

std::unique_ptr<SfzNode> SfzNode::load (const juce::File&, double, int, int, bool, bool, juce::String& error)
{
    error = "this engine was built without sfizz (engine/README.md)";
    return nullptr;
}

void SfzNode::prepareToPlay (double, int) {}
void SfzNode::reset() {}
void SfzNode::processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer& midi)
{
    buf.clear();
    midi.clear();
}

#endif

}  // namespace tt
