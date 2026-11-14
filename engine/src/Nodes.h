// TuneTrack live engine: transport and the engine's own graph nodes (MIDI source, stem player, fader, gain).
#pragma once

#include <juce_audio_utils/juce_audio_utils.h>

#include <atomic>
#include <bitset>
#include <cmath>
#include <map>
#include <memory>
#include <vector>

namespace tt
{
using juce::int64;

// Progress notes on stderr when TT_TRACE is set: the way to see which plugin call a crash or a hang is in.
inline void trace (const juce::String& what)
{
    static const bool on = juce::SystemStats::getEnvironmentVariable ("TT_TRACE", {}).isNotEmpty();
    if (on)
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

// Song position. The engine fills the block* fields before each graph block; nodes and plugins read them.
class Transport : public juce::AudioPlayHead
{
public:
    double sampleRate = 48000.0;
    int tsNum = 4, tsDen = 4;

    std::atomic<int64> pos { 0 };
    std::atomic<int64> seekRequest { -1 };
    std::atomic<bool> playing { false };
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
        i.setIsRecording (false);
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

    void processBlock (juce::AudioBuffer<float>&, juce::MidiBuffer& midi) override
    {
        midi.clear();
        const int n = transport.blockLen;
        const int64 pos = transport.blockPos;
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

private:
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
    juce::uint32 used = 0;
    size_t index = 0;
    int64 expected = -1;
    bool wasPlaying = false;
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

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        buf.clear();
        if (! transport.blockPlaying || buf.getNumChannels() < 2)
            return;
        const int n = buf.getNumSamples();
        const int64 pos = transport.blockPos;
        if (mapped != nullptr)
        {
            const int64 len = mapped->lengthInSamples;
            if (pos < len)
                mapped->read (&buf, 0, (int) juce::jmin<int64> (n, len - pos), pos, true, true);
        }
        else if (pos < memory.getNumSamples())
        {
            const int k = (int) juce::jmin<int64> (n, memory.getNumSamples() - pos);
            for (int c = 0; c < 2; ++c)
                buf.copyFrom (c, 0, memory, c, (int) pos, k);
        }
    }

private:
    Transport& transport;
    std::unique_ptr<juce::MemoryMappedAudioFormatReader> mapped;
    juce::AudioBuffer<float> memory;
};

// ---- Fader: volume, balance pan, automation, on/off, peak meter -----------------------------------------------

using AutoPts = std::vector<std::pair<int64, float>>;

struct FaderParams
{
    float db = 0.0f, pan = 0.0f;
    bool on = true;
    AutoPts volAuto, panAuto;
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
    std::atomic<int> latencyBefore { 0 };  // how late the audio arriving here is, so automation stays in time
    std::atomic<float> peak[2] { 0.0f, 0.0f };
    std::atomic<int> bad { 0 };  // samples that arrived as NaN / infinity and were silenced

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
            if (! primed) { onGain = on; primed = true; }
            const float dOn = (on - onGain) / (float) n;
            for (int i = 0; i < n; ++i)
            {
                const int64 t = moving ? t0 + i : t0;
                const float db = p->volAuto.empty() ? p->db : autoValue (p->volAuto, t, volHint);
                const float pan = juce::jlimit (-1.0f, 1.0f, p->panAuto.empty() ? p->pan : autoValue (p->panAuto, t, panHint));
                onGain += dOn;
                const float g = std::pow (10.0f, db / 20.0f) * onGain;
                gl = g * juce::jmin (1.0f, 1.0f - pan);
                gr = g * juce::jmin (1.0f, 1.0f + pan);
                l[i] *= gl;
                r[i] *= gr;
            }
            onGain = on;
        }

        const auto ml = buf.getMagnitude (0, 0, n), mr = buf.getMagnitude (1, 0, n);
        if (ml > peak[0].load (std::memory_order_relaxed)) peak[0].store (ml, std::memory_order_relaxed);
        if (mr > peak[1].load (std::memory_order_relaxed)) peak[1].store (mr, std::memory_order_relaxed);
    }

private:
    Transport& transport;
    float gl = 1.0f, gr = 1.0f, onGain = 1.0f;
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
