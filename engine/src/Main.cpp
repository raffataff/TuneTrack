// tunetrack-engine: TuneTrack's live audio engine.
//
// One process hosts the whole session (VST3 plugins, built-in synths and effects, stems) and either plays it
// through the audio interface or renders it faster than real time. The Python side drives it with one JSON
// command per line on stdin; replies and events come back on stdout as lines starting with "@@ ".
//
//   tunetrack-engine [--no-device] [--device <name part>] [--inputs] [--per-sample] [--threads <n>]
//   tunetrack-engine --render <spec.json> <out.wav> [--per-sample] [--threads <n>]
//   tunetrack-engine --devices
//   tunetrack-engine --describe <plugin.vst3> <out.xml>   (used by the engine itself)
#include "Recorder.h"
#include "Resampler.h"
#include "Session.h"

#include <iostream>
#include <thread>

#if JUCE_WINDOWS
 #include <windows.h>
 #include <dbghelp.h>

// A plugin that crashes takes the engine with it. Say which module it was in (and the calls that led there)
// before going, so the Python side can name the plugin instead of reporting a silent exit.
static void describeAddress (void* addr, char* out, size_t size)
{
    static const bool symbols = SymInitialize (GetCurrentProcess(), nullptr, TRUE) != 0;
    HMODULE mod = nullptr;
    char path[MAX_PATH] = "?";
    if (GetModuleHandleExA (GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCSTR) addr, &mod) && mod != nullptr)
        GetModuleFileNameA (mod, path, MAX_PATH);
    const char* file = path;
    for (char* c = path; *c != 0; ++c)
        if (*c == '\\' || *c == '/')
        {
            *c = '/';
            file = c + 1;
        }
    char buffer[sizeof (SYMBOL_INFO) + 256] = {};
    auto* sym = (SYMBOL_INFO*) buffer;
    sym->SizeOfStruct = sizeof (SYMBOL_INFO);
    sym->MaxNameLen = 255;
    DWORD64 off = 0;
    if (symbols && SymFromAddr (GetCurrentProcess(), (DWORD64) addr, &off, sym))
        snprintf (out, size, "%s+%llx %s", file, (unsigned long long) ((char*) addr - (char*) mod), sym->Name);
    else
        snprintf (out, size, "%s+%llx", file, (unsigned long long) ((char*) addr - (char*) mod));
}

static void printStack (const char* title, void* at, unsigned long code)
{
    char where[MAX_PATH + 320];
    describeAddress (at, where, sizeof (where));
    fprintf (stderr, "%s %08lx at %s\n", title, code, where);
    void* frames[48];
    const auto n = CaptureStackBackTrace (0, 48, frames, nullptr);
    for (int i = 0; i < (int) n; ++i)
    {
        describeAddress (frames[i], where, sizeof (where));
        fprintf (stderr, "  %2d %s\n", i, where);
    }
    fflush (stderr);
}

