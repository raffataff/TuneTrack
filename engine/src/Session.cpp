#include "Session.h"

#include "Params.h"

namespace tt
{
namespace
{
// How long a plugin gets to finish loading after it was handed a saved state (HALion loads its samples from
// message-thread callbacks). The offline worker waits 2 s for its one plugin; here every plugin of the session
// loads at once, so each new one adds to the wait. TT_SETTLE_MS / TT_SETTLE_EACH_MS override.
int settleBaseMs()
{
    static const int v = juce::SystemStats::getEnvironmentVariable ("TT_SETTLE_MS", "2000").getIntValue();
    return v;
}

int settleEachMs()
{
    static const int v = juce::SystemStats::getEnvironmentVariable ("TT_SETTLE_EACH_MS", "400").getIntValue();
    return v;
}

juce::String str (const juce::var& v, const char* key, const juce::String& fallback = {})
{
    const auto x = v[key];
    return (x.isVoid() || x.isUndefined()) ? fallback : x.toString();
}

double num (const juce::var& v, const char* key, double fallback)
{
    const auto x = v[key];
    return (x.isVoid() || x.isUndefined()) ? fallback : (double) x;
}

bool flag (const juce::var& v, const char* key, bool fallback)
{
    const auto x = v[key];
    return (x.isVoid() || x.isUndefined()) ? fallback : (bool) x;
}

int mainOuts (juce::AudioProcessor& p) { return p.getBusCount (false) > 0 ? juce::jmin (2, p.getMainBusNumOutputChannels()) : 0; }
int mainIns (juce::AudioProcessor& p)  { return p.getBusCount (true) > 0 ? juce::jmin (2, p.getMainBusNumInputChannels()) : 0; }

AutoPts points (const juce::var& v)
{
    AutoPts out;
    if (auto* a = v.getArray())
        for (auto& p : *a)
            if (auto* pair = p.getArray(); pair != nullptr && pair->size() >= 2)
                out.emplace_back ((int64) (double) (*pair)[0], (float) (double) (*pair)[1]);
    std::sort (out.begin(), out.end(), [] (auto& a, auto& b) { return a.first < b.first; });
    return out;
}

// How late the audio reaching each node of a graph is: the most plugin latency along any audio path into it (JUCE
// delays the other paths to match). MIDI paths add none.
class LatencyBefore
{
public:
    explicit LatencyBefore (juce::AudioProcessorGraph& g) : graph (g), conns (g.getConnections()) {}

    int operator() (juce::AudioProcessorGraph::NodeID id)
    {
        if (auto it = memo.find (id.uid); it != memo.end())
            return it->second;
        memo[id.uid] = 0;
        int most = 0;
        for (auto& c : conns)
            if (c.destination.nodeID == id && ! c.source.isMIDI())
                if (auto* src = graph.getNodeForId (c.source.nodeID))
                    most = juce::jmax (most, (*this) (c.source.nodeID) + src->getProcessor()->getLatencySamples());
        return memo[id.uid] = most;
    }

private:
    juce::AudioProcessorGraph& graph;
    std::vector<juce::AudioProcessorGraph::Connection> conns;
    std::map<juce::uint32, int> memo;
};

juce::String paramKeys (const juce::var& params)
{
    juce::StringArray keys;
    if (auto* o = params.getDynamicObject())
        for (auto& kv : o->getProperties())
            keys.add (kv.name.toString());
    keys.sort (false);
    return keys.joinIntoString (",");
}
}  // namespace

class Session::EditorWindow : public juce::DocumentWindow
{
public:
    EditorWindow (const juce::String& title, juce::AudioProcessorEditor* editor, std::function<void()> closed)
        : juce::DocumentWindow (title, juce::Colours::black, juce::DocumentWindow::closeButton), onClose (std::move (closed))
    {
        setUsingNativeTitleBar (true);
        setContentOwned (editor, true);
        setResizable (editor->isResizable(), false);
        centreWithSize (getWidth(), getHeight());
        setVisible (true);
        toFront (true);
    }

