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
    return root.writeTo (out);
}

Session::~Session()
{
    editors.clear();
    graph.clear();
}

void Session::prepare (double sr, int block, bool off)
{
    sampleRate = sr;
    blockSize = block;
    offline = off;
    graph.releaseResources();
    graph.setPlayConfigDetails (0, 2, sr, block);
    graph.setNonRealtime (off);
    graph.prepareToPlay (sr, block);
    updateLatencies();
}

// ---- nodes ---------------------------------------------------------------------------------------------------

Session::Slot* Session::take (const juce::String& owner, const juce::String& identity, const juce::String& ref, const Maker& make)
{
    auto key = owner + "|" + identity;
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
    s.node = graph.addNode (std::move (proc), std::nullopt, Graph::UpdateKind::none);
    s.ref = ref;
    s.fresh = true;
    topologyChanged = true;
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
                if (note.isNotEmpty())
                    warnings.add (s.ref + ": " + note);
            }
            else
                p->setValue ((float) (double) kv.value);
        }
    s.applied = juce::JSON::toString (params, true);
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
        if (s->fresh && state.isNotEmpty())
        {
            s->deferred = params;
            ++freshStates;
            settleUntil = juce::Time::currentTimeMillis() + settleBaseMs() + settleEachMs() * freshStates;
        }
        else if (s->applied != juce::JSON::toString (params, true) && s->deferred.isVoid())
            applyPluginParams (*s, params);
        else if (! s->deferred.isVoid())
            s->deferred = params;
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
            if (auto* o = params.getDynamicObject())
                for (auto& kv : o->getProperties())
                    f->setParam (kv.name.toString(), (float) (double) kv.value);
    return s;
}

// ---- wiring --------------------------------------------------------------------------------------------------

void Session::link (Graph::Node* from, Graph::Node* to)
{
    if (from == nullptr || to == nullptr)
        return;
    const int outs = from == outNode.get() ? 0 : mainOuts (*from->getProcessor());
    const int ins = to == outNode.get() ? 2 : mainIns (*to->getProcessor());
    for (int c = 0; c < ins && outs > 0; ++c)  // a mono source feeds both sides; a mono input hears the left
        wires.push_back ({ { from->nodeID, juce::jmin (c, outs - 1) }, { to->nodeID, c } });
}

void Session::linkKey (Graph::Node* from, Graph::Node* fx)
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
    for (int c = 0; c < juce::jmin (2, count) && outs > 0; ++c)
        wires.push_back ({ { from->nodeID, juce::jmin (c, outs - 1) }, { fx->nodeID, base + c } });
}

