#include "FaustNodes.h"

#ifdef _MSC_VER
 #pragma warning (push, 0)
#endif

#include "faust/dsp/dsp.h"
#include "faust/gui/MapUI.h"
#include "faust/gui/meta.h"
#include "faust/dsp/poly-dsp.h"
#include "tt_faust_gen.h"

// Faust's GUI class keeps these as globals; one definition per program.
std::list<GUI*> GUI::fGuiList;
ztimedmap GUI::gTimedZoneMap;

#ifdef _MSC_VER
 #pragma warning (pop)
#endif

namespace tt
{
namespace
{
class FaustFx final : public FaustNode
{
public:
    FaustFx (::dsp* d, const juce::String& name)
        : FaustNode ("faust:" + name, d->getNumInputs(), d->getNumOutputs()), fx (d)
    {
        fx->buildUserInterface (&ui);
    }

    bool setParam (const juce::String& name, float value) override
    {
        auto& zones = ui.getLabelMap();
        auto it = zones.find (name.toStdString());
        if (it == zones.end())
            return false;
        values[it->first] = value;
        *it->second = value;
        return true;
    }

    void prepareToPlay (double sr, int block) override
    {
        fx->init ((int) sr);
        auto& zones = ui.getLabelMap();
        for (auto& [k, v] : values)  // init() puts every control back to its default
            *zones[k] = v;
        scratch.setSize (juce::jmax (1, fx->getNumInputs()), juce::jmax (16, block));
    }

    void reset() override { fx->instanceClear(); }

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer&) override
    {
        const int n = buf.getNumSamples();
        const int ins = fx->getNumInputs(), outs = fx->getNumOutputs();
        if (n == 0 || n > scratch.getNumSamples() || buf.getNumChannels() < juce::jmax (ins, outs))
            return;
        float* in[8];
        float* out[8];
        for (int c = 0; c < ins; ++c)
        {
            scratch.copyFrom (c, 0, buf, c, 0, n);  // Faust code may write an output before reading every input
            in[c] = scratch.getWritePointer (c);
        }
        for (int c = 0; c < outs; ++c)
            out[c] = buf.getWritePointer (c);
        fx->compute (n, in, out);
    }

private:
    std::unique_ptr<::dsp> fx;
    MapUI ui;
    std::map<std::string, float> values;
    juce::AudioBuffer<float> scratch;
};

struct Poly : public mydsp_poly
{
    using mydsp_poly::mydsp_poly;

    void setAll (const std::string& label, float v)
    {
        for (auto* voice : fVoiceTable)
        {
            auto& zones = voice->getLabelMap();
            auto it = zones.find (label);
            if (it != zones.end())
                *it->second = v;
        }
    }

    // Every voice stopped and its oscillators, filters and delay lines cleared: a render then starts the same
    // way each time.
    void silence()
    {
        allNotesOff (true);
        for (auto* voice : fVoiceTable)
            voice->instanceClear();
    }

    bool has (const std::string& label)
    {
        return ! fVoiceTable.empty() && fVoiceTable[0]->getLabelMap().count (label) > 0;
    }
};

class FaustSynth final : public FaustNode
{
public:
    FaustSynth (::dsp* d, const juce::String& name, int voices, bool oneSampleAtATime)
        : FaustNode ("synth:" + name, 0, 2), perSample (oneSampleAtATime)
    {
        poly = std::make_unique<Poly> (d, juce::jmax (1, voices), true, false);
    }

    bool acceptsMidi() const override { return true; }

    bool setParam (const juce::String& name, float value) override
    {
        const auto key = name.toStdString();
        if (key == "freq" || key == "gain" || key == "gate" || ! poly->has (key))
            return false;
        values[key] = value;
        poly->setAll (key, value);
        return true;
    }

    void prepareToPlay (double sr, int) override
    {
        poly->init ((int) sr);
        for (auto& [k, v] : values)
            poly->setAll (k, v);
    }

    void reset() override { poly->silence(); }

    void processBlock (juce::AudioBuffer<float>& buf, juce::MidiBuffer& midi) override
    {
        const int n = buf.getNumSamples();
        if (buf.getNumChannels() < 2 || n == 0)
            return;
        float* out[2];
        int done = 0;
        auto run = [&] (int upTo)
        {
            while (done < upTo)
            {
                const int k = perSample ? 1 : juce::jmin (upTo - done, MIX_BUFFER_SIZE);
                out[0] = buf.getWritePointer (0, done);
                out[1] = buf.getWritePointer (1, done);
                poly->compute (k, nullptr, out);
                done += k;
            }
        };
        for (const auto meta : midi)
        {
            run (juce::jlimit (0, n, meta.samplePosition));
            const auto m = meta.getMessage();
            if (m.isNoteOn())             poly->keyOn (0, m.getNoteNumber(), m.getVelocity());
            else if (m.isNoteOff())       poly->keyOff (0, m.getNoteNumber(), 0);
            else if (m.isAllNotesOff() || m.isAllSoundOff()) poly->allNotesOff (m.isAllSoundOff());
        }
        run (n);
        midi.clear();
    }

private:
    std::unique_ptr<Poly> poly;
    std::map<std::string, float> values;
    bool perSample;
};
}  // namespace

std::unique_ptr<FaustNode> createFaustFx (const juce::String& name, bool sidechain)
{
    if (auto* d = ttCreateFaust (sidechain ? "fxsc" : "fx", name.toStdString()))
        return std::make_unique<FaustFx> (d, name);
    return nullptr;
}

std::unique_ptr<FaustNode> createFaustSynth (const juce::String& templateName, int voices, bool perSample)
{
    if (auto* d = ttCreateFaust ("synth", templateName.toStdString()))
        return std::make_unique<FaustSynth> (d, templateName, voices, perSample);
    return nullptr;
}

}  // namespace tt
