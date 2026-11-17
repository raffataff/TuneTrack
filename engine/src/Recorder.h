// Recording: the interface's input channels to WAV files, and the MIDI keyboard's events, while the transport runs.
//
// One recording is one continuous stretch of device time. Each armed audio track gets a file of its input channels
// as they arrived (before the strip's inserts). `marks` say where in that stretch the transport was: one at the
// start and one whenever it jumped (a loop wrapping round), as [samples into the recording, song position]. The
// Python side turns that into clips, one pass per time round the loop, moved earlier by the latency of the way out
// and the way in.
//
// A device that cannot run at the project's rate is recorded at its own (`deviceTime`): the files and every time
// counted in the recording are then in device samples, and the song positions in the marks stay in the project's.
#pragma once

#include "Nodes.h"

#include <array>

namespace tt
{
class Recorder
{
public:
    struct Track
    {
        juce::String id;
        juce::File file;
        int left = 0, right = -1;  // device channels; right < 0: a one-channel file
        std::unique_ptr<juce::AudioFormatWriter::ThreadedWriter> writer;
        float peak = 0.0f;
    };
    struct Mark { int64 at, pos; };
    struct Ev { int64 at; juce::uint8 status, d1, d2, src; };

    Recorder() : thread ("tt record")
    {
        events.resize (1 << 17);
        silence.resize (1 << 15, 0.0f);
        thread.startThread();
    }

    ~Recorder()
    {
        tracks.clear();  // each writer flushes what is still queued and closes its file
        thread.stopThread (4000);
    }

    // Message thread, before the recorder is handed to the audio thread. Returns the problem, or an empty string.
    juce::String add (const juce::String& id, const juce::File& file, int left, int right, double sampleRate)
    {
        file.getParentDirectory().createDirectory();
        file.deleteFile();
        std::unique_ptr<juce::OutputStream> stream (file.createOutputStream().release());
        juce::WavAudioFormat wav;
        const auto options = juce::AudioFormatWriterOptions().withSampleRate (sampleRate)
                                 .withNumChannels (right >= 0 ? 2 : 1).withBitsPerSample (24);
        auto writer = stream != nullptr ? wav.createWriterFor (stream, options) : nullptr;
        if (writer == nullptr)
            return "cannot write " + file.getFullPathName();
        Track t;
        t.id = id;
        t.file = file;
        t.left = left;
        t.right = right;
        t.writer = std::make_unique<juce::AudioFormatWriter::ThreadedWriter> (writer.release(), thread, 1 << 17);
        tracks.push_back (std::move (t));
        return {};
    }

    // Audio thread: `n` samples of the device block from `offset`, played at song position `pos`.
    void capture (const LiveIn& in, int offset, int n, int64 pos)
    {
        n = juce::jmin (n, (int) silence.size());
        mark (pos, n, written);
        write (in.audio, in.channels, in.midi, offset, n);
    }

    // Audio thread: the transport plays `n` samples from song position `pos`, heard `at` samples into the
    // recording. A position that does not follow on from the last one is a mark (the start, a loop wrapping).
    void mark (int64 pos, int n, int64 at)
    {
        if (pos != expected)
            if (const int m = numMarks.load (std::memory_order_relaxed); m < (int) marks.size())
            {
                marks[(size_t) m] = { at, pos };
                numMarks.store (m + 1, std::memory_order_release);
            }
        expected = pos + n;
    }

    // Audio thread: `n` samples of what came in, from `offset` in the device block, and the keyboards' events there.
    void write (const float* const* audio, int channels, const std::array<juce::MidiBuffer, LiveIn::sources>& midi, int offset, int n)
    {
        n = juce::jmin (n, (int) silence.size());
        for (auto& t : tracks)
        {
            const float* ch[2] = { silence.data(), silence.data() };
            const int want[2] = { t.left, t.right };
            for (int c = 0; c < 2; ++c)
                if (audio != nullptr && want[c] >= 0 && want[c] < channels && audio[want[c]] != nullptr)
                {
                    ch[c] = audio[want[c]] + offset;
                    const auto range = juce::FloatVectorOperations::findMinAndMax (ch[c], n);
                    t.peak = juce::jmax (t.peak, -range.getStart(), range.getEnd());
                }
            bool ok = t.writer->write (ch, n);
            for (int tries = 0; ! ok && patient && tries < 4000; ++tries)
            {
                juce::Thread::sleep (1);
                ok = t.writer->write (ch, n);
            }
            if (! ok)
                ++dropped;  // the disk fell behind
        }
        for (int src = 0; src < LiveIn::sources; ++src)
            for (const auto meta : midi[(size_t) src])
            {
                if (meta.samplePosition < offset || meta.samplePosition >= offset + n || meta.numBytes < 2 || meta.numBytes > 3)
                    continue;
                const int k = numEvents.load (std::memory_order_relaxed);
                if (k >= (int) events.size())
                    break;
                events[(size_t) k] = { written + meta.samplePosition - offset, meta.data[0], meta.data[1],
                                       (juce::uint8) (meta.numBytes > 2 ? meta.data[2] : 0), (juce::uint8) src };
                numEvents.store (k + 1, std::memory_order_release);
            }
        written += n;
    }

    // Message thread, once the audio thread no longer has the recorder: close the files and say what was recorded.
    juce::var finish()
    {
        auto* audio = new juce::DynamicObject();
        for (auto& t : tracks)
        {
            t.writer.reset();
            auto* o = new juce::DynamicObject();
            o->setProperty ("file", t.file.getFullPathName());
            o->setProperty ("channels", t.right >= 0 ? 2 : 1);
            o->setProperty ("peak", t.peak);
            audio->setProperty (juce::Identifier (t.id), juce::var (o));
        }
        juce::Array<juce::var> ms, ev, from;
        for (int i = 0; i < numMarks.load(); ++i)
        {
            ms.add (marks[(size_t) i].at);
            ms.add (marks[(size_t) i].pos);
        }
        for (int i = 0; i < numEvents.load(); ++i)
        {
            const auto& e = events[(size_t) i];
            ev.add (e.at);
            ev.add ((int) e.status);
            ev.add ((int) e.d1);
            ev.add ((int) e.d2);
            from.add ((int) e.src);
        }
        auto* o = new juce::DynamicObject();
        o->setProperty ("audio", juce::var (audio));
        o->setProperty ("marks", juce::var (ms));   // flat [samples into the recording, song position, ...]
        o->setProperty ("midi", juce::var (ev));    // flat [samples into the recording, status, data1, data2, ...]
        o->setProperty ("midi_src", juce::var (from));  // which keyboard each event came from (0 = sent to the engine)
        o->setProperty ("samples", written);
        o->setProperty ("file_sr", fileRate);
        o->setProperty ("dropped", dropped);
        return juce::var (o);
    }

    std::vector<Track> tracks;
    int64 written = 0;
    double fileRate = 0.0;     // the rate of the files and of every time counted in the recording
    bool deviceTime = false;   // recorded at the device's rate, a whole device block at a time (Main.cpp)
    bool patient = false;  // no device is waiting on the block (the `pull` op): wait for the disk instead of dropping

private:
    juce::TimeSliceThread thread;
    std::array<Mark, 4096> marks {};
    std::atomic<int> numMarks { 0 }, numEvents { 0 };
    std::vector<Ev> events;
    std::vector<float> silence;
    int64 expected = Transport::noSeek;
    int dropped = 0;
};

}  // namespace tt