juce::var Session::apply (const juce::var& spec)
{
    warnings.clear();
    next.clear();
    seen.clear();
    strips.clear();
    wires.clear();
    topologyChanged = false;
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

    struct KeyWant { juce::String track; Graph::Node::Ptr fx; juce::String ref; };
    std::vector<KeyWant> keys;
    struct Route { juce::String from, to; float db; bool send; int n; };
    std::vector<Route> routes;

    auto gainNode = [] (juce::String&) -> std::unique_ptr<juce::AudioProcessor> { return std::make_unique<GainNode>(); };
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
                        keys.push_back ({ key, s->node, ref });
                    prev = s->node;
                }
            }
        return prev;
    };

    // ---- strips ----
    if (auto* tracks = spec["tracks"].getArray())
        for (auto& t : *tracks)
        {
            const auto id = str (t, "id");
            Strip strip;
            Graph::Node::Ptr prev;
            if (str (t, "kind") == "bus")
            {
                if (auto* s = take (id, "head", id + "/in", gainNode))
                    prev = strip.head = s->node;
            }
            else if (const auto src = t["source"]; src.isObject())
            {
                const auto type = str (src, "type");
                if (auto* s = device (id, id + "/inst", src, true, false))
                {
                    prev = s->node;
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
                            if (m->applied != sig)
                            {
                                static_cast<MidiSourceNode*> (m->node->getProcessor())->seq.set (seq);
                                m->applied = sig;
                            }
                            wires.push_back ({ { m->node->nodeID, Graph::midiChannelIndex }, { s->node->nodeID, Graph::midiChannelIndex } });
                        }
                }
            }
            strip.tap = buildChain (id, t["fx"], prev);
            if (auto* f = take (id, "fader", id + "/fader", faderNode))
            {
                auto p = std::make_shared<FaderParams>();
                p->db = (float) num (t, "db", 0.0);
                p->pan = (float) num (t, "pan", 0.0);
                p->on = flag (t, "on", true);
                p->volAuto = points (t["vol_auto"]);
                p->panAuto = points (t["pan_auto"]);
                static_cast<FaderNode*> (f->node->getProcessor())->params.set (p);
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
        static_cast<FaderNode*> (f->node->getProcessor())->params.set (p);
        master.fader = f->node;
        link (master.tap.get(), f->node.get());
        link (f->node.get(), outNode.get());
    }

    // ---- routing, sends, sidechain keys ----
    for (auto& r : routes)
    {
        auto& from = strips[r.from];
        if (from.fader == nullptr)
            continue;
        if (r.to.isEmpty())
            continue;  // a bus the mixer leaves out (caught in a routing loop): it goes nowhere
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
            linkKey (it->second.tap.get(), k.fx.get());
    }
    strips["master"] = master;

    // ---- drop what is gone, rewire only if the wiring changed ----
    for (auto& [key, s] : slots)
    {
        editors.erase (key);
        graph.removeNode (s.node.get(), Graph::UpdateKind::none);
        topologyChanged = true;
    }
    slots = std::move (next);
    next.clear();

    auto order = [] (const Graph::Connection& a, const Graph::Connection& b) { return a < b; };
    std::sort (wires.begin(), wires.end(), order);
    if (topologyChanged || wires != wired)
    {
        for (auto& c : graph.getConnections())
            graph.removeConnection (c, Graph::UpdateKind::none);
        for (auto& c : wires)
            if (! graph.addConnection (c, Graph::UpdateKind::none))
                warnings.add ("engine: a connection was refused");
        trace ("rebuild");
        graph.rebuild();
        trace ("rebuilt");
        for (auto& c : graph.getConnections())
        {
            auto* from = graph.getNodeForId (c.source.nodeID);
            auto* to = graph.getNodeForId (c.destination.nodeID);
            trace ("  wire " + juce::String (c.source.nodeID.uid) + ":" + from->getProcessor()->getName() + "[" + juce::String (c.source.channelIndex)
                   + "] -> " + juce::String (c.destination.nodeID.uid) + ":" + to->getProcessor()->getName() + "[" + juce::String (c.destination.channelIndex) + "]");
        }
        wired = wires;
    }
    updateLatencies();

    auto* result = new juce::DynamicObject();
    result->setProperty ("warnings", juce::var (warnings));
    result->setProperty ("latency", totalLatency);
    result->setProperty ("nodes", (int) slots.size());
    result->setProperty ("settle_ms", settleRemainingMs());
    return juce::var (result);
}

void Session::updateLatencies()
{
    const auto conns = graph.getConnections();
    std::map<juce::uint32, int> memo;
    std::function<int (Graph::NodeID)> before = [&] (Graph::NodeID id) -> int
    {
        if (auto it = memo.find (id.uid); it != memo.end())
            return it->second;
        memo[id.uid] = 0;
        int most = 0;
        for (auto& c : conns)
            if (c.destination.nodeID == id && ! c.source.isMIDI())
                if (auto* src = graph.getNodeForId (c.source.nodeID))
                    most = juce::jmax (most, before (c.source.nodeID) + src->getProcessor()->getLatencySamples());
        return memo[id.uid] = most;
    };
    for (auto& [id, s] : strips)
        if (s.fader != nullptr)
            static_cast<FaderNode*> (s.fader->getProcessor())->latencyBefore = before (s.fader->nodeID);
    totalLatency = outNode != nullptr ? before (outNode->nodeID) : 0;
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
        }
    // a plugin may only report its real latency once its state has loaded
    graph.rebuild();
    updateLatencies();
    return warnings;
}

void Session::resetBuiltIns()
{
    for (auto& [key, s] : slots)
        if (auto* f = dynamic_cast<FaustNode*> (s.node->getProcessor()))
            f->reset();
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
