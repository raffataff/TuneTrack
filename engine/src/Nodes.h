// TuneTrack live engine: transport and the engine's own graph nodes (MIDI source, stem player, fader, gain).
#pragma once

#include <juce_audio_utils/juce_audio_utils.h>

#include <array>
#include <atomic>
#include <bitset>
#include <cmath>
#include <limits>
#include <map>
#include <memory>
#include <vector>

namespace tt
{
using juce::int64;

// Progress notes on stderr when TT_TRACE is set: the way to see which plugin call a crash or a hang is in.
inline bool tracing()
{
    static const bool on = juce::SystemStats::getEnvironmentVariable ("TT_TRACE", {}).isNotEmpty();
    return on;
}

inline void trace (const juce::String& what)
{
    if (tracing())
    {
        fputs (what.toRawUTF8(), stderr);
        fputc ('\n', stderr);
        fflush (stderr);
    }
}

// A value the message thread publishes and the audio thread picks up without ever waiting.
template <typename T>
class Shared
{
public:
    void set (std::shared_ptr<const T> v)
    {
        std::shared_ptr<const T> old;
        {
            const juce::SpinLock::ScopedLockType l (lock);
            old = std::move (pending);
            pending = std::move (v);
            dirty.store (true, std::memory_order_release);
        }
    }

    // Audio thread. Returns the current value (may be null before the first set).
    const T* get()
    {
        if (dirty.load (std::memory_order_acquire))
        {
            const juce::SpinLock::ScopedTryLockType l (lock);
            if (l.isLocked())
            {
                std::swap (current, pending);  // the old value is freed by the next set(), off the audio thread
                dirty.store (false, std::memory_order_release);
                changed = true;
            }
        }
        return current.get();
    }

    bool takeChanged() { const bool c = changed; changed = false; return c; }

private:
    juce::SpinLock lock;
    std::shared_ptr<const T> pending, current;
    std::atomic<bool> dirty { false };
    bool changed = false;
};

struct TempoPt { int64 sample; double beat; double bpm; };

// What comes in from outside while a device block is computed: the interface's input channels and the MIDI
// keyboard. Filled by the device callback before the graph runs; empty offline.
struct LiveIn
{
    const float* const* audio = nullptr;  // the device's input channels for this device block
    int channels = 0;
    int offset = 0;                        // where the graph block being computed starts inside the device block
    // Keyboard events, timed inside the device block, by where they came from: 0 = sent to the engine (the
    // on-screen keys), 1.. = the MIDI keyboards in the order they were opened.
    static constexpr int sources = 8;
    std::array<juce::MidiBuffer, sources> midi;

    void clearMidi()
    {
        for (auto& m : midi)
            m.clear();
    }
};

// Song position. The engine fills the block* fields before each graph block; nodes and plugins read them.
class Transport : public juce::AudioPlayHead
{
public:
    double sampleRate = 48000.0;
    int tsNum = 4, tsDen = 4;

    std::atomic<int64> pos { 0 };
    static constexpr int64 noSeek = std::numeric_limits<int64>::min();  // a count-in plays from before sample 0
    std::atomic<int64> seekRequest { noSeek };
    std::atomic<bool> playing { false };
    std::atomic<bool> recording { false };
    std::atomic<bool> click { false };
    std::atomic<int64> countUntil { noSeek };  // the click also plays before this position (a count-in)
    // While recording, what an armed audio track already holds is silent from punchIn to punchOut.
    std::atomic<int64> punchIn { std::numeric_limits<int64>::max() }, punchOut { std::numeric_limits<int64>::max() };

    bool punched (int64 at) const
    {
        return recording.load (std::memory_order_relaxed) && at >= punchIn.load (std::memory_order_relaxed)
               && at < punchOut.load (std::memory_order_relaxed);
    }
    LiveIn in;
    std::atomic<bool> looping { false };
    std::atomic<int64> loopStart { 0 }, loopEnd { 0 }, endSample { 0 };
    Shared<std::vector<TempoPt>> tempo;

