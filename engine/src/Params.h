#pragma once

#include <juce_audio_utils/juce_audio_utils.h>

namespace tt
{
// Index of the parameter a key names: its number, its exact name, or a piece of text only one name contains.
// -1 with `error` set when there is none.
int findParam (juce::AudioProcessor&, const juce::String& key, juce::String& error);

// The 0..1 value at which the parameter shows `target`. Sets `note` when the plugin can only get near it, and
// returns -1 with `error` set when it cannot be matched at all. Leaves the parameter at the returned value.
float valueForText (juce::AudioProcessorParameter&, const juce::String& target, juce::String& note, juce::String& error);

}  // namespace tt
