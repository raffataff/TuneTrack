// tunetrack-engine: TuneTrack's live audio engine.
//
// One process hosts the whole session (VST3 plugins, built-in synths and effects, stems) and either plays it
// through the audio interface or renders it faster than real time. The Python side drives it with one JSON
// command per line on stdin; replies and events come back on stdout as lines starting with "@@ ".
//
//   tunetrack-engine [--no-device] [--device <name part>] [--per-sample]
//   tunetrack-engine --render <spec.json> <out.wav> [--per-sample]
//   tunetrack-engine --devices
//   tunetrack-engine --describe <plugin.vst3> <out.xml>   (used by the engine itself)
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
class EngineApp : private juce::AudioIODeviceCallback, private juce::Timer
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
    juce::var listDevices()
    {
        juce::Array<juce::var> out;
        for (auto* type : deviceManager.getAvailableDeviceTypes())
        {
            type->scanForDevices();
            for (auto& name : type->getDeviceNames (false))
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
            const auto err = deviceManager.setAudioDeviceSetup (setup, true);
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
                             { "out_latency", d->getOutputLatencyInSamples() } });
        return object ({});
    }

    // ---- processing (audio thread, or the message thread while rendering offline) ----
    void renderBlock (int n)
    {
        int done = 0;
        while (done < n)
        {
            if (const auto s = transport.seekRequest.exchange (-1); s >= 0)
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
            float* chans[2] = { work.getWritePointer (0, done), work.getWritePointer (1, done) };
            juce::AudioBuffer<float> view (chans, 2, chunk);
            view.clear();
            midi.clear();
            session.graph.processBlock (view, midi);
            if (playing)
            {
                pos += chunk;
                const int64 end = transport.endSample.load();
                if (loop && le > ls && pos == le)
                    pos = ls;
                else if (stopAtEnd && end > 0 && pos >= end)
                    transport.playing = false;
            }
            done += chunk;
        }
        transport.pos.store (pos);
    }

    void audioDeviceIOCallbackWithContext (const float* const*, int, float* const* out, int numOut, int n,
                                           const juce::AudioIODeviceCallbackContext&) override
    {
        const juce::ScopedNoDenormals noDenormals;
        if (n > work.getNumSamples())
        {
            for (int c = 0; c < numOut; ++c)
                if (out[c] != nullptr)
                    juce::FloatVectorOperations::clear (out[c], n);
            return;
        }
        renderBlock (n);
        for (int c = 0; c < numOut; ++c)
        {
            if (out[c] == nullptr)
                continue;
            if (c < 2)
                juce::FloatVectorOperations::clip (out[c], work.getReadPointer (c), -1.0f, 1.0f, n);  // safety only
            else
                juce::FloatVectorOperations::clear (out[c], n);
        }
    }

    void audioDeviceAboutToStart (juce::AudioIODevice* d) override
    {
        const int block = juce::jmax (512, d->getCurrentBufferSizeSamples());
        work.setSize (2, block);
        session.prepare (d->getCurrentSampleRate(), block, false);
    }

    void audioDeviceStopped() override {}

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
            if (auto* d = deviceManager.getCurrentAudioDevice(); d != nullptr && d->getCurrentSampleRate() != transport.sampleRate)
                o->setProperty ("device_problem", "the audio device runs at " + juce::String (d->getCurrentSampleRate())
                                                      + " Hz but the project is " + juce::String (transport.sampleRate) + " Hz");
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
            return reply (cmd, o);
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
            return reply (cmd, object ({ { "devices", listDevices() }, { "current", juce::var (deviceInfo().get()) } }));
        if (op == "device")
        {
            if ((bool) cmd["close"])
            {
                deviceManager.removeAudioCallback (this);
                deviceManager.closeAudioDevice();
                return reply (cmd, object ({}));
            }
            const auto err = openDevice (cmd["name"].toString(), cmd["sr"].isVoid() ? transport.sampleRate : (double) cmd["sr"]);
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
        if (at != nullptr)
            beat = at->beat + (double) (p - at->sample) / transport.sampleRate * at->bpm / 60.0;
        return object ({ { "pos", p }, { "beat", beat }, { "playing", transport.playing.load() },
                         { "looping", transport.looping.load() } });
    }

    void timerCallback() override
    {
        auto o = status();
        o->setProperty ("ev", "tick");
        o->setProperty ("peaks", session.peaks());
        o->setProperty ("cpu", deviceManager.getCpuUsage());
        o->setProperty ("xruns", deviceManager.getXRunCount());
        send (juce::var (o.get()));
    }

    [[noreturn]] void quit()
    {
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
    juce::MidiBuffer midi;
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
        const auto err = app->openDevice (d >= 0 && d + 1 < args.size() ? args[d + 1] : juce::String(), 48000.0);
        auto ready = app->deviceInfo();
        ready->setProperty ("ev", "ready");
        if (err.isNotEmpty())
            ready->setProperty ("device_problem", err);
        tt::EngineApp::send (juce::var (ready.get()));
    }
    else
        tt::EngineApp::send (juce::var (tt::EngineApp::object ({ { "ev", "ready" } }).get()));

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