    int64 blockPos = 0;
    int blockLen = 0;
    bool blockPlaying = false;
    const std::vector<TempoPt>* tempoNow = nullptr;

    void locate (int64 sample, double& beat, double& bpm) const
    {
        beat = 0.0;
        bpm = 120.0;
        if (tempoNow == nullptr || tempoNow->empty())
        {
            beat = (double) sample / sampleRate * 2.0;
            return;
        }
        const TempoPt* p = &tempoNow->front();
        for (auto& t : *tempoNow)
        {
            if (t.sample > sample)
                break;
            p = &t;
        }
        bpm = p->bpm;
        beat = p->beat + (double) (sample - p->sample) / sampleRate * bpm / 60.0;
    }

    double beatAt (int64 sample) const
    {
        double beat, bpm;
        locate (sample, beat, bpm);
        return beat;
    }

    juce::Optional<PositionInfo> getPosition() const override
    {
        PositionInfo i;
        double beat, bpm;
        locate (blockPos, beat, bpm);
        const double perBar = juce::jmax (1.0, tsNum * 4.0 / juce::jmax (1, tsDen));
        i.setTimeInSamples (blockPos);
        i.setTimeInSeconds ((double) blockPos / sampleRate);
        i.setBpm (bpm);
        i.setPpqPosition (beat);
        i.setPpqPositionOfLastBarStart (std::floor (beat / perBar) * perBar);
        i.setBarCount ((int64) std::floor (beat / perBar));
        i.setTimeSignature (TimeSignature { tsNum, tsDen });
        i.setIsPlaying (blockPlaying);
        i.setIsRecording (recording.load());
        const bool loop = looping.load();
        i.setIsLooping (loop);
        if (loop)
            i.setLoopPoints (LoopPoints { beatAt (loopStart.load()), beatAt (loopEnd.load()) });
        return i;
    }
};

inline juce::AudioChannelSet channelSet (int n)
{
    if (n == 1) return juce::AudioChannelSet::mono();
    if (n == 2) return juce::AudioChannelSet::stereo();
    return juce::AudioChannelSet::discreteChannels (n);
}

class TtProcessor : public juce::AudioProcessor
{
public:
    TtProcessor (const juce::String& n, int ins, int outs) : juce::AudioProcessor (buses (ins, outs)), name (n) {}

    static BusesProperties buses (int ins, int outs)
    {
        BusesProperties b;
        if (ins > 0)  b = b.withInput ("In", channelSet (ins), true);
        if (outs > 0) b = b.withOutput ("Out", channelSet (outs), true);
        return b;
    }

    const juce::String getName() const override { return name; }
    void prepareToPlay (double, int) override {}
    void releaseResources() override {}
    double getTailLengthSeconds() const override { return 0.0; }
    bool acceptsMidi() const override { return false; }
    bool producesMidi() const override { return false; }
    juce::AudioProcessorEditor* createEditor() override { return nullptr; }
    bool hasEditor() const override { return false; }
    int getNumPrograms() override { return 1; }
    int getCurrentProgram() override { return 0; }
    void setCurrentProgram (int) override {}
    const juce::String getProgramName (int) override { return {}; }
    void changeProgramName (int, const juce::String&) override {}
    void getStateInformation (juce::MemoryBlock&) override {}
    void setStateInformation (const void*, int) override {}
    bool isBusesLayoutSupported (const BusesLayout&) const override { return true; }

private:
    juce::String name;
};

// ---- MIDI source: plays a track's events in time with the transport ------------------------------------------

struct MidiEv { int64 sample; juce::uint8 status, d1, d2; };
using MidiSeq = std::vector<MidiEv>;

class MidiSourceNode : public TtProcessor
{
public:
    explicit MidiSourceNode (Transport& t) : TtProcessor ("midi", 0, 0), transport (t) {}
    bool producesMidi() const override { return true; }

    Shared<MidiSeq> seq;

    // A test note for the "is this instrument ready" probe (Session::probeBegin). Offline only: set and read on
    // the message thread while no audio device is running the graph.
    void startProbe (int channel, int note, int velocity, int64 length)
    {
        probe = { channel, note, velocity, length, false };
        probing = true;
    }

