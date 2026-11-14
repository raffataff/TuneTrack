// Setting hosted-plugin parameters by number or by the text the plugin shows ("-12 dB", "400 Hz", "On").
// A port of tunetrack/worker.py (_param_index, _value_for_text), so both engines land on the same values.
#include "Params.h"

#include <cmath>
#include <optional>
#include <regex>

namespace tt
{
namespace
{
std::optional<double> num (const juce::String& text)
{
    static const std::regex number (R"([-+]?\d+(?:\.\d+)?)");
    static const std::regex kilo (R"(\d\s*k(hz)?\b)");
    const std::string s = text.replace (",", ".").toStdString();
    std::smatch m;
    if (! std::regex_search (s, m, number))
        return std::nullopt;
    double v = std::stod (m.str (0));
    const std::string low = text.toLowerCase().toStdString();
    if (std::regex_search (low, kilo))
        v *= 1000.0;
    return v;
}

bool showsFreqUnit (const juce::String& text)
{
    static const std::regex unit (R"(hz\b|\d\s*k\b)");
    return std::regex_search (text.toLowerCase().toStdString(), unit);
}

bool freqName (const juce::String& name)
{
    static const std::regex r ("freq|hz|pass|filter|cut|hpf|lpf|crossover|xover", std::regex::icase);
    return std::regex_search (name.toStdString(), r);
}

struct Sample { float v; juce::String text; std::optional<double> n; };
}  // namespace

int findParam (juce::AudioProcessor& p, const juce::String& key, juce::String& error)
{
    const auto k = key.trim().toLowerCase();
    auto& params = p.getParameters();
    if (k.isNotEmpty() && k.containsOnly ("0123456789"))
    {
        const int i = k.getIntValue();
        if (i < params.size())
            return i;
        error = "No parameter " + key;
        return -1;
    }
    juce::Array<int> subs;
    for (int i = 0; i < params.size(); ++i)
    {
        const auto name = params[i]->getName (512).trim().toLowerCase();
        if (name == k)
            return i;
        if (name.contains (k))
            subs.add (i);
    }
    if (subs.size() == 1)
        return subs[0];
    // Names as pedalboard spells them ("delay_bpm" for "Delay BPM"): settings made for a Waves-style shell plugin
    auto plain = [] (const juce::String& text)
    {
        juce::String out;
        for (auto c : text.toLowerCase())
            if (juce::CharacterFunctions::isLetterOrDigit (c)) out << c;
            else if (out.isNotEmpty() && ! out.endsWithChar ('_')) out << '_';
        return out.trimCharactersAtEnd ("_");
    };
    const auto want = plain (k);
    juce::Array<int> loose;
    for (int i = 0; i < params.size(); ++i)
    {
        const auto name = plain (params[i]->getName (512));
        if (name == want)
            return i;
        if (name.contains (want))
            loose.add (i);
    }
    if (subs.isEmpty() && loose.size() == 1)
        return loose[0];
    error = "No parameter '" + key + "'";
    if (! subs.isEmpty())
    {
        juce::StringArray names;
        for (int i = 0; i < juce::jmin (8, subs.size()); ++i)
            names.add (params[subs[i]]->getName (512));
        error << "; candidates: " << names.joinIntoString (", ");
    }
    return -1;
}

float valueForText (juce::AudioProcessorParameter& param, const juce::String& target, juce::String& note, juce::String& error)
{
    const auto name = param.getName (512);
    auto textAt = [&] (double v)
    {
        param.setValue ((float) v);
        return param.getText ((float) v, 512).trim();
    };
    const auto want = target.trim().toLowerCase();
    auto wantN = num (want);
    std::vector<Sample> samples;
    for (int i = 0; i <= 200; ++i)
    {
        const float v = (float) i / 200.0f;
        const auto txt = textAt (v);
        if (txt.toLowerCase() == want)
            return v;
        samples.push_back ({ v, txt, num (txt) });
    }
    if (! wantN)
    {
        for (auto& s : samples)
            if (s.text.toLowerCase().contains (want) || (s.text.isNotEmpty() && want.contains (s.text.toLowerCase())))
                return s.v;
        error = "No value of " + name + " shows as '" + target + "'";
        return -1.0f;
    }
    const Sample* lo = nullptr;
    const Sample* hi = nullptr;
    for (auto& s : samples)
        if (s.n)
        {
            if (lo == nullptr || *s.n < *lo->n) lo = &s;
            if (hi == nullptr || *s.n > *hi->n) hi = &s;
        }
    if (lo == nullptr)
    {
        error = name + " shows no numbers: set it by one of its texts";
        return -1.0f;
    }
    double w = *wantN;
    bool inRange = *lo->n <= w && w <= *hi->n;
    if (! inRange && *lo->n <= w / 1000.0 && w / 1000.0 <= *hi->n && (freqName (name) || want.contains ("hz")))
    {
        bool anyUnit = false;
        for (auto& s : samples)
            if (s.n && showsFreqUnit (s.text))
                anyUnit = true;
        if (! anyUnit)
        {
            w /= 1000.0;
            inRange = true;
        }
    }
    double best = 0.0, bestErr = 1e300;
    for (auto& s : samples)
        if (s.n && std::abs (*s.n - w) < bestErr)
        {
            bestErr = std::abs (*s.n - w);
            best = s.v;
        }
    double val = best;
    if (inRange)
    {
        double a = juce::jmax (0.0, best - 0.005), b = juce::jmin (1.0, best + 0.005);
        for (int i = 0; i < 18; ++i)
        {
            const double mid = (a + b) / 2.0;
            const auto n = num (textAt (mid));
            const auto nLo = num (textAt (a));
            if (! n || ! nLo)
                break;
            if ((*nLo <= w && w <= *n) || (*n <= w && w <= *nLo))
                b = mid;
            else
                a = mid;
        }
        auto err = [&] (double v)
        {
            const auto n = num (textAt (v));
            return n ? std::abs (*n - w) : 1e300;
        };
        const double cands[3] = { a, (a + b) / 2.0, b };
        double e = 1e301;
        for (double c : cands)
        {
            const double ce = err (c);
            if (ce < e) { e = ce; val = c; }
        }
    }
    const auto got = textAt (val);
    const auto gotN = num (got);
    if (! (gotN && std::abs (*gotN - w) <= juce::jmax (0.02 * std::abs (w), 0.05)))
        note = name + " can't show '" + target + "' (it goes from '" + lo->text + "' to '" + hi->text + "'); set to '" + got + "'";
    return (float) val;
}

}  // namespace tt
