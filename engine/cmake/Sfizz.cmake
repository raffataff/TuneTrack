# SFZ instruments through the sfizz library (BSD-2), built from its source release when it is in
# third_party/sfizz/ (engine/README.md says where to get it). Without it the engine builds as before and SFZ
# tracks reach it as stems rendered by TuneTrack's own SFZ player.
set(TT_SFIZZ_DIR "${CMAKE_CURRENT_LIST_DIR}/../third_party/sfizz/sfizz-1.2.3" CACHE PATH
    "sfizz source release (the tarball with external/ in it, not a git checkout without submodules)")

# Every target sfizz defines, abseil's included, links the C runtime as a DLL like JUCE and the engine: sfizz
# asks for the static one, and the two cannot be mixed in one program.
function(tt_sfizz_dynamic_crt dir)
    get_property(targets DIRECTORY "${dir}" PROPERTY BUILDSYSTEM_TARGETS)
    foreach(t IN LISTS targets)
        get_target_property(type ${t} TYPE)
        if(NOT type STREQUAL "INTERFACE_LIBRARY")
            set_property(TARGET ${t} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
        endif()
    endforeach()
    get_property(subdirs DIRECTORY "${dir}" PROPERTY SUBDIRECTORIES)
    foreach(s IN LISTS subdirs)
        tt_sfizz_dynamic_crt("${s}")
    endforeach()
endfunction()

# Replace `old` with `new` in one of sfizz's files, once: `new` must contain `old`'s replacement marker so a
# second configure sees the patch is in. Stops when `old` is not there (another sfizz version).
function(tt_sfizz_patch rel old new)
    set(f "${TT_SFIZZ_DIR}/${rel}")
    file(READ "${f}" text)
    string(FIND "${text}" "TuneTrack: engine/cmake/Sfizz.cmake" done)
    if(done EQUAL -1)
        string(REPLACE "${old}" "${new}" patched "${text}")
        if(patched STREQUAL text)
            message(FATAL_ERROR "sfizz: cannot patch ${f} (a different sfizz version?)")
        endif()
        file(WRITE "${f}" "${patched}")
    endif()
endfunction()

# A function, so the settings below stay out of the engine's own scope.
function(tt_add_sfizz target)
    if(NOT EXISTS "${TT_SFIZZ_DIR}/CMakeLists.txt")
        message(STATUS "sfizz: no source at ${TT_SFIZZ_DIR}; SFZ instruments play from stems")
        return()
    endif()

    # sfizz keeps its random generator in a header as a static: one per source file, shared by every SFZ
    # instrument in the process. Two SFZ strips computed on two threads would race for it, and a track's round
    # robins and random layers would depend on what else rendered beside it. One per thread instead; the SFZ node
    # swaps its own in around every call (src/SfizzRandom.cpp, src/SfzNode.cpp).
    tt_sfizz_patch("src/sfizz/MathHelpers.h"
        "static fast_rand randomGenerator;"
        "extern thread_local fast_rand randomGenerator; // TuneTrack: engine/cmake/Sfizz.cmake")

    # Offline ("freewheeling") every block first waits for the samples its notes asked for. But a note's request
    # goes through a queue that a dispatcher thread empties when it gets to it, and the wait covered only what the
    # dispatcher had already handed on: a voice could run past its preloaded start into a sample not loaded yet
    # (silence or a glitch, a different one each render). The wait takes what is still queued as well. The
    # dispatcher pops under the same mutex, and the queue takes any number of consumers.
    tt_sfizz_patch("src/sfizz/FilePool.cpp"
        "void sfz::FilePool::waitForBackgroundLoading() noexcept\n{\n    std::lock_guard<std::mutex> guard { loadingJobsMutex };\n"
        "void sfz::FilePool::waitForBackgroundLoading() noexcept\n{\n    std::lock_guard<std::mutex> guard { loadingJobsMutex };\n\n    // TuneTrack: engine/cmake/Sfizz.cmake\n    QueuedFileData queued;\n    while (filesToLoad->try_pop(queued)) {\n        if (!queued.id.expired())\n            loadingJobs.push_back(\n                threadPool->enqueue([this](const QueuedFileData& data) { loadingJob(data); }, std::move(queued)));\n    }\n")

    set(CMAKE_CXX_STANDARD 17)  # what sfizz and its abseil are built with on MSVC
    set(ENABLE_LTO OFF)
    set(SFIZZ_SHARED OFF)
    set(SFIZZ_JACK OFF)
    set(SFIZZ_RENDER OFF)
    set(SFIZZ_BENCHMARKS OFF)
    set(SFIZZ_TESTS OFF)
    set(SFIZZ_DEMOS OFF)
    set(SFIZZ_DEVTOOLS OFF)
    set(SFIZZ_USE_SNDFILE OFF)
    set(SFIZZ_GIT_SUBMODULE_CHECK OFF)
    add_subdirectory("${TT_SFIZZ_DIR}" sfizz EXCLUDE_FROM_ALL)

    target_sources(sfizz_internal PRIVATE "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../src/SfizzRandom.cpp")
    tt_sfizz_dynamic_crt("${TT_SFIZZ_DIR}")
    target_link_libraries(${target} PRIVATE sfizz::sfizz)
    target_compile_definitions(${target} PRIVATE TT_HAVE_SFIZZ=1)
    message(STATUS "sfizz: ${TT_SFIZZ_DIR}")
endfunction()