    // The MIDI keyboard plays this strip's instrument (an armed track), on the instrument's channel.
    std::atomic<bool> thru { false };
    std::atomic<int> thruChannel { 1 };
    std::atomic<int> thruFrom { 0 };  // the one keyboard that plays it (LiveIn::midi's number); 0 = all of them

    void processBlock (juce::AudioBuffer<float>&, juce::MidiBuffer& midi) override
    {
        midi.clear();
        sequence (midi);
        if (! probing)
            keyboard (midi);
    }

private:
    void keyboard (juce::MidiBuffer& midi)
    {
        const int ch = juce::jlimit (1, 16, thruChannel.load (std::memory_order_relaxed));
        if (! thru.load (std::memory_order_relaxed))
        {
            for (int k = 0; k < 128 && held.any(); ++k)  // disarmed with keys down
                if (held.test ((size_t) k))
                    midi.addEvent (juce::MidiMessage::noteOff (ch, k), 0);
            held.reset();
            return;
        }
        const int from = transport.in.offset, n = transport.blockLen;
        const int only = thruFrom.load (std::memory_order_relaxed);
        for (int src = 0; src < LiveIn::sources; ++src)
        {
            if (only > 0 && src > 0 && src != only)
                continue;  // another keyboard's (what is sent to the engine plays every armed strip)
            for (const auto meta : transport.in.midi[(size_t) src])
            {
                if (meta.samplePosition < from || meta.samplePosition >= from + n)
                    continue;
                auto m = meta.getMessage();
                if (m.isSysEx() || m.getRawDataSize() > 3)
                    continue;
                m.setChannel (ch);
                if (m.isNoteOn())       held.set ((size_t) m.getNoteNumber());
                else if (m.isNoteOff()) held.reset ((size_t) m.getNoteNumber());
                midi.addEvent (m, meta.samplePosition - from);
            }
        }
    }

    void sequence (juce::MidiBuffer& midi)
    {
        const int n = transport.blockLen;
        const int64 pos = transport.blockPos;
        if (probing)
        {
            if (! probe.started)
                midi.addEvent (juce::MidiMessage::noteOn (probe.channel, probe.note, (juce::uint8) probe.velocity), 0);
            probe.started = true;
            if (probe.left <= n)
            {
                midi.addEvent (juce::MidiMessage::noteOff (probe.channel, probe.note), (int) juce::jmax<int64> (0, probe.left - 1));
                probing = false;
            }
            probe.left -= n;
            return;
        }
        const MidiSeq* s = seq.get();
        const bool swapped = seq.takeChanged();

        if (! transport.blockPlaying || s == nullptr)
        {
            if (wasPlaying)
                silence (midi, true);
            wasPlaying = false;
            return;
        }

        if (! wasPlaying || pos != expected || swapped)
        {
            // Jumped (start, seek, loop wrap) or the notes were edited under us: release what should no longer
            // sound, keep what still should, and bring controllers to where they would be by now.
            std::bitset<2048> active;
            std::array<short, 16 * 128> cc;
            std::array<int, 16> bend;
            cc.fill (-1);
            bend.fill (-1);
            size_t i = 0;
            for (; i < s->size() && (*s)[i].sample < pos; ++i)
            {
                const auto& e = (*s)[i];
                const int ch = e.status & 0x0f, kind = e.status & 0xf0;
                if (kind == 0x90 && e.d2 > 0)        active.set ((size_t) (ch * 128 + e.d1));
                else if (kind == 0x80 || kind == 0x90) active.reset ((size_t) (ch * 128 + e.d1));
                else if (kind == 0xb0)               cc[(size_t) (ch * 128 + e.d1)] = e.d2;
                else if (kind == 0xe0)               bend[(size_t) ch] = e.d1 | (e.d2 << 7);
            }
            index = i;
            for (size_t k = 0; k < 2048; ++k)
                if (sounding.test (k) && ! active.test (k))
                {
                    midi.addEvent (juce::MidiMessage::noteOff ((int) (k / 128) + 1, (int) (k % 128)), 0);
                    sounding.reset (k);
                }
            if (! wasPlaying || pos != expected)
            {
                for (size_t k = 0; k < cc.size(); ++k)
                    if (cc[k] >= 0)
                        midi.addEvent (juce::MidiMessage::controllerEvent ((int) (k / 128) + 1, (int) (k % 128), cc[k]), 0);
                for (size_t ch = 0; ch < 16; ++ch)
                    if (bend[ch] >= 0)
                        midi.addEvent (juce::MidiMessage::pitchWheel ((int) ch + 1, bend[ch]), 0);
            }
        }

        const int64 end = pos + n;
        for (; index < s->size() && (*s)[index].sample < end; ++index)
        {
            const auto& e = (*s)[index];
            const int ch = e.status & 0x0f, kind = e.status & 0xf0;
            const int at = (int) juce::jlimit<int64> (0, (int64) juce::jmax (0, n - 1), e.sample - pos);
            if (kind == 0x90 && e.d2 > 0)          sounding.set ((size_t) (ch * 128 + e.d1));
            else if (kind == 0x80 || kind == 0x90) sounding.reset ((size_t) (ch * 128 + e.d1));
            used |= (1u << ch);
            midi.addEvent (juce::MidiMessage (e.status, e.d1, e.d2), at);
        }
        expected = end;
        wasPlaying = true;
    }

