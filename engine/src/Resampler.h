// Arbitrary-ratio sample-rate conversion for a device that cannot run at the project's rate.
//
// A Kaiser-windowed sinc, tabulated once: the cut-off sits just under the lower of the two rates' Nyquist, so going
// down a rate nothing above the device's Nyquist folds back, and going up the images are removed. The caller keeps
// the input in a FIFO; `phase` is where in it the next output sample falls (in input samples), always at least
// `halfWidth()` samples in, so the kernel has the history it needs.
#pragma once

#include <juce_audio_basics/juce_audio_basics.h>

#include <cmath>
#include <vector>

namespace tt
{
class Resampler
{
public:
    // ratio: input samples per output sample.
    void setUp (double r)
    {
        ratio = r;
        cutoff = r > 1.0 ? 0.9 / r : 0.95;  // of the input's Nyquist
        width = crossings / cutoff;
        const int size = (int) std::ceil (width * resolution) + 2;
        table.assign ((size_t) size, 0.0f);
        const double i0b = bessel0 (beta);
        for (int i = 0; i < size; ++i)
        {
            const double x = (double) i / resolution;  // input samples from the centre
            if (x >= width)
                continue;
            const double t = cutoff * x;
            const double sinc = t == 0.0 ? 1.0 : std::sin (juce::MathConstants<double>::pi * t) / (juce::MathConstants<double>::pi * t);
            const double w = x / width;
            table[(size_t) i] = (float) (cutoff * sinc * bessel0 (beta * std::sqrt (1.0 - w * w)) / i0b);
        }
        taps = (int) std::ceil (width);
        phase = taps;
    }

    int halfWidth() const { return taps; }

    // Input samples needed in the FIFO to make n outputs from where it stands.
    int needed (int n) const { return (int) std::floor (phase + (n - 1) * ratio) + taps + 1; }

    // n outputs from `in` (one channel) starting at the current phase. Call once per channel with the same n, then
    // advance(n) once.
    void process (const float* in, float* out, int n) const
    {
        double c = phase;
        for (int j = 0; j < n; ++j, c += ratio)
        {
            const int centre = (int) std::floor (c);
            double sum = 0.0, weight = 0.0;
            for (int k = centre - taps + 1; k <= centre + taps; ++k)
            {
                const double at = std::abs (c - k) * resolution;
                const int i = (int) at;
                if (i + 1 >= (int) table.size())
                    continue;
                const double f = at - i;
                const double h = table[(size_t) i] + f * (table[(size_t) i + 1] - table[(size_t) i]);
                sum += h * in[k];
                weight += h;
            }
            out[j] = (float) (weight != 0.0 ? sum / weight : 0.0);  // unity gain at DC, whatever the fraction
        }
    }

    // Moves past n outputs; returns how many input samples the FIFO can drop from its front.
    int advance (int n)
    {
        phase += n * ratio;
        const int drop = juce::jmax (0, (int) std::floor (phase) - taps);
        phase -= drop;
        return drop;
    }

    void reset() { phase = taps; }

private:
    static double bessel0 (double x)
    {
        double sum = 1.0, term = 1.0;
        for (int k = 1; k < 40; ++k)
        {
            term *= (x / (2.0 * k)) * (x / (2.0 * k));
            sum += term;
        }
        return sum;
    }

    static constexpr double crossings = 24.0, beta = 8.6;
    static constexpr int resolution = 512;
    double ratio = 1.0, cutoff = 1.0, width = 1.0, phase = 0.0;
    int taps = 1;
    std::vector<float> table;
};

}  // namespace tt
