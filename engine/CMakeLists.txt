cmake_minimum_required(VERSION 3.22)
project(TuneTrackEngine VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# JUCE is used from its source checkout (AGPLv3). Override with -DJUCE_SOURCE_DIR=... or the JUCE_DIR env var.
if(DEFINED ENV{JUCE_DIR} AND NOT JUCE_SOURCE_DIR)
    set(JUCE_SOURCE_DIR "$ENV{JUCE_DIR}")
endif()
set(JUCE_SOURCE_DIR "C:/Program Files (x86)/JUCE" CACHE PATH "JUCE source checkout")
add_subdirectory("${JUCE_SOURCE_DIR}" juce EXCLUDE_FROM_ALL)

juce_add_console_app(tunetrack-engine
    PRODUCT_NAME "tunetrack-engine"
    NEEDS_CURL FALSE
    NEEDS_WEB_BROWSER FALSE)

target_sources(tunetrack-engine PRIVATE
    src/Main.cpp
    src/Session.cpp
    src/FaustNodes.cpp
    src/Params.cpp)

target_include_directories(tunetrack-engine PRIVATE src gen third_party/faust)

target_compile_definitions(tunetrack-engine PRIVATE
    JUCE_WEB_BROWSER=0
    JUCE_USE_CURL=0
    JUCE_PLUGINHOST_VST3=1
    JUCE_ASIO=1
    JUCE_WASAPI=1
    JUCE_DIRECTSOUND=0
    JUCE_DISPLAY_SPLASH_SCREEN=0
    JUCE_REPORT_APP_USAGE=0
    _CRT_SECURE_NO_WARNINGS=1
    NOMINMAX=1)

if(MSVC)
    # /Zi + /DEBUG: a .pdb beside the exe, so the crash report can name functions (the code is still optimised)
    target_compile_options(tunetrack-engine PRIVATE /bigobj /MP /Zi)
    target_link_options(tunetrack-engine PRIVATE /DEBUG /OPT:REF /OPT:ICF)
    target_link_libraries(tunetrack-engine PRIVATE dbghelp)
endif()

target_link_libraries(tunetrack-engine
    PRIVATE
        juce::juce_audio_utils
    PUBLIC
        juce::juce_recommended_config_flags)