    void silence (juce::MidiBuffer& midi, bool pedals)
    {
        for (size_t k = 0; k < 2048; ++k)
            if (sounding.test (k))
                midi.addEvent (juce::MidiMessage::noteOff ((int) (k / 128) + 1, (int) (k % 128)), 0);
        sounding.reset();
        if (pedals)
            for (int ch = 0; ch < 16; ++ch)
                if (used & (1u << ch))
                {
                    midi.addEvent (juce::MidiMessage::controllerEvent (ch + 1, 64, 0), 0);
                    midi.addEvent (juce::MidiMessage::allNotesOff (ch + 1), 0);
                }
    }

    Transport& transport;
    std::bitset<2048> sounding;
    std::bitset<128> held;  // keyboard notes passed through and not yet released
    juce::uint32 used = 0;
    size_t index = 0;
    int64 expected = -1;
    bool wasPlaying = false;
    struct Probe { int channel = 1, note = 60, velocity = 100; int64 left = 0; bool started = false; } probe;
    bool probing = false;
};

// ---- Meter: passes audio through; measures it while the ready probe runs --------------------------------------

class MeterNode : public TtProcessor
{
public:
    MeterNode() : TtProcessor ("meter", 2, 2) {}

    bool on = false;  // message thread, offline only (see MidiSourceNode::startProbe)
    double sum = 0.0;
    int64 count = 0;
    float peak = 0.0f;

    void start() { sum = 0.0; count = 0; peak = 0.0f; on = true; }

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        if (! on)
            return;
        const int n = buf.getNumSamples();
        for (int c = 0; c < juce::jmin (2, buf.getNumChannels()); ++c)
        {
            const float* x = buf.getReadPointer (c);
            for (int i = 0; i < n; ++i)
                if (std::isfinite (x[i]))
                    sum += (double) x[i] * x[i];
            peak = juce::jmax (peak, buf.getMagnitude (c, 0, n));
        }
        count += n;
    }
};

// ---- Stem: plays an audio file that starts at song position 0 -------------------------------------------------

class StemNode : public TtProcessor
{
public:
    StemNode (Transport& t, const juce::File& file) : TtProcessor ("stem", 0, 2), transport (t)
    {
        juce::WavAudioFormat wav;
        mapped.reset (wav.createMemoryMappedReader (file));
        if (mapped != nullptr && ! mapped->mapEntireFile())
            mapped.reset();
        if (mapped == nullptr)
        {
            juce::AudioFormatManager fm;
            fm.registerBasicFormats();
            if (std::unique_ptr<juce::AudioFormatReader> r { fm.createReaderFor (file) })
            {
                memory.setSize (2, (int) r->lengthInSamples);
                r->read (&memory, 0, (int) r->lengthInSamples, 0, true, true);
                ok = true;
            }
        }
        else
            ok = true;
    }