    void closeButtonPressed() override { onClose(); }

private:
    std::function<void()> onClose;
};

Session::Session (Transport& t) : transport (t)
{
    juce::addDefaultFormatsToManager (formats);
    graph.setPlayHead (&transport);
    outNode = graph.addNode (std::make_unique<Graph::AudioGraphIOProcessor> (Graph::AudioGraphIOProcessor::audioOutputNode));
}

bool Session::describe (const juce::String& path, const juce::File& out)
{
    juce::AudioPluginFormatManager fm;
    juce::addDefaultFormatsToManager (fm);
    juce::OwnedArray<juce::PluginDescription> types;
    for (auto* f : fm.getFormats())
        if (f->fileMightContainThisPluginType (path))
            f->findAllTypesForFile (types, path);
    juce::XmlElement root ("PLUGINS");
    for (auto* d : types)
        root.addChildElement (d->createXml().release());
    // several engines may list the same new plugin at once (offline renders run side by side): each writes its
    // own file and renames it, so nobody reads a half-written one
    const auto part = out.getSiblingFile (out.getFileName() + "." + juce::String (juce::Random::getSystemRandom().nextInt64()) + ".part");
    if (! root.writeTo (part))
        return false;
    if (! part.moveFileTo (out))
    {
        part.deleteFile();
        return out.existsAsFile();
    }
    return true;
}

Session::~Session()
{
    editors.clear();
    schedule.set (nullptr);
    autos.set (nullptr);
    graph.clear();
    slots.clear();
    lanes.clear();
}

void Session::prepare (double sr, int block, bool off)
{
    sampleRate = sr;
    blockSize = block;
    offline = off;
    graph.releaseResources();
    graph.setPlayConfigDetails (0, 2, sr, block);
    graph.setNonRealtime (off);       // each LaneNode tells its lane's devices
    graph.prepareToPlay (sr, block);  // each LaneNode prepares its lane and reports its latency
    updateLatencies();
}

// ---- nodes ---------------------------------------------------------------------------------------------------

Session::Slot* Session::take (const juce::String& owner, const juce::String& identity, const juce::String& ref, const Maker& make)
{
    // a device never moves between the session's graph and a lane: one that must is made afresh
    auto key = owner + "|" + (building != &graph ? "lane|" : "") + identity;
    key << "#" << seen[key]++;
    if (auto it = slots.find (key); it != slots.end())
    {
        auto& s = next[key];
        s = std::move (it->second);
        slots.erase (it);
        s.ref = ref;
        s.fresh = false;
        return &s;
    }
    juce::String error;
    auto proc = make (error);
    if (proc == nullptr)
    {
        warnings.add (ref + ": " + (error.isNotEmpty() ? error : juce::String ("could not be created")));
        return nullptr;
    }
    proc->setPlayHead (&transport);
    Slot s;
    trace ("add " + ref);
    s.node = building->addNode (std::move (proc), std::nullopt, Graph::UpdateKind::none);
    s.in = building;
    s.ref = ref;
    s.key = key;
    s.fresh = true;
    changed.insert (building);
    return &(next[key] = std::move (s));
}

std::unique_ptr<juce::AudioPluginInstance> Session::loadPlugin (const juce::var& dev, bool instrument, bool keyed, juce::String& error)
{
    const auto path = str (dev, "path");
    const auto shell = str (dev, "shell");
    auto& types = typeCache[path];
    if (types == nullptr)
    {
        // Listing what a file holds makes the plugin build and drop a throwaway instance. HALion Sonic's licence
        // client does not survive that: the next real instance in the same process crashes. So the listing is
        // done once per plugin file by a second, short-lived engine process and kept on disk.
        types = std::make_unique<juce::OwnedArray<juce::PluginDescription>>();
        const auto exe = juce::File::getSpecialLocation (juce::File::currentExecutableFile);
        const auto key = path + "|" + juce::String (juce::File (path).getLastModificationTime().toMilliseconds());
        const auto cache = exe.getSiblingFile ("plugin-cache").getChildFile (juce::String::toHexString (key.hashCode64()) + ".xml");
        if (! cache.existsAsFile())
        {
            trace ("describe " + path);
            cache.getParentDirectory().createDirectory();
            juce::ChildProcess child;
            if (child.start (juce::StringArray { exe.getFullPathName(), "--describe", path, cache.getFullPathName() }, 0))
                if (! child.waitForProcessToFinish (120000))
                    child.kill();
        }
        if (auto xml = juce::parseXML (cache))
            for (auto* e : xml->getChildIterator())
            {
                juce::PluginDescription d;
                if (d.loadFromXml (*e))
                    types->add (new juce::PluginDescription (d));
            }
    }
    const juce::PluginDescription* desc = nullptr;
    for (auto* d : *types)
        if (shell.isEmpty() || d->name.equalsIgnoreCase (shell))
        {
            desc = d;
            break;
        }
    if (desc == nullptr)
    {
        error = shell.isNotEmpty() ? "no plugin named '" + shell + "' in " + path : "no plugin found in " + path;
        return nullptr;
    }
    trace ("create " + desc->name);
    auto inst = formats.createPluginInstance (*desc, sampleRate, blockSize, error);
    if (inst == nullptr)
        return nullptr;
    trace ("created: ins " + juce::String (inst->getTotalNumInputChannels()) + " outs " + juce::String (inst->getTotalNumOutputChannels()));
    inst->setNonRealtime (offline);
    {
        // Some plugins (Valhalla Supermassive) arrive with their main buses switched off: no channels, no sound.
        auto layout = inst->getBusesLayout();
        bool changed = false;
        auto wake = [&] (juce::Array<juce::AudioChannelSet>& buses, bool isInput)
        {
            if (buses.size() > 0 && buses.getReference (0).isDisabled())
            {
                auto set = inst->getBus (isInput, 0)->getDefaultLayout();
                buses.getReference (0) = set.isDisabled() ? juce::AudioChannelSet::stereo() : set;
                changed = true;
            }
        };
        wake (layout.outputBuses, false);
        if (! instrument)
            wake (layout.inputBuses, true);
        if (changed && ! inst->setBusesLayout (layout))
        {
            if (auto* bus = inst->getBus (false, 0)) bus->enable (true);
            if (auto* bus = instrument ? nullptr : inst->getBus (true, 0)) bus->enable (true);
        }
        trace ("buses: ins " + juce::String (inst->getTotalNumInputChannels()) + " outs " + juce::String (inst->getTotalNumOutputChannels()));
    }
    if (! instrument)  // never touch an instrument's buses: it crashes HALion Sonic
    {
        if (! keyed && inst->getTotalNumInputChannels() > 2)
        {
            auto layout = inst->getBusesLayout();
            for (int i = 1; i < layout.inputBuses.size(); ++i)
                layout.inputBuses.getReference (i) = juce::AudioChannelSet::disabled();
            for (int i = 1; i < layout.outputBuses.size(); ++i)
                layout.outputBuses.getReference (i) = juce::AudioChannelSet::disabled();
            inst->setBusesLayout (layout);
        }
        else if (keyed)
        {
            if (auto* bus = inst->getBus (true, 1); bus != nullptr && ! bus->isEnabled())
                bus->enable (true);
        }
    }
    const auto state = str (dev, "state");
    if (state.isNotEmpty())
    {
        juce::MemoryBlock data;
        if (juce::File (state).loadFileAsData (data))
        {
            trace ("state " + state);
            inst->setStateInformation (data.getData(), (int) data.getSize());
            trace ("state set");
        }
        else
            warnings.add (str (dev, "name", path) + ": state file missing: " + state);
    }
    return inst;
}

void Session::applyPluginParams (Slot& s, const juce::var& params)
{
    auto* proc = s.node->getProcessor();
    s.staticValues.clear();
    if (auto* o = params.getDynamicObject())
        for (auto& kv : o->getProperties())
        {
            juce::String error, note;
            const int idx = findParam (*proc, kv.name.toString(), error);
            if (idx < 0)
            {
                warnings.add (s.ref + ": " + error);
                continue;
            }
            auto* p = proc->getParameters()[idx];
            if (kv.value.isString())
            {
                const float v = valueForText (*p, kv.value.toString(), note, error);
                if (v < 0.0f)
                {
                    warnings.add (s.ref + ": " + error);
                    continue;
                }
                p->setValue (v);
                s.staticValues[idx] = v;
                if (note.isNotEmpty())
                    warnings.add (s.ref + ": " + note);
            }
            else
            {
                p->setValue ((float) (double) kv.value);
                s.staticValues[idx] = p->getValue();
            }
        }
    s.applied = juce::JSON::toString (params, true);
}

void Session::resolveAutomation (Slot& s)
{
    const auto sig = juce::JSON::toString (s.autoSpec, true);
    if (sig == s.autoSig)
        return;
    s.autoSig = sig;
    s.autoParams.clear();
    auto* proc = s.node->getProcessor();
    auto* o = s.autoSpec.getDynamicObject();
    if (o == nullptr)
        return;
    for (auto& kv : o->getProperties())
    {
        juce::String error;
        const int idx = findParam (*proc, kv.name.toString(), error);
        if (idx < 0)
        {
            warnings.add (s.ref + ": automation: " + error);
            continue;
        }
        auto* p = proc->getParameters()[idx];
        if (s.preAuto.count (idx) == 0)
            s.preAuto[idx] = p->getValue();
        // the text a plugin shows ("-12 dB", "2 kHz") becomes the 0..1 value that shows it, once per distinct text
        std::map<juce::String, float> texts;
        juce::StringArray notes;
        AutoPts pts;
        if (auto* a = kv.value.getArray())
            for (auto& pt : *a)
            {
                auto* pair = pt.getArray();
                if (pair == nullptr || pair->size() < 2)
                    continue;
                const auto& v = (*pair)[1];
                float x;
                if (v.isString())
                {
                    const auto text = v.toString();
                    if (auto it = texts.find (text); it != texts.end())
                        x = it->second;
                    else
                    {
                        juce::String note, err;
                        x = texts[text] = valueForText (*p, text, note, err);
                        if (x < 0.0f)
                            warnings.add (s.ref + ": automation: " + err);
                        else if (note.isNotEmpty())
                            notes.addIfNotAlreadyThere (note);
                    }
                    if (x < 0.0f)
                        continue;
                }
                else
                    x = juce::jlimit (0.0f, 1.0f, (float) (double) v);
                pts.emplace_back ((juce::int64) (double) (*pair)[0], x);
            }
        for (auto& n : notes)
            warnings.add (s.ref + ": automation: " + n);
        std::stable_sort (pts.begin(), pts.end(), [] (auto& a, auto& b) { return a.first < b.first; });
        if (! pts.empty())
            s.autoParams.emplace_back (idx, std::move (pts));
    }
}

void Session::publishAutomation()
{
    struct Build
    {
        std::shared_ptr<AutoPlan> plan = std::make_shared<AutoPlan>();
        juce::String sig;
    };
    std::map<Graph*, Build> builds;
    std::map<Graph*, std::unique_ptr<LatencyBefore>> walks;
    auto latencyOf = [&] (Slot& s)
    {
        auto& w = walks[s.in];
        if (w == nullptr)
            w = std::make_unique<LatencyBefore> (*s.in);
        return (*w) (s.node->nodeID);
    };

    for (auto& [key, s] : slots)
    {
        if (! s.deferred.isVoid())
            continue;  // its state is still loading: finishSettle publishes it
        if (auto* f = dynamic_cast<FaustNode*> (s.node->getProcessor()))
        {
            std::map<juce::String, AutoPts> lanes;
            if (auto* o = s.autoSpec.getDynamicObject())
                for (auto& kv : o->getProperties())
                    if (auto pts = numericPoints (kv.value); ! pts.empty())
                        lanes[kv.name.toString()] = std::move (pts);
            if (lanes.empty() && s.faustSig.isEmpty())
                continue;
            const int lat = lanes.empty() ? 0 : latencyOf (s);
            const auto sig = juce::JSON::toString (s.autoSpec, true) + "|" + juce::String (lat);
            if (sig != s.faustSig)
            {
                s.faustSig = sig;
                for (auto& bad : f->automate (lanes, lat))
                    warnings.add (s.ref + ": automation: no setting '" + bad + "'");
            }
            continue;
        }
        if (s.autoParams.empty() && s.preAuto.empty())
            continue;
        auto& b = builds[s.in];
        const int lat = s.autoParams.empty() ? 0 : latencyOf (s);
        auto& params = s.node->getProcessor()->getParameters();
        b.plan->keep.push_back (s.node);
        std::set<int> now;
        for (auto& [idx, pts] : s.autoParams)
        {
            b.plan->params.push_back ({ s.node, params[idx], pts, lat });
            now.insert (idx);
        }
        for (auto& [idx, before] : s.preAuto)
            if (now.count (idx) == 0)
            {
                auto it = s.staticValues.find (idx);
                b.plan->restore.push_back ({ params[idx], it != s.staticValues.end() ? it->second : before });
            }
        // a setting changed under an automated parameter moved it: a new plan sets every value again
        b.sig << key << "|" << s.autoSig << "|" << lat << "|" << s.applied << ";";
    }

    auto publish = [&] (Graph* g, Shared<AutoPlan>& to, juce::String& had)
    {
        auto it = builds.find (g);
        const auto sig = it != builds.end() ? it->second.sig : juce::String();
        if (sig == had)
            return;
        had = sig;
        to.set (it != builds.end() ? std::shared_ptr<const AutoPlan> (it->second.plan) : nullptr);
    };
    publish (&graph, autos, autoSig);
    for (auto& [id, lane] : lanes)
        publish (&lane->graph, lane->autos, lane->autoSig);
}

Session::Slot* Session::device (const juce::String& owner, const juce::String& ref, const juce::var& dev, bool instrument, bool keyed)
{
    const auto type = str (dev, "type");
    const auto params = dev["params"];

    if (type == "vst3")
    {
        const auto state = str (dev, "state");
        juce::String id = "vst3|" + str (dev, "path") + "|" + str (dev, "shell") + "|" + state + "|";
        if (state.isNotEmpty())
            id << juce::File (state).getLastModificationTime().toMilliseconds();
        id << "|" << (instrument ? "i" : "f") << (keyed ? "k" : "") << "|" << paramKeys (params);
        auto* s = take (owner, id, ref, [&] (juce::String& e) { return loadPlugin (dev, instrument, keyed, e); });
        if (s == nullptr)
            return nullptr;
        s->plugin = true;
        s->autoSpec = dev["auto"];
        if (s->fresh && state.isNotEmpty())
        {
            s->deferred = params;
            ++freshStates;
            settleUntil = juce::Time::currentTimeMillis() + settleBaseMs() + settleEachMs() * freshStates;
        }
        else if (! s->deferred.isVoid())
            s->deferred = params;
        else
        {
            if (s->applied != juce::JSON::toString (params, true))
                applyPluginParams (*s, params);
            resolveAutomation (*s);
        }
        return s;
    }

    Slot* s = nullptr;
    if (type == "faust")
    {
        const auto name = str (dev, "name");
        s = take (owner, "faust|" + name + (keyed ? "|sc" : ""), ref,
                  [&] (juce::String& e) -> std::unique_ptr<juce::AudioProcessor>
                  {
                      auto n = createFaustFx (name, keyed);
                      if (n == nullptr) e = "no built-in effect '" + name + "'";
                      return n;
                  });
    }
    else if (type == "synth")
    {
        const auto name = str (dev, "template");
        const int voices = (int) num (dev, "voices", 32);
        s = take (owner, "synth|" + name + "|" + juce::String (voices) + (perSampleSynths ? "|1" : ""), ref,
                  [&] (juce::String& e) -> std::unique_ptr<juce::AudioProcessor>
                  {
                      auto n = createFaustSynth (name, voices, perSampleSynths);
                      if (n == nullptr) e = "no built-in synth template '" + name + "'";
                      return n;
                  });
    }
    else if (type == "sfz")
    {
        const juce::File file (str (dev, "path"));
        const int voices = (int) num (dev, "voices", 128);
        const bool fullCC7 = flag (dev, "full_cc7", true);
        s = take (owner, "sfz|" + file.getFullPathName() + "|" + juce::String (file.getLastModificationTime().toMilliseconds())
                      + "|" + juce::String (voices) + (fullCC7 ? "|cc7" : ""), ref,
                  [&] (juce::String& e) -> std::unique_ptr<juce::AudioProcessor>
                  { return SfzNode::load (file, sampleRate, blockSize, voices, fullCC7, offline, e); });
        if (s != nullptr && dev["auto"].getDynamicObject() != nullptr)
            warnings.add (ref + ": automation: an SFZ instrument has no settings to automate (use CC)");
        return s;
    }
    else if (type == "stem")
    {
        const juce::File file (str (dev, "file"));
        s = take (owner, "stem|" + file.getFullPathName() + "|" + juce::String (file.getLastModificationTime().toMilliseconds()), ref,
                  [&] (juce::String& e) -> std::unique_ptr<juce::AudioProcessor>
                  {
                      auto n = std::make_unique<StemNode> (transport, file);
                      if (! n->ok) { e = "cannot read " + file.getFullPathName(); return nullptr; }
                      return n;
                  });
        return s;
    }
    else
    {
        warnings.add (ref + ": unknown device type '" + type + "'");
        return nullptr;
    }

    if (s != nullptr)
        if (auto* f = dynamic_cast<FaustNode*> (s->node->getProcessor()))
        {
            s->autoSpec = dev["auto"];  // follows from publishAutomation, once the latency before it is known
            if (auto* o = params.getDynamicObject())
                for (auto& kv : o->getProperties())
                    f->setParam (kv.name.toString(), (float) (double) kv.value);
        }
    return s;
}

// ---- wiring --------------------------------------------------------------------------------------------------

void Session::link (Graph::Node* from, Graph::Node* to)
{
    if (from == nullptr || to == nullptr)
        return;
    const bool toOutput = dynamic_cast<Graph::AudioGraphIOProcessor*> (to->getProcessor()) != nullptr;
    const int outs = mainOuts (*from->getProcessor());
    const int ins = toOutput ? 2 : mainIns (*to->getProcessor());
    for (int c = 0; c < ins && outs > 0; ++c)  // a mono source feeds both sides; a mono input hears the left
        wiring->push_back ({ { from->nodeID, juce::jmin (c, outs - 1) }, { to->nodeID, c } });
}

void Session::linkKey (Graph::Node* from, Graph::Node* fx, const juce::String& owner, const juce::String& ref)
{
    if (from == nullptr || fx == nullptr)
        return;
    auto& p = *fx->getProcessor();
    const int outs = mainOuts (*from->getProcessor());
    int base = -1, count = 0;
    if (dynamic_cast<FaustNode*> (&p) != nullptr)
    {
        base = 2;
        count = p.getTotalNumInputChannels() - 2;
    }
    else if (auto* bus = p.getBus (true, 1); bus != nullptr && bus->isEnabled())
    {
        base = p.getChannelIndexInProcessBlockBuffer (true, 1, 0);
        count = bus->getNumberOfChannels();
    }
    if (count == 1 && outs >= 2)
    {
        // a mono key input hears both sides, (L + R) / 2, as the offline worker feeds it (not the left alone)
        if (auto* m = take (owner, "mono|" + ref, ref + "/key",
                            [] (juce::String&) -> std::unique_ptr<juce::AudioProcessor> { return std::make_unique<MonoNode>(); }))
        {
            link (from, m->node.get());
            wiring->push_back ({ { m->node->nodeID, 0 }, { fx->nodeID, base } });
        }
        return;
    }
    for (int c = 0; c < juce::jmin (2, count) && outs > 0; ++c)
        wiring->push_back ({ { from->nodeID, juce::jmin (c, outs - 1) }, { fx->nodeID, base + c } });
}

juce::var Session::apply (const juce::var& spec)
{
    warnings.clear();
    next.clear();
    seen.clear();
    strips.clear();
    probeWants.clear();
    wires.clear();
    nextLanes.clear();
    changed.clear();
    building = &graph;
    wiring = &wires;
    freshStates = 0;

    // ---- transport ----
    transport.sampleRate = num (spec, "sr", sampleRate);
    if (auto* ts = spec["ts"].getArray(); ts != nullptr && ts->size() >= 2)
    {
        transport.tsNum = (int) (*ts)[0];
        transport.tsDen = (int) (*ts)[1];
    }
    auto tempo = std::make_shared<std::vector<TempoPt>>();
    if (auto* a = spec["tempo"].getArray())
        for (auto& t : *a)
            if (auto* p = t.getArray(); p != nullptr && p->size() >= 3)
                tempo->push_back ({ (int64) (double) (*p)[0], (double) (*p)[1], (double) (*p)[2] });
    transport.tempo.set (tempo);
    transport.endSample = (int64) num (spec, "end", 0);
    if (auto* lp = spec["loop"].getArray(); lp != nullptr && lp->size() >= 2)
    {
        transport.loopStart = (int64) (double) (*lp)[0];
        transport.loopEnd = (int64) (double) (*lp)[1];
    }
    else
    {
        transport.loopStart = 0;
        transport.loopEnd = 0;
    }

    struct KeyWant { juce::String track; Graph::Node::Ptr fx; juce::String ref, owner; };
    std::vector<KeyWant> keys;
    struct Route { juce::String from, to; float db; bool send; int n; };
    std::vector<Route> routes;

    auto gainNode = [] (juce::String&) -> std::unique_ptr<juce::AudioProcessor> { return std::make_unique<GainNode>(); };
    auto meterNode = [] (juce::String&) -> std::unique_ptr<juce::AudioProcessor> { return std::make_unique<MeterNode>(); };
    auto faderNode = [this] (juce::String&) -> std::unique_ptr<juce::AudioProcessor> { return std::make_unique<FaderNode> (transport); };

    auto buildChain = [&] (const juce::String& id, const juce::var& fxList, Graph::Node::Ptr prev) -> Graph::Node::Ptr
    {
        if (auto* fx = fxList.getArray())
            for (int i = 0; i < fx->size(); ++i)
            {
                const auto& dev = (*fx)[i];
                const auto key = str (dev, "key");
                const auto ref = id + "/fx/" + juce::String (i);
                if (auto* s = device (id, ref, dev, false, key.isNotEmpty()))
                {
                    link (prev.get(), s->node.get());
                    if (key.isNotEmpty() && key != "silent")
                        keys.push_back ({ key, s->node, ref, id });
                    prev = s->node;
                }
            }
        return prev;
    };

    // an insert that listens to another strip keeps its strip in the session's graph, where JUCE lines the key up
    auto listens = [] (const juce::var& fxList)
    {
        if (auto* fx = fxList.getArray())
            for (auto& dev : *fx)
                if (const auto key = str (dev, "key"); key.isNotEmpty() && key != "silent")
                    return true;
        return false;
    };

    // ---- strips ----
    if (auto* tracks = spec["tracks"].getArray())
        for (auto& t : *tracks)
        {
            const auto id = str (t, "id");
            Strip strip;
            Graph::Node::Ptr prev;
            building = &graph;
            wiring = &wires;
            if (str (t, "kind") == "bus")
            {
                if (auto* s = take (id, "head", id + "/in", gainNode))
                    prev = strip.head = s->node;
                strip.tap = buildChain (id, t["fx"], prev);
            }
            else if (const auto src = t["source"], live = t["input"]; src.isObject() || live.isObject())
            {
                // "input": {"ch": [left, right?], "monitor": bool}: the interface's input joins the strip before
                // its inserts (heard while monitoring). A strip may have an input and nothing recorded yet.
                const auto type = str (src, "type");
                std::shared_ptr<Lane> lane;
                if (! listens (t["fx"]))
                {
                    // computed in a lane of its own, side by side with the others (Lanes.h)
                    if (auto it = lanes.find (id); it != lanes.end())
                        lane = it->second;
                    else
                    {
                        lane = std::make_shared<Lane> (transport);
                        lane->prepare (sampleRate, blockSize);
                        changed.insert (&lane->graph);
                    }
                    nextLanes[id] = lane;
                    lane->weight = type == "vst3" ? 4 : type == "sfz" ? 2 : 1;
                    if (auto* fx = t["fx"].getArray())
                        for (auto& dev : *fx)
                            lane->weight += str (dev, "type") == "vst3" ? 2 : 0;
                    building = &lane->graph;
                    wiring = &laneWires[id];
                    wiring->clear();
                }
                if (auto* s = src.isObject() ? device (id, id + "/inst", src, true, false) : nullptr)
                {
                    prev = s->node;
                    if (auto* stem = dynamic_cast<StemNode*> (s->node->getProcessor()))
                        stem->duck = flag (src, "duck", false);
                    const auto instKey = s->key;
                    Graph::Node::Ptr meter;
                    if (type == "vst3")
                        if (auto* mt = take (id, "meter", id + "/meter", meterNode))
                        {
                            link (prev.get(), mt->node.get());
                            prev = meter = mt->node;
                        }
                    if (type != "stem")
                        if (auto* m = take (id, "midi", id + "/midi", [this] (juce::String&) -> std::unique_ptr<juce::AudioProcessor>
                                            { return std::make_unique<MidiSourceNode> (transport); }))
                        {
                            auto seq = std::make_shared<MidiSeq>();
                            juce::int64 hash = 1469598103934665603LL;
                            if (auto* ev = src["events"].getArray())
                            {
                                seq->reserve ((size_t) ev->size() / 4);
                                for (int i = 0; i + 3 < ev->size(); i += 4)
                                {
                                    MidiEv e { (int64) (double) (*ev)[i], (juce::uint8) (int) (*ev)[i + 1],
                                               (juce::uint8) (int) (*ev)[i + 2], (juce::uint8) (int) (*ev)[i + 3] };
                                    hash = (hash ^ (e.sample * 16777619LL + e.status * 65536 + e.d1 * 256 + e.d2)) * 1099511628211LL;
                                    seq->push_back (e);
                                }
                            }
                            const auto sig = juce::String (hash) + ":" + juce::String ((int) seq->size());
                            auto* source = static_cast<MidiSourceNode*> (m->node->getProcessor());
                            if (m->applied != sig)
                            {
                                source->seq.set (seq);
                                m->applied = sig;
                            }
                            source->thruChannel = (int) num (src, "channel", 0) + 1;
                            source->thru = flag (src, "thru", false);
                            source->thruFrom = (int) num (src, "thru_from", 0);
                            wiring->push_back ({ { m->node->nodeID, Graph::midiChannelIndex }, { s->node->nodeID, Graph::midiChannelIndex } });
                            if (meter != nullptr)
                            {
                                std::vector<const MidiEv*> ons;
                                for (auto& e : *seq)
                                    if ((e.status & 0xf0) == 0x90 && e.d2 > 0)
                                        ons.push_back (&e);
                                if (! ons.empty())
                                {
                                    std::sort (ons.begin(), ons.end(), [] (auto* a, auto* b) { return a->d1 < b->d1; });
                                    const auto* e = ons[ons.size() / 2];
                                    probeWants.push_back ({ instKey, id + "/inst", m->node, meter, (e->status & 0x0f) + 1, e->d1, e->d2 });
                                }
                            }
                        }
                }
                if (live.isObject())
                    if (auto* in = take (id, "input", id + "/input", [this] (juce::String&) -> std::unique_ptr<juce::AudioProcessor>
                                         { return std::make_unique<InputNode> (transport); }))
                    {
                        auto* node = static_cast<InputNode*> (in->node->getProcessor());
                        auto* ch = live["ch"].getArray();
                        node->left = ch != nullptr && ch->size() > 0 ? (int) (*ch)[0] : 0;
                        node->right = ch != nullptr && ch->size() > 1 ? (int) (*ch)[1] : -1;
                        node->monitor = flag (live, "monitor", false);
                        if (auto* sum = take (id, "insum", id + "/insum", gainNode))  // what is recorded + what comes in
                        {
                            link (prev.get(), sum->node.get());
                            link (in->node.get(), sum->node.get());
                            prev = sum->node;
                        }
                    }
                prev = buildChain (id, t["fx"], prev);
                if (lane != nullptr)
                {
                    link (prev.get(), lane->outNode.get());
                    building = &graph;
                    wiring = &wires;
                    prev = nullptr;
                    if (auto* ln = take (id, "lane|" + juce::String (lane->serial), id + "/lane",
                                         [this, lane] (juce::String&) -> std::unique_ptr<juce::AudioProcessor>
                                         { return std::make_unique<LaneNode> (lane, pool); }))
                        prev = ln->node;
                }
                strip.tap = prev;
            }
            building = &graph;
            wiring = &wires;
            if (auto* f = take (id, "fader", id + "/fader", faderNode))
            {
                auto p = std::make_shared<FaderParams>();
                p->db = (float) num (t, "db", 0.0);
                p->pan = (float) num (t, "pan", 0.0);
                p->on = flag (t, "on", true);
                p->volAuto = points (t["vol_auto"]);
                p->panAuto = points (t["pan_auto"]);
                static_cast<FaderNode*> (f->node->getProcessor())->publish (p);
                strip.fader = f->node;
                link (strip.tap.get(), f->node.get());
            }
            strips[id] = strip;
            routes.push_back ({ id, str (t, "out", "master"), 0.0f, false, 0 });
            if (auto* sends = t["sends"].getArray())
                for (int i = 0; i < sends->size(); ++i)
                    if (auto* s = (*sends)[i].getArray(); s != nullptr && s->size() >= 2)
                        routes.push_back ({ id, (*s)[0].toString(), (float) (double) (*s)[1], true, i });
        }

    // ---- master ----
    Strip master;
    const auto m = spec["master"];
    if (auto* s = take ("master", "head", "master/in", gainNode))
        master.head = s->node;
    master.tap = buildChain ("master", m["fx"], master.head);
    if (auto* f = take ("master", "fader", "master/fader", faderNode))
    {
        auto p = std::make_shared<FaderParams>();
        p->db = (float) num (m, "db", 0.0);
        static_cast<FaderNode*> (f->node->getProcessor())->publish (p);
        master.fader = f->node;
        link (master.tap.get(), f->node.get());
        link (f->node.get(), outNode.get());
    }
    // the metronome: after the master, never in a file (Transport::click is off outside live playback)
    if (auto* c = take ("master", "click", "master/click", [this] (juce::String&) -> std::unique_ptr<juce::AudioProcessor>
                        { return std::make_unique<ClickNode> (transport); }))
        link (c->node.get(), outNode.get());

    // ---- routing, sends, sidechain keys ----
    for (auto& r : routes)
    {
        auto& from = strips[r.from];
        if (from.fader == nullptr)
            continue;
        if (r.to.isEmpty())
            continue;  // a bus the mixer leaves out (caught in a routing loop): it goes nowhere
        if (r.to == "direct")
        {
            // past the master, straight to the output (the picture's own sound as a guide: never in a file, and the
            // master's inserts and fader leave it alone). The graph delays it to line up with the master's latency.
            if (! r.send && outNode != nullptr)
                link (from.fader.get(), outNode.get());
            continue;
        }
        auto target = master.head;
        if (auto it = strips.find (r.to); r.to != "master" && it != strips.end() && it->second.head != nullptr)
            target = it->second.head;
        else if (r.send)
            continue;  // a send to something that is not a bus
        if (! r.send)
            link (from.fader.get(), target.get());
        else if (auto* s = take (r.from, "send|" + r.to, r.from + "/send/" + juce::String (r.n), gainNode))
        {
            static_cast<GainNode*> (s->node->getProcessor())->gain = std::pow (10.0f, r.db / 20.0f);
            link (from.fader.get(), s->node.get());
            link (s->node.get(), target.get());
        }
    }
    for (auto& k : keys)
    {
        auto it = strips.find (k.track);
        if (it == strips.end() || it->second.tap == nullptr)
            warnings.add (k.ref + ": sidechain key has no audio; the effect hears silence");
        else
            linkKey (it->second.tap.get(), k.fx.get(), k.owner, k.ref);
    }
    strips["master"] = master;

    // ---- drop what is gone, rewire only the graphs whose wiring changed ----
    for (auto& [key, s] : slots)
    {
        editors.erase (key);
        s.in->removeNode (s.node.get(), Graph::UpdateKind::none);
        changed.insert (s.in);
    }
    slots = std::move (next);
    next.clear();
    lanes = std::move (nextLanes);  // a lane dropped here lives on until neither the schedule nor its node holds it
    nextLanes.clear();
    for (auto it = laneWires.begin(); it != laneWires.end();)
        it = lanes.count (it->first) > 0 ? std::next (it) : laneWires.erase (it);

    rebuildAll();

    // the lanes the audio thread computes from the next block on, heaviest first so the long ones start early
    auto plan = std::make_shared<LaneSchedule>();
    for (auto& [id, lane] : lanes)
        plan->lanes.push_back (lane);
    std::stable_sort (plan->lanes.begin(), plan->lanes.end(), [] (auto& a, auto& b) { return a->weight > b->weight; });
    schedule.set (std::move (plan));

    auto* result = new juce::DynamicObject();
    result->setProperty ("warnings", juce::var (warnings));
    result->setProperty ("latency", totalLatency);
    result->setProperty ("nodes", (int) slots.size());
    result->setProperty ("lanes", (int) lanes.size());
    result->setProperty ("threads", pool.threads());
    result->setProperty ("settle_ms", settleRemainingMs());
    return juce::var (result);
}

void Session::rebuildAll()
{
    auto order = [] (const Graph::Connection& a, const Graph::Connection& b) { return a < b; };
    auto rewire = [&] (Graph& g, std::vector<Graph::Connection>& want, std::vector<Graph::Connection>& have)
    {
        std::sort (want.begin(), want.end(), order);
        if (changed.count (&g) > 0 || want != have)
        {
            for (auto& c : g.getConnections())
                g.removeConnection (c, Graph::UpdateKind::none);
            for (auto& c : want)
                if (! g.addConnection (c, Graph::UpdateKind::none))
                    warnings.add ("engine: a connection was refused");
            have = want;
        }
        g.rebuild();  // nothing happens unless the wiring or a device's latency changed
    };
    trace ("rebuild");
    // lanes first: the session's graph lines strips up by each lane's latency
    for (auto& [id, lane] : lanes)
        rewire (lane->graph, laneWires[id], lane->wired);
    for (auto& [id, s] : strips)
        if (s.tap != nullptr)
            if (auto* ln = dynamic_cast<LaneNode*> (s.tap->getProcessor()))
                ln->syncLatency();
    rewire (graph, wires, wired);
    changed.clear();
    trace ("rebuilt");
    for (auto& c : tracing() ? graph.getConnections() : std::vector<Graph::Connection>())
    {
        auto* from = graph.getNodeForId (c.source.nodeID);
        auto* to = graph.getNodeForId (c.destination.nodeID);
        trace ("  wire " + juce::String (c.source.nodeID.uid) + ":" + from->getProcessor()->getName() + "[" + juce::String (c.source.channelIndex)
               + "] -> " + juce::String (c.destination.nodeID.uid) + ":" + to->getProcessor()->getName() + "[" + juce::String (c.destination.channelIndex) + "]");
    }
    updateLatencies();
}

void Session::updateLatencies()
{
    LatencyBefore before (graph);
    for (auto& [id, s] : strips)
        if (s.fader != nullptr)
            static_cast<FaderNode*> (s.fader->getProcessor())->latencyBefore = before (s.fader->nodeID);
    totalLatency = outNode != nullptr ? before (outNode->nodeID) : 0;
    publishAutomation();
}

int Session::settleRemainingMs() const
{
    return (int) juce::jmax<juce::int64> (0, settleUntil - juce::Time::currentTimeMillis());
}

juce::StringArray Session::finishSettle()
{
    warnings.clear();
    for (auto& [key, s] : slots)
        if (! s.deferred.isVoid())
        {
            applyPluginParams (s, s.deferred);
            s.deferred = juce::var();
            resolveAutomation (s);
        }
    // a plugin may only report its real latency once its state has loaded
    rebuildAll();
    return warnings;
}

void Session::resetBuiltIns()
{
    for (auto& [key, s] : slots)
        if (auto* f = dynamic_cast<FaustNode*> (s.node->getProcessor()))
            f->reset();
        else if (auto* z = dynamic_cast<SfzNode*> (s.node->getProcessor()))
            z->reset();
}

// ---- the ready probe -----------------------------------------------------------------------------------------

bool Session::hasUnproven() const
{
    for (auto& w : probeWants)
        if (auto it = slots.find (w.slot); it != slots.end() && ! it->second.ready)
            return true;
    return false;
}

int Session::probeBegin (juce::int64 length)
{
    int n = 0;
    for (auto& w : probeWants)
        if (auto it = slots.find (w.slot); it != slots.end() && ! it->second.ready)
        {
            static_cast<MidiSourceNode*> (w.midi->getProcessor())->startProbe (w.channel, w.note, w.velocity, length);
            static_cast<MeterNode*> (w.meter->getProcessor())->start();
            ++n;
        }
    return n;
}

int Session::probeEnd()
{
    int left = 0;
    for (auto& w : probeWants)
    {
        auto it = slots.find (w.slot);
        auto* m = static_cast<MeterNode*> (w.meter->getProcessor());
        if (it == slots.end() || it->second.ready || ! m->on)
            continue;
        m->on = false;
        auto& s = it->second;
        const bool silent = m->peak < 1.0e-4f;
        const float db = (m->count > 0 && m->sum > 0.0) ? (float) (10.0 * std::log10 (m->sum / (2.0 * (double) m->count))) : -1000.0f;
        if (! silent && s.probes > 0 && ! s.probeSilent && db <= s.probeDb + 1.5f)
            s.ready = true;
        trace ("probe " + w.ref + ": " + juce::String (db, 1) + " dB" + (silent ? " (silent)" : "") + (s.ready ? " ready" : ""));
        s.probeDb = db;
        s.probeSilent = silent;
        ++s.probes;
        if (! s.ready)
            ++left;
    }
    return left;
}

juce::StringArray Session::giveUpProbes()
{
    juce::StringArray out;
    for (auto& w : probeWants)
        if (auto it = slots.find (w.slot); it != slots.end() && ! it->second.ready)
        {
            auto& s = it->second;
            s.ready = true;
            if (s.probes == 0)
                continue;  // the probe was skipped ("probe": false): nothing to report
            out.add (w.ref + (s.probeSilent ? ": made no sound when one of its own notes was tried"
                                            : ": was still getting louder when the render started (still loading?)"));
        }
    return out;
}

// ---- mix: faders, pans and sends moved now, without a reload ---------------------------------------------------

juce::String Session::mix (const juce::var& changes)
{
    juce::String problems;
    if (auto* a = changes.getArray())
        for (auto& c : *a)
        {
            const auto id = c["id"].toString();
            if (c.hasProperty ("to"))
            {
                // a send's gain node (the routing pass: take (from, "send|" + to) in the session's graph)
                auto it = slots.find (id + "|send|" + c["to"].toString() + "#0");
                if (it == slots.end() || it->second.node == nullptr || ! c.hasProperty ("db"))
                    problems << id << ": no send to " << c["to"].toString() << "; ";
                else
                    static_cast<GainNode*> (it->second.node->getProcessor())->gain = std::pow (10.0f, (float) (double) c["db"] / 20.0f);
                continue;
            }
            auto it = strips.find (id);
            if (it == strips.end() || it->second.fader == nullptr)
            {
                problems << id << ": no such strip; ";
                continue;
            }
            auto* f = static_cast<FaderNode*> (it->second.fader->getProcessor());
            const FaderParams* was = f->published.get();
            auto p = std::make_shared<FaderParams> (was != nullptr ? *was : FaderParams {});
            // an automated fader keeps its curve and plays it offset by the move, measured from the spec's level
            if (c.hasProperty ("db"))
            {
                const float db = (float) (double) c["db"];
                if (p->volAuto.empty()) p->db = db;
                else p->trim = db - p->db;
            }
            if (c.hasProperty ("pan"))
            {
                const float pan = juce::jlimit (-1.0f, 1.0f, (float) (double) c["pan"]);
                if (p->panAuto.empty()) p->pan = pan;
                else p->panTrim = pan - p->pan;
            }
            f->publish (p);
        }
    return problems;
}

// ---- taps ----------------------------------------------------------------------------------------------------

juce::String Session::armTaps (const juce::var& taps, juce::int64 length)
{
    juce::String problems;
    if (auto* o = taps.getDynamicObject())
        for (auto& kv : o->getProperties())
        {
            const auto id = kv.name.toString();
            auto it = strips.find (id);
            if (it == strips.end() || it->second.fader == nullptr)
            {
                problems << id << ": no such strip; ";
                continue;
            }
            const juce::File file (kv.value.toString());
            file.getParentDirectory().createDirectory();
            file.deleteFile();
            std::unique_ptr<juce::OutputStream> stream (file.createOutputStream().release());
            juce::WavAudioFormat wav;
            const auto options = juce::AudioFormatWriterOptions()
                                     .withSampleRate (sampleRate)
                                     .withNumChannels (2)
                                     .withBitsPerSample (32)
                                     .withSampleFormat (juce::AudioFormatWriterOptions::SampleFormat::floatingPoint);
            auto writer = stream != nullptr ? wav.createWriterFor (stream, options) : nullptr;
            if (writer == nullptr)
            {
                problems << id << ": cannot write " << file.getFullPathName() << "; ";
                continue;
            }
            auto* f = static_cast<FaderNode*> (it->second.fader->getProcessor());
            f->tap = std::move (writer);
            f->tapSkip = f->latencyBefore.load();
            f->tapLeft = length;
            f->tapPeak = 0.0f;
            tapped.add (id);
        }
    return problems;
}

juce::var Session::closeTaps()
{
    auto* o = new juce::DynamicObject();
    for (auto& id : tapped)
        if (auto it = strips.find (id); it != strips.end() && it->second.fader != nullptr)
        {
            auto* f = static_cast<FaderNode*> (it->second.fader->getProcessor());
            f->tap.reset();  // flushes and closes the file
            f->tapLeft = 0;
            o->setProperty (juce::Identifier (id), f->tapPeak);
        }
    tapped.clear();
    return juce::var (o);
}

juce::var Session::peaks()
{
    auto* o = new juce::DynamicObject();
    for (auto& [id, s] : strips)
        if (s.fader != nullptr)
        {
            auto* f = static_cast<FaderNode*> (s.fader->getProcessor());
            juce::Array<juce::var> lr;
            lr.add (f->peak[0].exchange (0.0f));
            lr.add (f->peak[1].exchange (0.0f));
            if (const int bad = f->bad.exchange (0); bad > 0)
                lr.add (bad);
            o->setProperty (juce::Identifier (id), juce::var (lr));
        }
    return juce::var (o);
}

// ---- plugin windows ------------------------------------------------------------------------------------------

Session::Slot* Session::findRef (const juce::String& ref)
{
    for (auto& [key, s] : slots)
        if (s.ref == ref)
            return &s;
    return nullptr;
}

juce::String Session::openEditor (const juce::String& ref)
{
    for (auto& [key, s] : slots)
        if (s.ref == ref)
        {
            if (auto it = editors.find (key); it != editors.end())
            {
                it->second->toFront (true);
                return {};
            }
            auto* proc = s.node->getProcessor();
            auto* ed = proc->hasEditor() ? proc->createEditorIfNeeded() : nullptr;
            if (ed == nullptr)
                return ref + " has no window";
            const auto k = key;
            editors[key] = std::make_unique<EditorWindow> (proc->getName() + "  (" + ref + ")", ed, [this, k]
            {
                juce::MessageManager::callAsync ([this, k] { editors.erase (k); });
            });
            return {};
        }
    return "nothing loaded at " + ref;
}

void Session::closeEditors() { editors.clear(); }

juce::String Session::saveState (const juce::String& ref, const juce::File& to)
{
    auto* s = findRef (ref);
    if (s == nullptr)
        return "nothing loaded at " + ref;
    juce::MemoryBlock data;
    s->node->getProcessor()->getStateInformation (data);
    if (data.getSize() == 0)
        return ref + " has no state to save";
    to.getParentDirectory().createDirectory();
    return to.replaceWithData (data.getData(), data.getSize()) ? juce::String() : "could not write " + to.getFullPathName();
}

}  // namespace tt