// With TT_TRACE set: every access violation as it happens, even one a plugin goes on to catch.
static LONG WINAPI firstChance (EXCEPTION_POINTERS* info)
{
    static int shown = 0;
    if (info->ExceptionRecord->ExceptionCode == 0xC0000005 && shown++ < 3)
    {
        printStack ("first-chance", info->ExceptionRecord->ExceptionAddress, info->ExceptionRecord->ExceptionCode);
        fflush (stderr);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static LONG WINAPI crashFilter (EXCEPTION_POINTERS* info)
{
    char where[MAX_PATH + 320];
    describeAddress (info->ExceptionRecord->ExceptionAddress, where, sizeof (where));
    printf ("@@ {\"ev\": \"crash\", \"code\": \"%08lx\", \"at\": \"%s\"}\n", info->ExceptionRecord->ExceptionCode, where);
    fflush (stdout);
    printStack ("crash", info->ExceptionRecord->ExceptionAddress, info->ExceptionRecord->ExceptionCode);
    return EXCEPTION_EXECUTE_HANDLER;
}
#endif

namespace tt
{
class EngineApp : private juce::AudioIODeviceCallback, private juce::Timer, private juce::MidiInputCallback
{
public:
    EngineApp() : session (transport) {}

    ~EngineApp() override
    {
        stopTimer();
        deviceManager.removeAudioCallback (this);
        deviceManager.closeAudioDevice();
        session.closeEditors();
    }

    // ---- inputs: the interface's input channels and the MIDI keyboards ----
    // MIDI thread: stamped with the time it arrived, handed to the audio thread through `keys`.
    void handleIncomingMidiMessage (juce::MidiInput* from, const juce::MidiMessage& m) override
    {
        if (m.isActiveSense() || m.isMidiClock())
            return;
        for (size_t k = 0; k < keyboardIs.size(); ++k)
            if (keyboardIs[k].load (std::memory_order_acquire) == from)
                return keys[k + 1].addMessageToQueue (m);
    }

    // Opens every MIDI input not open yet (a keyboard plugged in since); returns the names of those open, in
    // order: the first is source 1 of LiveIn::midi (0 is what the `midi` op sends).
    juce::var openKeyboards()
    {
        for (auto& d : juce::MidiInput::getAvailableDevices())
        {
            bool have = false;
            for (auto& k : keyboards)
                have = have || k->getIdentifier() == d.identifier;
            if (! have && keyboards.size() < keyboardIs.size())
                if (auto in = juce::MidiInput::openDevice (d.identifier, this))
                {
                    keyboardIs[keyboards.size()].store (in.get(), std::memory_order_release);
                    in->start();
                    keyboards.push_back (std::move (in));
                }
        }
        juce::Array<juce::var> names;
        for (auto& k : keyboards)
            names.add (k->getName());
        return names;
    }

    juce::var inputNames()
    {
        juce::Array<juce::var> out;
        if (auto* d = deviceManager.getCurrentAudioDevice())
            for (auto& n : d->getInputChannelNames())
                out.add (n);
        return out;
    }

    // ---- output ----
    static void send (const juce::var& v)
    {
        std::cout << "@@ " << juce::JSON::toString (v, true) << std::endl;
    }

    static juce::DynamicObject::Ptr object (std::initializer_list<std::pair<const char*, juce::var>> items)
    {
        juce::DynamicObject::Ptr o = new juce::DynamicObject();
        for (auto& [k, v] : items)
            o->setProperty (k, v);
        return o;
    }

    void reply (const juce::var& cmd, juce::DynamicObject::Ptr o)
    {
        o->setProperty ("re", cmd["id"]);
        send (juce::var (o.get()));
    }

    void fail (const juce::var& cmd, const juce::String& message)
    {
        reply (cmd, object ({ { "error", message } }));
    }

    // ---- audio device ----
    juce::var listDevices (bool inputs = false)
    {
        juce::Array<juce::var> out;
        for (auto* type : deviceManager.getAvailableDeviceTypes())
        {
            type->scanForDevices();
            for (auto& name : type->getDeviceNames (inputs))
                out.add (juce::var (object ({ { "type", type->getTypeName() }, { "name", name } }).get()));
        }
        return out;
    }

    // Opens the output named by `wanted` (any part of "type: name"); with no name, an ASIO interface if there
    // is one, else the system default. Returns the problem, or an empty string.
    juce::String openDevice (const juce::String& wanted, double sampleRate)
    {
        deviceManager.removeAudioCallback (this);
        struct Choice { juce::String type, name; int rank; };
        juce::Array<Choice> choices;
        for (auto* type : deviceManager.getAvailableDeviceTypes())
        {
            type->scanForDevices();
            const auto tn = type->getTypeName();
            for (auto& name : type->getDeviceNames (false))
            {
                int rank = 100;
                if (wanted.isNotEmpty())
                    rank = (tn + ": " + name).containsIgnoreCase (wanted) ? 0 : 1000;
                else if (tn == "ASIO")
                    rank = name.containsIgnoreCase ("SSL") ? 0 : (name.containsIgnoreCase ("ASIO4ALL") || name.containsIgnoreCase ("Generic") ? 60 : 10);
                else if (tn == "Windows Audio")
                    rank = 50;
                if (rank < 1000)
                    choices.add ({ tn, name, rank });
            }
        }
        std::stable_sort (choices.begin(), choices.end(), [] (const Choice& a, const Choice& b) { return a.rank < b.rank; });
        juce::String problems;
        for (auto& c : choices)
        {
            deviceManager.setCurrentAudioDeviceType (c.type, true);
            juce::AudioDeviceManager::AudioDeviceSetup setup;
            setup.outputDeviceName = c.name;
            setup.inputDeviceName = {};
            setup.sampleRate = sampleRate;
            setup.bufferSize = 0;
            setup.useDefaultInputChannels = false;
            setup.inputChannels.clear();
            setup.useDefaultOutputChannels = true;
            auto err = juce::String ("no inputs asked for");
            inputProblem = {};
            if (wantInputs)
            {
                // an ASIO driver is one device both ways; elsewhere the system's default input
                auto withIn = setup;
                if (auto* type = deviceManager.getCurrentDeviceTypeObject())
                {
                    const auto ins = type->getDeviceNames (true);
                    // the input device asked for, else the same device (an ASIO driver is one both ways), else
                    // the system's
                    withIn.inputDeviceName = wantedInput.isNotEmpty() && ins.contains (wantedInput) ? wantedInput
                                             : ins.contains (c.name) ? c.name : ins[juce::jmax (0, type->getDefaultDeviceIndex (true))];
                }
                withIn.inputChannels.setRange (0, 64, true);
                err = withIn.inputDeviceName.isEmpty() ? juce::String ("no input device") : deviceManager.setAudioDeviceSetup (withIn, true);
                if (err.isNotEmpty())
                    inputProblem = "inputs could not be opened (" + err + ")";
            }
            if (err.isNotEmpty())
                err = deviceManager.setAudioDeviceSetup (setup, true);
            if (err.isEmpty() && deviceManager.getCurrentAudioDevice() != nullptr)
            {
                deviceManager.addAudioCallback (this);
                return {};
            }
            problems << c.type << ": " << c.name << " - " << err << "; ";
            if (wanted.isNotEmpty())
                break;
        }
        return problems.isEmpty() ? juce::String ("no audio output found") : problems;
    }

    juce::DynamicObject::Ptr deviceInfo()
    {
        if (auto* d = deviceManager.getCurrentAudioDevice())
            return object ({ { "type", d->getTypeName() }, { "name", d->getName() }, { "sr", d->getCurrentSampleRate() },
                             { "block", d->getCurrentBufferSizeSamples() },
                             { "out_latency", d->getOutputLatencyInSamples() },
                             { "in_latency", d->getInputLatencyInSamples() },
                             { "ins", d->getActiveInputChannels().countNumberOfSetBits() },
                             { "input_problem", inputProblem },
                             { "rate", transport.sampleRate },           // the session's; converted when it differs
                             { "converted", ratio != 1.0 } });
        return object ({});
    }

    // ---- processing (audio thread, or the message thread while rendering offline) ----
    void renderBlock (int n)
    {
        int done = 0;
        while (done < n)
        {
            if (const auto s = transport.seekRequest.exchange (Transport::noSeek); s != Transport::noSeek)
                pos = s;
            const bool playing = transport.playing.load();
            const bool loop = transport.looping.load();
            const int64 ls = transport.loopStart.load(), le = transport.loopEnd.load();
            int chunk = n - done;
            if (playing && loop && le > ls && pos < le)
                chunk = (int) juce::jmin<int64> (chunk, le - pos);
            transport.blockPos = pos;
            transport.blockLen = chunk;
            transport.blockPlaying = playing;
            transport.tempoNow = transport.tempo.get();
            transport.in.offset = done;
            if (playing && recorder != nullptr)
            {
                if (! recorder->deviceTime)
                    recorder->capture (transport.in, done, chunk, pos);
                else  // heard once what is waiting in the converter has gone out
                    recorder->mark (pos, chunk, recBase + (int64) std::llround ((ahead + done) / ratio));
            }
            float* chans[2] = { work.getWritePointer (0, done), work.getWritePointer (1, done) };
            juce::AudioBuffer<float> view (chans, 2, chunk);
            view.clear();
            midi.clear();
            session.runLanes (chunk);
            session.graph.processBlock (view, midi);
            if (playing)
            {
                pos += chunk;
                const int64 end = transport.endSample.load();
                if (loop && le > ls && pos == le)
                    pos = ls;
                else if (stopAtEnd && recorder == nullptr && end > 0 && pos >= end)
                    transport.playing = false;
                else if (recorder != nullptr && pos >= stopAt.load (std::memory_order_relaxed))
                    transport.playing = false;  // past the punch out: the Python side sees it and stops the recording
            }
            done += chunk;
        }
        transport.pos.store (pos);
    }

    // ---- sample-rate conversion ----
    // The session always runs at the project's rate, so what is heard is what offline renders measure. A device
    // that cannot run at that rate (Windows Audio in shared mode runs at the system mixer's) is fed through a
    // converter (Resampler.h): the session is computed in its own blocks into a FIFO, and each device block takes
    // what it needs from there.
    void setUpConversion (double devRate, int devBlock)
    {
        deviceRate = devRate;
        ratio = devRate > 0.0 ? transport.sampleRate / devRate : 1.0;  // project samples per device sample
        if (std::abs (ratio - 1.0) < 1.0e-9)
            ratio = 1.0;
        fifoCount = 0;
        if (ratio == 1.0)
            return;
        resampler.setUp (ratio);
        const int history = resampler.halfWidth();
        fifo.setSize (2, 2 * history + (int) std::ceil (devBlock * ratio) + work.getNumSamples() + 16);
        fifo.clear();
        fifoCount = history;  // silence before the start, for the kernel to look back on
        converted.setSize (2, devBlock);
    }

    // Device thread (or the `pull` test): the next n samples at the device's rate, into `converted`.
    void pullConverted (int n)
    {
        while (fifoCount < resampler.needed (n))
        {
            const int k = juce::jmin (work.getNumSamples(), fifo.getNumSamples() - fifoCount);
            if (k <= 0)
            {
                converted.clear();
                return;
            }
            ahead = fifoCount - resampler.halfWidth();
            renderBlock (k);
            transport.in.clearMidi();  // the keyboard was heard at the start of that block
            for (int c = 0; c < 2; ++c)
                fifo.copyFrom (c, fifoCount, work, c, 0, k);
            fifoCount += k;
        }
        for (int c = 0; c < 2; ++c)
            resampler.process (fifo.getReadPointer (c), converted.getWritePointer (c), n);
        const int drop = juce::jlimit (0, fifoCount, resampler.advance (n));
        for (int c = 0; c < 2; ++c)
        {
            float* d = fifo.getWritePointer (c);
            std::memmove (d, d + drop, sizeof (float) * (size_t) (fifoCount - drop));
        }
        fifoCount -= drop;
    }

    // One device block: what came in (`in`, and the keyboards' events in `devMidi`) is there for the graph to read
    // and the recorder to write while it runs.
    // With sample-rate conversion the session's blocks are not the device's. The input is then not heard through
    // the engine, and is recorded as it arrives, at the device's rate; the keyboard plays at the start of the
    // next session block (a few ms of jitter) and is recorded at the time it was played.
    void liveBlock (const float* const* in, int numIn, int n)
    {
        const juce::SpinLock::ScopedTryLockType held (recLock);
        recorder = held.isLocked() ? recording.get() : nullptr;
        for (int c = 0; c < juce::jmin (numIn, (int) inPeak.size()); ++c)
            if (in[c] != nullptr)
            {
                const auto range = juce::FloatVectorOperations::findMinAndMax (in[c], n);
                const float m = juce::jmax (-range.getStart(), range.getEnd());
                if (m > inPeak[(size_t) c].load (std::memory_order_relaxed))
                    inPeak[(size_t) c].store (m, std::memory_order_relaxed);
            }
        if (ratio == 1.0)
        {
            transport.in.audio = numIn > 0 ? in : nullptr;
            transport.in.channels = numIn;
            for (size_t s = 0; s < devMidi.size(); ++s)
                transport.in.midi[s].swapWith (devMidi[s]);
            renderBlock (n);
            transport.in.clearMidi();
        }
        else
        {
            transport.in.audio = nullptr;
            for (size_t s = 0; s < devMidi.size(); ++s)
                for (const auto meta : devMidi[s])
                    transport.in.midi[s].addEvent (meta.data, meta.numBytes, 0);
            const bool was = transport.playing.load();
            recBase = recorder != nullptr ? recorder->written : 0;
            pullConverted (n);
            if (recorder != nullptr && recorder->deviceTime && (was || transport.playing.load()))
                recorder->write (numIn > 0 ? in : nullptr, numIn, devMidi, 0, n);
        }
        for (auto& m : devMidi)
            m.clear();
        transport.in.audio = nullptr;
        recorder = nullptr;
    }

    void audioDeviceIOCallbackWithContext (const float* const* in, int numIn, float* const* out, int numOut, int n,
                                           const juce::AudioIODeviceCallbackContext&) override
    {
        const juce::ScopedNoDenormals noDenormals;
        const auto& from = ratio == 1.0 ? work : converted;
        if (n > from.getNumSamples())
        {
            for (int c = 0; c < numOut; ++c)
                if (out[c] != nullptr)
                    juce::FloatVectorOperations::clear (out[c], n);
            return;
        }
        for (size_t s = 0; s < keys.size(); ++s)
            keys[s].removeNextBlockOfMessages (devMidi[s], n);
        liveBlock (in, numIn, n);
        for (int c = 0; c < numOut; ++c)
        {
            if (out[c] == nullptr)
                continue;
            if (c < 2)
                juce::FloatVectorOperations::clip (out[c], from.getReadPointer (c), -1.0f, 1.0f, n);  // safety only
            else
                juce::FloatVectorOperations::clear (out[c], n);
        }
    }

    void audioDeviceAboutToStart (juce::AudioIODevice* d) override
    {
        const double devRate = d->getCurrentSampleRate();
        const int devBlock = d->getCurrentBufferSizeSamples();
        const double rate = transport.sampleRate;  // the session runs at the project's rate, whatever the device's
        const int block = juce::jmax (512, (int) std::ceil (devBlock * rate / juce::jmax (1.0, devRate)) + 1);
        work.setSize (2, block);
        session.prepare (rate, block, false);
        setUpConversion (devRate, devBlock);
        for (auto& k : keys)
            k.reset (devRate);
    }

    void audioDeviceStopped() override {}

    // Message thread, on load: the session moves to the project's rate. The device is asked for it first; one that
    // cannot run at it keeps its own rate and is converted to.
    void matchRate (double rate)
    {
        transport.sampleRate = rate;
        auto* d = deviceManager.getCurrentAudioDevice();
        if (d == nullptr)
        {
            session.prepare (rate, session.blockSize, session.offline);
            return;
        }
        if (d->getCurrentSampleRate() != rate && d->getAvailableSampleRates().contains (rate))
        {
            const auto before = deviceManager.getAudioDeviceSetup();
            auto setup = before;
            setup.sampleRate = rate;
            // restarts the device; audioDeviceAboutToStart prepares the session at the new rate
            if (deviceManager.setAudioDeviceSetup (setup, true).isEmpty() && deviceManager.getCurrentAudioDevice() != nullptr
                && deviceManager.getCurrentAudioDevice()->getCurrentSampleRate() == rate)
                return;
            deviceManager.setAudioDeviceSetup (before, true);
        }
        deviceManager.removeAudioCallback (this);  // the restart prepares the session at the project's rate
        deviceManager.addAudioCallback (this);
    }

    // ---- offline render ----
    // The graph runs on this (message) thread while offline: the device stops calling it, and the session is
    // prepared for 512-sample blocks without real-time constraints. endOffline puts things back.
    struct Offline { bool live, reprepared; double sr; int block; bool offline; };
    static constexpr int offlineBlock = 512;

    Offline beginOffline()
    {
        Offline o { deviceManager.getCurrentAudioDevice() != nullptr, false, session.sampleRate, session.blockSize, session.offline };
        if (o.live)
            deviceManager.removeAudioCallback (this);
        transport.in.audio = nullptr;
        transport.in.clearMidi();
        transport.click = false;  // never in a file
        transport.countUntil = Transport::noSeek;
        if (o.sr != transport.sampleRate || o.block != offlineBlock || ! o.offline)
        {
            session.prepare (transport.sampleRate, offlineBlock, true);
            o.reprepared = true;
        }
        if (work.getNumSamples() < offlineBlock)
            work.setSize (2, offlineBlock);
        return o;
    }

    void endOffline (const Offline& o)
    {
        if (o.reprepared)
            session.prepare (o.sr, o.block, o.offline);
        if (o.live)
            deviceManager.addAudioCallback (this);
    }

    // One round of the ready probe (Session::probeBegin). Returns how many instruments are not ready yet.
    int probeOnce()
    {
        const auto o = beginOffline();
        const juce::ScopedNoDenormals noDenormals;
        const int64 length = (int64) (0.3 * transport.sampleRate);
        transport.playing = false;
        renderBlock (offlineBlock);    // sources release whatever was sounding
        session.graph.reset();         // voices and tails cleared, so the probe hears only its own note
        if (session.probeBegin (length) > 0)
            for (int64 done = 0; done <= length; done += offlineBlock)
                renderBlock (offlineBlock);
        const int left = session.probeEnd();
        endOffline (o);
        return left;
    }

    static int envMs (const char* name, const char* fallback)
    {
        return juce::SystemStats::getEnvironmentVariable (name, fallback).getIntValue();
    }

    juce::DynamicObject::Ptr renderOffline (const juce::var& cmd)
    {
        const juce::File outFile (cmd["out"].toString());
        const bool master = cmd["out"].toString().isNotEmpty();  // without `out`, only the taps are written
        const double sr = transport.sampleRate;
        const int64 length = cmd["length"].isVoid() ? transport.endSample.load() : (int64) (double) cmd["length"];
        if (length <= 0)
            return object ({ { "error", "nothing to render (length 0)" } });

        const auto t0 = juce::Time::getMillisecondCounterHiRes();
        const auto offline = beginOffline();
        const int block = offlineBlock;

        const juce::ScopedNoDenormals noDenormals;
        const bool wasLooping = transport.looping.exchange (false);
        trace ("render: pre-roll");
        stopAtEnd = false;
        transport.playing = false;
        transport.seekRequest = 0;
        renderBlock (block);        // sources release whatever was sounding
        session.graph.reset();      // tails, voices and delay lines cleared: the same render every time
        for (int i = 0; i < (int) (0.25 * sr) / block + 1; ++i)  // let instruments apply their loaded patch
            renderBlock (block);
        session.resetBuiltIns();  // their LFOs and clocks ran through the pre-roll; offline they start at zero
        transport.seekRequest = 0;
        transport.playing = true;
        session.peaks();  // start every strip's meter from zero
        const auto tapProblems = session.armTaps (cmd["taps"], length);

        const int latency = session.latency();
        const int64 total = length + latency;
        trace ("render: " + juce::String (total) + " samples, latency " + juce::String (latency));
        juce::AudioBuffer<float> result (2, master ? (int) length : 1);
        result.clear();
        for (int64 done = 0; done < total; done += block)
        {
            renderBlock (block);
            const int64 from = juce::jmax<int64> (done, latency), to = juce::jmin<int64> (done + block, total);
            if (master && to > from)
                for (int c = 0; c < 2; ++c)
                    result.copyFrom (c, (int) (from - latency), work, c, (int) (from - done), (int) (to - from));
        }
        const auto stripPeaks = session.peaks();  // post-fader peak of every strip over the whole render
        const auto tapPeaks = session.closeTaps();
        transport.playing = false;
        transport.seekRequest = 0;
        renderBlock (block);  // lets every source release its notes
        transport.looping = wasLooping;
        stopAtEnd = true;

        juce::String error = tapProblems;
        if (master)
        {
            outFile.getParentDirectory().createDirectory();
            outFile.deleteFile();
            std::unique_ptr<juce::OutputStream> stream (outFile.createOutputStream().release());
            juce::WavAudioFormat wav;
            const auto options = juce::AudioFormatWriterOptions()
                                     .withSampleRate (sr)
                                     .withNumChannels (2)
                                     .withBitsPerSample (32)
                                     .withSampleFormat (juce::AudioFormatWriterOptions::SampleFormat::floatingPoint);
            if (stream == nullptr)
                error = "cannot write " + outFile.getFullPathName();
            else if (auto writer = wav.createWriterFor (stream, options))
                writer->writeFromAudioSampleBuffer (result, 0, result.getNumSamples());
            else
                error = "cannot create a WAV writer";
        }

        endOffline (offline);
        if (error.isNotEmpty())
            return object ({ { "error", error } });
        return object ({ { "out", master ? outFile.getFullPathName() : juce::String() }, { "samples", length },
                         { "latency", latency }, { "peaks", stripPeaks }, { "taps", tapPeaks },
                         { "peak", master ? result.getMagnitude (0, result.getNumSamples()) : 0.0f },
                         { "seconds", (juce::Time::getMillisecondCounterHiRes() - t0) / 1000.0 } });
    }

    // ---- commands (message thread) ----
    void handleLine (const juce::String& line)
    {
        juce::var cmd;
        if (juce::JSON::parse (line, cmd).failed() || ! cmd.isObject())
        {
            send (juce::var (object ({ { "error", "not a JSON command: " + line.substring (0, 80) } }).get()));
            return;
        }
        handle (cmd);
    }

    void handle (const juce::var& cmd)
    {
        const auto op = cmd["op"].toString();
        if (op == "load")
        {
            juce::var spec = cmd["spec"];
            if (! spec.isObject())
            {
                const juce::File f (cmd["path"].toString());
                if (juce::JSON::parse (f.loadFileAsString(), spec).failed() || ! spec.isObject())
                    return fail (cmd, "cannot read spec " + f.getFullPathName());
            }
            session.perSampleSynths = perSample || (bool) spec["per_sample"];
            if (const double rate = spec["sr"].isVoid() ? 0.0 : (double) spec["sr"]; rate > 0.0 && rate != session.sampleRate)
                matchRate (rate);  // before any device is made, so plugins start at the project's rate
            const auto res = session.apply (spec);
            beats.clear();
            if (auto* a = spec["tempo"].getArray())
                for (auto& t : *a)
                    if (auto* p = t.getArray(); p != nullptr && p->size() >= 3)
                        beats.push_back ({ (int64) (double) (*p)[0], (double) (*p)[1], (double) (*p)[2] });
            if (! loaded)
                transport.looping = transport.loopEnd.load() > transport.loopStart.load();
            loaded = true;
            if (session.settleRemainingMs() > 0)
                juce::Timer::callAfterDelay (session.settleRemainingMs() + 20, [this] { settle(); });
            juce::DynamicObject::Ptr o = res.getDynamicObject();
            if (deviceManager.getCurrentAudioDevice() != nullptr)
                o->setProperty ("device", juce::var (deviceInfo().get()));
            return reply (cmd, o);
        }
        if (op == "play")
        {
            if (! cmd["from"].isVoid())
                transport.seekRequest = (int64) (double) cmd["from"];
            if (! cmd["loop"].isVoid())
                transport.looping = (bool) cmd["loop"];
            transport.playing = true;
            return reply (cmd, object ({ { "playing", true } }));
        }
        if (op == "record")
            return startRecording (cmd);
        if (op == "record_stop")
        {
            if (recording == nullptr)
                return fail (cmd, "not recording");
            juce::DynamicObject::Ptr o = stopRecording().getDynamicObject();
            o->setProperty ("comp", comp());
            o->setProperty ("sr", transport.sampleRate);
            return reply (cmd, o);
        }
        if (op == "click")
        {
            transport.click = (bool) cmd["on"];
            return reply (cmd, object ({ { "click", transport.click.load() } }));
        }
        if (op == "inputs")
        {
            // what can be recorded: the open device's input channels (open them with device + "inputs") and the
            // MIDI keyboards, which are opened here
            auto o = deviceInfo();
            o->setProperty ("audio", inputNames());
            o->setProperty ("midi", openKeyboards());
            return reply (cmd, o);
        }
        if (op == "midi")
        {
            // play the armed instrument as a keyboard would: [[status, data1, data2], ...]
            if (auto* a = cmd["msgs"].getArray())
                for (auto& m : *a)
                    if (auto* b = m.getArray(); b != nullptr && b->size() >= 3)
                    {
                        juce::MidiMessage msg ((int) (*b)[0], (int) (*b)[1], (int) (*b)[2]);
                        msg.setTimeStamp (juce::Time::getMillisecondCounterHiRes() * 0.001);
                        keys[0].addMessageToQueue (msg);
                    }
            return reply (cmd, object ({}));
        }
        if (op == "stop")
        {
            transport.playing = false;
            return reply (cmd, object ({ { "playing", false } }));
        }
        if (op == "seek")
        {
            transport.seekRequest = (int64) (double) cmd["to"];
            return reply (cmd, object ({}));
        }
        if (op == "loop")
        {
            if (! cmd["start"].isVoid())
            {
                transport.loopStart = (int64) (double) cmd["start"];
                transport.loopEnd = (int64) (double) cmd["end"];
            }
            transport.looping = (bool) cmd["on"];
            return reply (cmd, object ({}));
        }
        if (op == "status")
        {
            auto o = status();
            o->setProperty ("device", juce::var (deviceInfo().get()));
            o->setProperty ("latency", session.latency());
            o->setProperty ("settle_ms", session.settleRemainingMs());
            o->setProperty ("threads", session.threads());
            return reply (cmd, o);
        }
        if (op == "pull")
        {
            // A check of the device path without a device: play from the start as a device at `device_sr` would
            // hear it, `length` device samples in blocks of `block`, written to `out`.
            if (deviceManager.getCurrentAudioDevice() != nullptr)
                return fail (cmd, "pull runs without an audio device only");
            const double devRate = (double) cmd["device_sr"];
            const int devBlock = cmd["block"].isVoid() ? 480 : (int) cmd["block"];
            const int64 total = (int64) (double) cmd["length"];
            if (devRate <= 0.0 || devBlock <= 0 || total <= 0)
                return fail (cmd, "pull needs device_sr, block and length");
            const juce::ScopedNoDenormals noDenormals;
            setUpConversion (devRate, devBlock);
            juce::AudioBuffer<float> result (2, (int) total);
            // "input": a file played as the device's input channels; "midi": [[device sample, status, d1, d2
            // [, which keyboard]], ...] as keyboards' events; "hold": leave the transport as it is (a recording
            // started before the pull)
            juce::AudioBuffer<float> input;
            if (const auto path = cmd["input"].toString(); path.isNotEmpty())
            {
                juce::AudioFormatManager fm;
                fm.registerBasicFormats();
                std::unique_ptr<juce::AudioFormatReader> r { fm.createReaderFor (juce::File (path)) };
                if (r == nullptr)
                    return fail (cmd, "cannot read " + path);
                input.setSize ((int) r->numChannels, (int) juce::jmax<int64> (total, r->lengthInSamples));
                input.clear();
                r->read (&input, 0, (int) r->lengthInSamples, 0, true, true);
            }
            const bool hold = (bool) cmd["hold"];
            if (! hold)
            {
                transport.seekRequest = 0;
                transport.playing = true;
            }
            stopAtEnd = false;
            std::vector<const float*> ins ((size_t) input.getNumChannels());
            for (int64 done = 0; done < total;)
            {
                const int most = ratio == 1.0 ? juce::jmin (devBlock, work.getNumSamples()) : devBlock;
                const int k = (int) juce::jmin<int64> (most, total - done);
                for (size_t c = 0; c < ins.size(); ++c)
                    ins[c] = input.getReadPointer ((int) c, (int) done);
                if (auto* a = cmd["midi"].getArray())
                    for (auto& m : *a)
                        if (auto* b = m.getArray(); b != nullptr && b->size() >= 4)
                            if (const auto at = (int64) (double) (*b)[0]; at >= done && at < done + k)
                                devMidi[(size_t) juce::jlimit (0, LiveIn::sources - 1, b->size() > 4 ? (int) (*b)[4] : 0)]
                                    .addEvent (juce::MidiMessage ((int) (*b)[1], (int) (*b)[2], (int) (*b)[3]), (int) (at - done));
                liveBlock (ins.data(), (int) ins.size(), k);
                for (int c = 0; c < 2; ++c)
                    result.copyFrom (c, (int) done, ratio == 1.0 ? work : converted, c, 0, k);
                done += k;
            }
            if (! hold)
                transport.playing = false;
            stopAtEnd = true;
            setUpConversion (transport.sampleRate, devBlock);
            const juce::File outFile (cmd["out"].toString());
            outFile.deleteFile();
            std::unique_ptr<juce::OutputStream> stream (outFile.createOutputStream().release());
            juce::WavAudioFormat wav;
            const auto options = juce::AudioFormatWriterOptions().withSampleRate (devRate).withNumChannels (2)
                                     .withBitsPerSample (32).withSampleFormat (juce::AudioFormatWriterOptions::SampleFormat::floatingPoint);
            auto writer = stream != nullptr ? wav.createWriterFor (stream, options) : nullptr;
            if (writer == nullptr)
                return fail (cmd, "cannot write " + outFile.getFullPathName());
            writer->writeFromAudioSampleBuffer (result, 0, result.getNumSamples());
            return reply (cmd, object ({ { "out", outFile.getFullPathName() } }));
        }
        if (op == "ticks")
        {
            const int hz = (int) cmd["hz"];
            if (hz > 0) startTimerHz (juce::jmin (60, hz)); else stopTimer();
            return reply (cmd, object ({}));
        }
        if (op == "render")
        {
            if (const int wait = session.settleRemainingMs(); wait > 0)
            {
                juce::Timer::callAfterDelay (wait + 60, [this, cmd] { handle (cmd); });
                return;
            }
            settle();
            // New plugin instruments are played a test note until they sound settled, with the message thread
            // free between rounds so they can finish loading (TT_PROBE_MS caps the wait, TT_PROBE_GAP_MS spaces
            // the rounds). "probe": false skips it.
            const auto now = juce::Time::getMillisecondCounterHiRes();
            if (probeSince <= 0.0)
                probeSince = now;
            if ((cmd["probe"].isVoid() || (bool) cmd["probe"]) && session.hasUnproven()
                && now - probeSince < envMs ("TT_PROBE_MS", "15000") && probeOnce() > 0)
            {
                juce::Timer::callAfterDelay (envMs ("TT_PROBE_GAP_MS", "250"), [this, cmd] { handle (cmd); });
                return;
            }
            const auto unready = session.giveUpProbes();
            const double probed = now - probeSince;
            probeSince = 0.0;
            auto res = renderOffline (cmd);
            res->setProperty ("probe_ms", (int) probed);
            if (! unready.isEmpty())
                res->setProperty ("unready", juce::var (unready));
            reply (cmd, res);
            if (onRendered)
                onRendered();
            return;
        }
        if (op == "editor")
        {
            const auto err = session.openEditor (cmd["ref"].toString());
            return err.isEmpty() ? reply (cmd, object ({})) : fail (cmd, err);
        }
        if (op == "close_editors")
        {
            session.closeEditors();
            return reply (cmd, object ({}));
        }
        if (op == "save_state")
        {
            const auto err = session.saveState (cmd["ref"].toString(), juce::File (cmd["file"].toString()));
            return err.isEmpty() ? reply (cmd, object ({})) : fail (cmd, err);
        }
        if (op == "devices")
            return reply (cmd, object ({ { "devices", listDevices() }, { "inputs", listDevices (true) },
                                         { "current", juce::var (deviceInfo().get()) } }));
        if (op == "device")
        {
            if ((bool) cmd["close"])
            {
                deviceManager.removeAudioCallback (this);
                deviceManager.closeAudioDevice();
                return reply (cmd, object ({}));
            }
            if (! cmd["inputs"].isVoid())
                wantInputs = (bool) cmd["inputs"];
            // "input": the device to record from ("" = the output device's own inputs, or the system's);
            // "pick": choose the output as at start-up, whatever is open now
            if (! cmd["input"].isVoid())
                wantedInput = cmd["input"].toString();
            auto name = cmd["name"].toString();
            if (name.isEmpty() && ! (bool) cmd["pick"])
                if (auto* d = deviceManager.getCurrentAudioDevice())
                    name = d->getTypeName() + ": " + d->getName();  // the same device, with or without its inputs
            const auto err = openDevice (name, cmd["sr"].isVoid() ? transport.sampleRate : (double) cmd["sr"]);
            auto o = deviceInfo();
            if (err.isNotEmpty())
                o->setProperty ("error", err);
            return reply (cmd, o);
        }
        if (op == "quit")
        {
            reply (cmd, object ({}));
            quit();
        }
        fail (cmd, "unknown op '" + op + "'");
    }

    void settle()
    {
        if (session.settleRemainingMs() > 0)
            return;
        const auto w = session.finishSettle();
        if (! w.isEmpty())
            send (juce::var (object ({ { "ev", "warnings" }, { "warnings", juce::var (w) } }).get()));
    }

    juce::DynamicObject::Ptr status()
    {
        const int64 p = transport.pos.load();
        double beat = p / transport.sampleRate * 2.0;
        const TempoPt* at = nullptr;
        for (auto& t : beats)
        {
            if (t.sample > p)
                break;
            at = &t;
        }
        if (at == nullptr && ! beats.empty())
            at = &beats.front();  // a count-in: before the start, at the first tempo
        if (at != nullptr)
            beat = at->beat + (double) (p - at->sample) / transport.sampleRate * at->bpm / 60.0;
        return object ({ { "pos", p }, { "beat", beat }, { "playing", transport.playing.load() },
                         { "looping", transport.looping.load() }, { "recording", transport.recording.load() },
                         { "click", transport.click.load() } });
    }

    void timerCallback() override
    {
        auto o = status();
        o->setProperty ("ev", "tick");
        o->setProperty ("peaks", session.peaks());
        juce::Array<juce::var> ins;
        if (auto* d = deviceManager.getCurrentAudioDevice())
            for (int c = 0; c < juce::jmin ((int) inPeak.size(), d->getActiveInputChannels().countNumberOfSetBits()); ++c)
                ins.add (inPeak[(size_t) c].exchange (0.0f));
        o->setProperty ("ins", juce::var (ins));
        o->setProperty ("cpu", deviceManager.getCpuUsage());
        o->setProperty ("xruns", deviceManager.getXRunCount());
        send (juce::var (o.get()));
    }

    // ---- recording ----
    juce::var comp()
    {
        auto* d = deviceManager.getCurrentAudioDevice();
        return juce::var (object ({ { "graph", session.latency() }, { "out", d != nullptr ? d->getOutputLatencyInSamples() : 0 },
                                    { "in", d != nullptr ? d->getInputLatencyInSamples() : 0 } }).get());
    }

    // {"audio": {track id: {"file": path, "ch": [left, right?]}}, "from": sample (may be before 0: a count-in),
    //  "punch": [in, out?] (armed audio tracks are silent between them), "stop_at": sample the transport stops at}
    // Without a device, "device_sr" records as a device at that rate would (the `pull` op then plays it).
    void startRecording (const juce::var& cmd)
    {
        if (recording != nullptr)
            return fail (cmd, "already recording");
        auto rec = std::make_unique<Recorder>();
        rec->patient = deviceManager.getCurrentAudioDevice() == nullptr;
        if (rec->patient && ! cmd["device_sr"].isVoid())
            setUpConversion ((double) cmd["device_sr"], 480);
        rec->deviceTime = ratio != 1.0;
        rec->fileRate = rec->deviceTime ? deviceRate : transport.sampleRate;
        if (auto* a = cmd["audio"].getDynamicObject())
        {
            for (auto& kv : a->getProperties())
            {
                auto* ch = kv.value["ch"].getArray();
                const auto err = rec->add (kv.name.toString(), juce::File (kv.value["file"].toString()),
                                           ch != nullptr && ch->size() > 0 ? (int) (*ch)[0] : 0,
                                           ch != nullptr && ch->size() > 1 ? (int) (*ch)[1] : -1, rec->fileRate);
                if (err.isNotEmpty())
                    return fail (cmd, err);
            }
        }
        if (! cmd["from"].isVoid())
            transport.seekRequest = (int64) (double) cmd["from"];
        if (! cmd["loop"].isVoid())
            transport.looping = (bool) cmd["loop"];
        transport.countUntil = cmd["count_until"].isVoid() ? Transport::noSeek : (int64) (double) cmd["count_until"];
        const auto never = std::numeric_limits<int64>::max();
        auto* punch = cmd["punch"].getArray();
        transport.punchOut = punch != nullptr && punch->size() > 1 ? (int64) (double) (*punch)[1] : never;
        transport.punchIn = punch != nullptr && punch->size() > 0 ? (int64) (double) (*punch)[0] : never;
        stopAt = cmd["stop_at"].isVoid() ? never : (int64) (double) cmd["stop_at"];
        const double fileRate = rec->fileRate;
        {
            const juce::SpinLock::ScopedLockType l (recLock);
            recording = std::move (rec);
        }
        transport.recording = true;
        transport.playing = true;
        reply (cmd, object ({ { "recording", true }, { "comp", comp() }, { "sr", transport.sampleRate }, { "file_sr", fileRate } }));
    }

    juce::var stopRecording()
    {
        transport.playing = false;
        transport.recording = false;
        transport.countUntil = Transport::noSeek;
        transport.punchIn = transport.punchOut = stopAt = std::numeric_limits<int64>::max();
        std::unique_ptr<Recorder> rec;
        {
            const juce::SpinLock::ScopedLockType l (recLock);
            rec = std::move (recording);
        }
        return rec != nullptr ? rec->finish() : juce::var();
    }

    [[noreturn]] void quit()
    {
        stopRecording();
        stopTimer();
        deviceManager.removeAudioCallback (this);
        deviceManager.closeAudioDevice();
        session.closeEditors();
        std::cout.flush();
        std::_Exit (0);  // some plugins never finish shutting down; the work is saved by now
    }

    Transport transport;
    Session session;
    juce::AudioDeviceManager deviceManager;
    juce::AudioBuffer<float> work { 2, 512 };
    double deviceRate = 0.0, ratio = 1.0;  // ratio: project samples per device sample (1 = no conversion)
    Resampler resampler;
    juce::AudioBuffer<float> fifo, converted;
    int fifoCount = 0;
    juce::MidiBuffer midi;
    // From the MIDI thread to the audio thread, one per source of LiveIn::midi: [0] what the `midi` op sends,
    // then the keyboards. `devMidi` is the device block's share of each.
    std::array<juce::MidiMessageCollector, LiveIn::sources> keys;
    std::array<juce::MidiBuffer, LiveIn::sources> devMidi;
    std::vector<std::unique_ptr<juce::MidiInput>> keyboards;  // message thread
    std::array<std::atomic<juce::MidiInput*>, LiveIn::sources - 1> keyboardIs {};  // the same, for the MIDI thread
    std::atomic<int64> stopAt { std::numeric_limits<int64>::max() };  // while recording: the transport stops here
    int64 recBase = 0;  // device-rate recording: where in the recording the device block being computed starts
    int ahead = 0;      // ... and how many session samples wait in the converter before the block being rendered
    std::array<std::atomic<float>, 32> inPeak {};          // per input channel since the last tick
    std::unique_ptr<Recorder> recording;                   // message thread; the audio thread reads it under recLock
    Recorder* recorder = nullptr;                          // audio thread: `recording` for the block being computed
    juce::SpinLock recLock;
    juce::String inputProblem, wantedInput;
    bool wantInputs = false;
    std::vector<TempoPt> beats;
    int64 pos = 0;
    double probeSince = 0.0;  // when the ready probe of the render being handled started
    std::function<void()> onRendered;  // --render: quit once the render has been written
    bool stopAtEnd = true, loaded = false, perSample = false;
};
}  // namespace tt

int main (int argc, char* argv[])
{
   #if JUCE_WINDOWS
    SetUnhandledExceptionFilter (crashFilter);
    if (juce::SystemStats::getEnvironmentVariable ("TT_TRACE", {}).isNotEmpty())
        AddVectoredExceptionHandler (1, firstChance);
   #endif
    juce::ScopedJuceInitialiser_GUI gui;
    juce::StringArray args;
    for (int i = 1; i < argc; ++i)
        args.add (juce::String::fromUTF8 (argv[i]));

    if (const int d = args.indexOf ("--describe"); d >= 0 && d + 2 < args.size())
    {
        const bool ok = tt::Session::describe (args[d + 1], juce::File (args[d + 2]));
        std::_Exit (ok ? 0 : 1);  // no teardown: the plugin's throwaway instance must not take the result with it
    }

    auto* app = new tt::EngineApp();  // never destroyed: quit() ends the process
    app->perSample = args.contains ("--per-sample");
    {
        // threads that compute lanes, this one included; by default one per physical core but one, at most 8
        const int t = args.indexOf ("--threads");
        const int asked = t >= 0 && t + 1 < args.size() ? args[t + 1].getIntValue() : 0;
        app->session.setThreads (asked > 0 ? asked : juce::jlimit (1, 8, juce::SystemStats::getNumPhysicalCpus() - 1));
    }

    if (args.contains ("--devices"))
    {
        tt::EngineApp::send (app->listDevices());
        app->quit();
    }

    if (const int r = args.indexOf ("--render"); r >= 0 && r + 2 < args.size())
    {
        app->session.prepare (48000.0, 512, true);
        juce::var spec;
        const juce::File specFile (args[r + 1]);
        if (juce::JSON::parse (specFile.loadFileAsString(), spec).failed() || ! spec.isObject())
        {
            std::cerr << "cannot read spec " << args[r + 1] << std::endl;
            return 2;
        }
        const double sr = spec["sr"].isVoid() ? 48000.0 : (double) spec["sr"];
        app->session.prepare (sr, 512, true);
        auto* load = new juce::DynamicObject();
        load->setProperty ("op", "load");
        load->setProperty ("spec", spec);
        load->setProperty ("id", "load");
        auto* render = new juce::DynamicObject();
        render->setProperty ("op", "render");
        render->setProperty ("out", args[r + 2]);
        render->setProperty ("id", "render");
        const juce::var loadCmd (load), renderCmd (render);
        app->onRendered = [app] { app->quit(); };  // the render waits for plugins to settle and sound ready
        juce::MessageManager::callAsync ([app, loadCmd, renderCmd]
        {
            app->handle (loadCmd);
            app->handle (renderCmd);
        });
        juce::MessageManager::getInstance()->runDispatchLoop();
        return 0;
    }

    app->session.prepare (48000.0, 512, args.contains ("--no-device"));
    if (! args.contains ("--no-device"))
    {
        const int d = args.indexOf ("--device");
        app->wantInputs = args.contains ("--inputs");
        const auto err = app->openDevice (d >= 0 && d + 1 < args.size() ? args[d + 1] : juce::String(), 48000.0);
        auto ready = app->deviceInfo();
        ready->setProperty ("ev", "ready");
        ready->setProperty ("sfz", tt::sfzAvailable());
        if (err.isNotEmpty())
            ready->setProperty ("device_problem", err);
        tt::EngineApp::send (juce::var (ready.get()));
    }
    else
        tt::EngineApp::send (juce::var (tt::EngineApp::object ({ { "ev", "ready" }, { "sfz", tt::sfzAvailable() } }).get()));

    std::thread ([app]
    {
        std::string line;
        while (std::getline (std::cin, line))
        {
            const auto text = juce::String::fromUTF8 (line.c_str());
            if (text.trim().isNotEmpty())
                juce::MessageManager::callAsync ([app, text] { app->handleLine (text); });
        }
        juce::MessageManager::callAsync ([app] { app->quit(); });  // the parent went away
    }).detach();

    juce::MessageManager::getInstance()->runDispatchLoop();
    return 0;
}