    bool ok = false;
    std::atomic<bool> duck { false };  // an armed audio track: silent between the punch points while recording

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        buf.clear();
        if (! transport.blockPlaying || buf.getNumChannels() < 2)
            return;
        const int n = buf.getNumSamples();
        const float target = duck.load (std::memory_order_relaxed) && transport.punched (transport.blockPos) ? 0.0f : 1.0f;
        const float before = gain;
        gain = target;
        if (before == 0.0f && target == 0.0f)
            return;
        const int skip = (int) juce::jlimit<int64> (0, n, -transport.blockPos);  // a count-in starts before sample 0
        const int64 pos = transport.blockPos + skip;
        const int64 len = mapped != nullptr ? mapped->lengthInSamples : memory.getNumSamples();
        const int k = (int) juce::jmin<int64> (n - skip, len - pos);
        if (k <= 0)
            return;
        if (mapped != nullptr)
            mapped->read (&buf, skip, k, pos, true, true);
        else
            for (int c = 0; c < 2; ++c)
                buf.copyFrom (c, skip, memory, c, (int) pos, k);
        if (before != 1.0f || target != 1.0f)
            buf.applyGainRamp (0, juce::jmin (n, 256), before, target);  // a few ms, no click
        if (target == 0.0f && n > 256)
            buf.clear (256, n - 256);
    }

private:
    Transport& transport;
    float gain = 1.0f;
    std::unique_ptr<juce::MemoryMappedAudioFormatReader> mapped;
    juce::AudioBuffer<float> memory;
};

// ---- Input: the interface's input channels, heard through the strip while monitoring ---------------------------

class InputNode : public TtProcessor
{
public:
    explicit InputNode (Transport& t) : TtProcessor ("input", 0, 2), transport (t) {}

    std::atomic<int> left { 0 }, right { -1 };  // device channels; right < 0: one channel on both sides
    std::atomic<bool> monitor { false };

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        buf.clear();
        const auto& in = transport.in;
        const int n = buf.getNumSamples();
        const float target = monitor.load (std::memory_order_relaxed) ? 1.0f : 0.0f;
        if (in.audio == nullptr || buf.getNumChannels() < 2 || (gain == 0.0f && target == 0.0f))
        {
            gain = target;
            return;
        }
        const int l = left.load (std::memory_order_relaxed);
        int r = right.load (std::memory_order_relaxed);
        if (r < 0)
            r = l;
        const int chans[2] = { l, r };
        for (int c = 0; c < 2; ++c)
            if (chans[c] >= 0 && chans[c] < in.channels && in.audio[chans[c]] != nullptr)
                buf.addFromWithRamp (c, 0, in.audio[chans[c]] + in.offset, n, gain, target);
        gain = target;
    }

private:
    Transport& transport;
    float gain = 0.0f;
};

// ---- Click: the metronome, a blip on every beat with the bar's first one higher --------------------------------

class ClickNode : public TtProcessor
{
public:
    explicit ClickNode (Transport& t) : TtProcessor ("click", 0, 2), transport (t) {}

