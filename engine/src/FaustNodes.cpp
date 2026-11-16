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
// ---- automation ----------------------------------------------------------------------------------------------

juce::StringArray FaustNode::automate (const std::map<juce::String, AutoPts>& lanes, int latency)
{
    juce::StringArray unknown;
    auto s = std::make_shared<AutoState>();
    s->latency = latency;
    std::set<std::string> now;
    for (auto& [name, pts] : lanes)
    {
        const auto label = name.toStdString();
        auto zones = zonesFor (label);
        if (zones.empty() || pts.empty())
        {
            if (zones.empty())
                unknown.add (name);
            continue;
        }
        s->ctls.push_back ({ std::move (zones), pts });
        now.insert (label);
    }
    ever.insert (now.begin(), now.end());
    for (auto& label : ever)
        if (now.count (label) == 0)
            if (float v; settingFor (label, v))
                s->restore.push_back ({ zonesFor (label), v });
    automated = std::move (now);
    plan.set (std::move (s));
    return unknown;
}

const FaustNode::AutoState* FaustNode::autoBegin (juce::int64& t0, bool& moving)
{
    const AutoState* s = plan.get();
    if (s == nullptr)
        return nullptr;
    if (plan.takeChanged())
        for (auto& [zones, v] : s->restore)
            for (auto* z : zones)
                *z = v;
    if (s->ctls.empty())
        return nullptr;
    const auto* t = dynamic_cast<const Transport*> (getPlayHead());
    t0 = (t != nullptr ? t->blockPos : 0) - s->latency;
    moving = t != nullptr && t->blockPlaying;
    return s;
}

void FaustNode::autoSet (const AutoState& s, juce::int64 t)
{
    t -= ((t % autoGrid) + autoGrid) % autoGrid;
    for (auto& c : s.ctls)
    {
        const float v = autoValue (c.pts, t, c.hint);
        for (auto* z : c.zones)
            *z = v;
    }
}

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
        if (! isAutomated (it->first))
            *it->second = value;
        return true;
    }

    std::vector<float*> zonesFor (const std::string& label) override
    {
        auto& zones = ui.getLabelMap();
        auto it = zones.find (label);
        return it == zones.end() ? std::vector<float*>() : std::vector<float*> { it->second };
    }

    bool settingFor (const std::string& label, float& v) override
    {
        auto it = values.find (label);
        if (it == values.end())
            return false;
        v = it->second;
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
        juce::int64 t0 = 0;
        bool moving = false;
        const auto* a = autoBegin (t0, moving);
        if (a == nullptr || ! moving)
        {
            if (a != nullptr)
                autoSet (*a, t0);
            fx->compute (n, in, out);
            return;
        }
        float* pin[8];
        float* pout[8];
        for (int i = 0; i < n;)
        {
            const int k = autoSpan (t0 + i, n - i);
            autoSet (*a, t0 + i);
            for (int c = 0; c < ins; ++c)  pin[c] = in[c] + i;
            for (int c = 0; c < outs; ++c) pout[c] = out[c] + i;
            fx->compute (k, pin, pout);
            i += k;
        }
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

    std::vector<float*> zones (const std::string& label)
    {
        std::vector<float*> out;
        for (auto* voice : fVoiceTable)
        {
            auto& m = voice->getLabelMap();
            if (auto it = m.find (label); it != m.end())
                out.push_back (it->second);
        }
        return out;
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
        if (! isAutomated (key))
            poly->setAll (key, value);
        return true;
    }

    std::vector<float*> zonesFor (const std::string& label) override
    {
        if (label == "freq" || label == "gain" || label == "gate")  // played by the notes
            return {};
        return poly->zones (label);
    }

    bool settingFor (const std::string& label, float& v) override
    {
        auto it = values.find (label);
        if (it == values.end())
            return false;
        v = it->second;
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
        juce::int64 t0 = 0;
        bool moving = false;
        const auto* a = autoBegin (t0, moving);
        if (a != nullptr)
            autoSet (*a, t0);
        if (! moving)
            a = nullptr;  // set once for the block
        auto run = [&] (int upTo)
        {
            while (done < upTo)
            {
                int k = perSample ? 1 : juce::jmin (upTo - done, MIX_BUFFER_SIZE);
                if (a != nullptr)
                {
                    const juce::int64 t = t0 + done;
                    if (done > 0 && ((t % autoGrid) + autoGrid) % autoGrid == 0)
                        autoSet (*a, t);
                    k = autoSpan (t, k);
                }
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
