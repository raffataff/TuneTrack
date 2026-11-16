// Compiled into sfizz itself (cmake/Sfizz.cmake), with sfizz's include paths and flags.
//
// The patched MathHelpers.h declares sfizz's random generator once per thread. This defines it, and lets an SFZ
// node keep a generator of its own: before each call into its synth the node swaps its state in, afterwards back
// out (SfzNode.cpp). A track's round robins and random layers then depend only on its own notes, whichever thread
// computes it and whatever else renders beside it.
#include "MathHelpers.h"

#include <cstdint>
#include <cstring>
#include <type_traits>

// (MathHelpers.h keeps these in the global namespace, not in sfz)
namespace Random
{
thread_local fast_rand randomGenerator;
}  // namespace Random

namespace tt_sfizz
{
// Puts `state` into the calling thread's generator and returns what was there.
std::uint32_t swapRandom (std::uint32_t state)
{
    static_assert (sizeof (fast_rand) == sizeof (std::uint32_t) && std::is_trivially_copyable<fast_rand>::value,
                   "fast_rand is no longer one 32-bit word: SfizzRandom.cpp needs another look");
    std::uint32_t old;
    std::memcpy (&old, &Random::randomGenerator, sizeof old);
    Random::randomGenerator.seed (state);
    return old;
}
}  // namespace tt_sfizz