    std::atomic<float> level { 0.35f };

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        buf.clear();
        const int n = buf.getNumSamples();
        const bool on = transport.click.load (std::memory_order_relaxed)
                        || transport.blockPos < transport.countUntil.load (std::memory_order_relaxed);
        if (! on || ! transport.blockPlaying || buf.getNumChannels() < 2 || n == 0)
        {
            left = 0;
            return;
        }
        const double sr = transport.sampleRate;
        const double perBar = juce::jmax (1.0, transport.tsNum * 4.0 / juce::jmax (1, transport.tsDen));
        const double b0 = transport.beatAt (transport.blockPos), b1 = transport.beatAt (transport.blockPos + n);
        double next = std::ceil (b0 - 1.0e-9);
        float* l = buf.getWritePointer (0);
        float* r = buf.getWritePointer (1);
        const float g = level.load (std::memory_order_relaxed);
        for (int i = 0; i < n; ++i)
        {
            if (next < b1 && b1 > b0 && i >= (int) ((next - b0) / (b1 - b0) * n))
            {
                const double inBar = next - std::floor (next / perBar + 1.0e-9) * perBar;
                step = juce::MathConstants<double>::twoPi * (std::abs (inBar) < 1.0e-6 ? 1760.0 : 1174.66) / sr;
                phase = 0.0;
                total = left = (int) (0.035 * sr);
                next += 1.0;
            }
            if (left > 0)
            {
                const float env = (float) left / (float) total;
                const float v = g * env * env * (float) std::sin (phase);
                phase += step;
                --left;
                l[i] = v;
                r[i] = v;
            }
        }
    }

private:
    Transport& transport;
    double phase = 0.0, step = 0.0;
    int left = 0, total = 1;
};

// ---- Fader: volume, balance pan, automation, on/off, peak meter -----------------------------------------------

using AutoPts = std::vector<std::pair<int64, float>>;

struct FaderParams
{
    float db = 0.0f, pan = 0.0f;
    bool on = true;
    AutoPts volAuto, panAuto;
    // A fader being moved on an automated strip (the "mix" op): the curve plays this much higher (dB) or further
    // over (pan). The spec never sets it: saving the move shifts the curve's points instead.
    float trim = 0.0f, panTrim = 0.0f;
};

inline float autoValue (const AutoPts& pts, int64 t, size_t& hint)
{
    // same as numpy.interp: straight lines between points, held flat before the first and after the last
    if (t <= pts.front().first) return pts.front().second;
    if (t >= pts.back().first)  return pts.back().second;
    if (hint + 1 >= pts.size() || pts[hint].first > t)
        hint = 0;
    while (hint + 2 < pts.size() && pts[hint + 1].first <= t)
        ++hint;
    const auto& a = pts[hint];
    const auto& b = pts[hint + 1];
    if (b.first == a.first) return b.second;
    return a.second + (b.second - a.second) * (float) ((double) (t - a.first) / (double) (b.first - a.first));
}

class FaderNode : public TtProcessor
{
public:
    explicit FaderNode (Transport& t) : TtProcessor ("fader", 2, 2), transport (t) {}

    Shared<FaderParams> params;
    std::shared_ptr<const FaderParams> published;  // message thread: the last value given to params

    void publish (std::shared_ptr<const FaderParams> p)
    {
        published = p;
        params.set (std::move (p));
    }

    std::atomic<int> latencyBefore { 0 };  // how late the audio arriving here is, so automation stays in time
    std::atomic<float> peak[2] { 0.0f, 0.0f };
    std::atomic<int> bad { 0 };  // samples that arrived as NaN / infinity and were silenced

    // Offline renders only (message thread, no device running): the strip's audio before the fader, written to a
    // file from song position 0 for `left` samples. The first `skip` samples are the latency of the plugins
    // before this point, so the file lines up with the music as the offline renderer's per-track files do.
    std::unique_ptr<juce::AudioFormatWriter> tap;
    int64 tapSkip = 0, tapLeft = 0;
    float tapPeak = 0.0f;

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        const FaderParams* p = params.get();
        const int n = buf.getNumSamples();
        if (p == nullptr || buf.getNumChannels() < 2 || n == 0)
            return;
        float* l = buf.getWritePointer (0);
        float* r = buf.getWritePointer (1);
        // A plugin that puts out NaN or infinity must not poison the buses it feeds (the offline renderer cleans
        // each track the same way before mixing).
        int dirty = 0;
        for (int i = 0; i < n; ++i)
        {
            if (! std::isfinite (l[i])) { l[i] = 0.0f; ++dirty; }
            if (! std::isfinite (r[i])) { r[i] = 0.0f; ++dirty; }
        }
        if (dirty > 0)
            bad.fetch_add (dirty, std::memory_order_relaxed);
        if (tap != nullptr && tapLeft > 0)
        {
            const int from = (int) juce::jmin<int64> (tapSkip, n);
            tapSkip -= from;
            const int k = (int) juce::jmin<int64> (tapLeft, n - from);
            if (k > 0)
            {
                tap->writeFromAudioSampleBuffer (buf, from, k);
                tapPeak = juce::jmax (tapPeak, buf.getMagnitude (from, k));
                tapLeft -= k;
            }
        }
        const float on = p->on ? 1.0f : 0.0f;
        const int64 t0 = transport.blockPos - latencyBefore.load (std::memory_order_relaxed);
        const bool moving = transport.blockPlaying;

        if (p->volAuto.empty() && p->panAuto.empty())
        {
            const float g = std::pow (10.0f, p->db / 20.0f) * on;
            const float pan = juce::jlimit (-1.0f, 1.0f, p->pan);
            const float tl = g * juce::jmin (1.0f, 1.0f - pan), tr = g * juce::jmin (1.0f, 1.0f + pan);
            if (! primed) { gl = tl; gr = tr; primed = true; }
            if (gl == tl && gr == tr)
            {
                for (int i = 0; i < n; ++i) { l[i] *= tl; r[i] *= tr; }
            }
            else
            {
                const float dl = (tl - gl) / (float) n, dr = (tr - gr) / (float) n;
                for (int i = 0; i < n; ++i) { gl += dl; gr += dr; l[i] *= gl; r[i] *= gr; }
                gl = tl; gr = tr;
            }
            onGain = on;
        }
        else
        {
            if (! primed) { onGain = on; trimNow = p->trim; panTrimNow = p->panTrim; primed = true; }
            const float dOn = (on - onGain) / (float) n;
            const float dTrim = (p->trim - trimNow) / (float) n, dPanTrim = (p->panTrim - panTrimNow) / (float) n;
            for (int i = 0; i < n; ++i)
            {
                const int64 t = moving ? t0 + i : t0;
                trimNow += dTrim;
                panTrimNow += dPanTrim;
                const float db = p->volAuto.empty() ? p->db : autoValue (p->volAuto, t, volHint) + trimNow;
                const float pan = juce::jlimit (-1.0f, 1.0f, p->panAuto.empty() ? p->pan : autoValue (p->panAuto, t, panHint) + panTrimNow);
                onGain += dOn;
                const float g = std::pow (10.0f, db / 20.0f) * onGain;
                gl = g * juce::jmin (1.0f, 1.0f - pan);
                gr = g * juce::jmin (1.0f, 1.0f + pan);
                l[i] *= gl;
                r[i] *= gr;
            }
            onGain = on;
            trimNow = p->trim;
            panTrimNow = p->panTrim;
        }

        const auto ml = buf.getMagnitude (0, 0, n), mr = buf.getMagnitude (1, 0, n);
        if (ml > peak[0].load (std::memory_order_relaxed)) peak[0].store (ml, std::memory_order_relaxed);
        if (mr > peak[1].load (std::memory_order_relaxed)) peak[1].store (mr, std::memory_order_relaxed);
    }

private:
    Transport& transport;
    float gl = 1.0f, gr = 1.0f, onGain = 1.0f, trimNow = 0.0f, panTrimNow = 0.0f;
    bool primed = false;
    size_t volHint = 0, panHint = 0;
};

// ---- Gain: a send level, or a unity summing point at the head of a bus ---------------------------------------

class GainNode : public TtProcessor
{
public:
    GainNode() : TtProcessor ("gain", 2, 2) {}
    std::atomic<float> gain { 1.0f };

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        const float target = gain.load (std::memory_order_relaxed);
        const int n = buf.getNumSamples();
        if (! primed) { cur = target; primed = true; }
        if (cur == target)
        {
            if (target != 1.0f)
                buf.applyGain (target);
        }
        else
        {
            buf.applyGainRamp (0, n, cur, target);
            cur = target;
        }
    }

private:
    float cur = 1.0f;
    bool primed = false;
};

}  // namespace tt
