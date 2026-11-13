"""The studio's departments (seats): what each one is for, what it may change, and which tools it gets.

A seat is a standing responsibility; who fills it (an AI agent, an outside app, or the user) can change at any time.
`owns` names the kinds of change a seat is expected to make (see takes.CATEGORIES). Changes outside it are not
blocked - they are flagged when a take is compared, so whoever accepts it sees that a mixer rewrote a melody.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Kinds of change, as takes.py classifies them.
FORM, BRIEF, NOTES, SOUND, MIX, MASTER, TRACKS = "form", "brief", "notes", "sound", "mix", "master", "tracks"

# MCP tool groups (names as registered in mcp_server.py).
READ = {"status", "project_info", "project_list", "notes_read", "score", "analyze", "theory", "plugins_list",
        "patches_list", "synths_list", "sfz_list", "sfz_info", "plugin_params"}
HEAR = {"render", "listen", "audition"}
WRITE = {"write", "add_notes", "chords", "pattern", "drums", "cc", "notes_edit", "clip_edit", "arrange", "midi_import"}
SHAPE = {"project_set", "markers", "harmony"}
TRACK = {"track_add", "track_update", "track_remove", "track_duplicate"}
MIXING = {"track_update", "fx_add", "fx_update", "fx_remove", "automation"}
AUDIO = {"audio_import"}
LIBRARY = {"patch_import", "patch_import_folder", "patch_update", "plugins_scan"}
HISTORY = {"undo", "redo"}
# Every seat can see the board, talk, ask the user, and work in a take.
CREW = {"studio_status", "inbox", "message_send", "task_update", "ask_human", "take_new", "take_use", "take_diff",
        "take_sync", "take_submit"}
LEAD = {"task_add", "take_accept", "take_reject", "seat_assign", "midi_export"}
# Never given to a launched agent: these open windows or apps on the user's desktop, or switch the open project.
HUMAN_ONLY = {"play", "ui_open", "patch_capture", "project_new", "project_open"}


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    room: str
    purpose: str
    owns: frozenset = frozenset()
    tools: frozenset = frozenset()
    optional: bool = False  # not needed for every session (picture work, live recording)
    merges_into: str | None = None  # on small sessions this seat's work can be done by another
    tier: str = "mid"  # how demanding the seat is: top | high | mid | fast (a hint when choosing a model)
    hands: tuple = field(default=())  # seats this one normally hands work to


def _role(id, title, room, purpose, owns, tools, **kw) -> Role:
    return Role(id, title, room, purpose, frozenset(owns), frozenset(READ | CREW | set(tools)), **kw)


ROLES: dict[str, Role] = {r.id: r for r in [
    _role("producer", "Producer", "Control desk",
          "Turns the brief into a plan: form, tempo, key, who does what and in which order. Hands out tasks, "
          "listens to what comes back, accepts or returns takes, and decides when the cue is finished.",
          {FORM, BRIEF, TRACKS}, HEAR | SHAPE | TRACK | LEAD | HISTORY,
          tier="top", hands=("spotting", "composer", "rhythm", "sound", "mix", "review")),
    _role("spotting", "Spotting", "Screening room",
          "Reads the picture or the game: scenes, cuts, hit points, mood per section. Produces the cue sheet and "
          "the tempo map and markers that make the hits land on beats.",
          {FORM, BRIEF}, SHAPE, optional=True, tier="high", hands=("producer", "composer")),
    _role("composer", "Composer", "Writing room",
          "Themes, harmony and structure: the melody people remember, the chord track, how sections contrast.",
          {NOTES, FORM, TRACKS}, HEAR | WRITE | SHAPE | TRACK | HISTORY,
          tier="top", hands=("arranger", "rhythm", "review")),
    _role("arranger", "Arranger", "Scoring stage",
          "Decides who plays what: doublings, voicings, registers, countermelodies, articulations and dynamics "
          "curves, so a sketch becomes an ensemble that sounds performed.",
          {NOTES, TRACKS}, HEAR | WRITE | TRACK | HISTORY,
          merges_into="composer", tier="high", hands=("sound", "mix", "review")),
    _role("rhythm", "Rhythm", "Drum booth",
          "Drums, percussion and bass: the groove, fills, how tight or loose it feels (swing, humanize, quantize) "
          "for the genre.",
          {NOTES, TRACKS}, HEAR | WRITE | TRACK | HISTORY, tier="mid", hands=("mix", "review")),
    _role("sound", "Sound", "Instrument rack",
          "Chooses and shapes the instruments: which patch, sample library or synth plays each part, its "
          "articulation and dynamics controller, auditioned against the part it has to play.",
          {SOUND, TRACKS}, HEAR | {"track_update", "track_add"} | LIBRARY | HISTORY, tier="mid",
          hands=("arranger", "mix")),
    _role("tracking", "Tracking", "Live room",
          "Recorded audio - guitar, voice, anything through the interface: takes, comping, placing and cleaning "
          "the audio clips.",
          {NOTES, TRACKS}, HEAR | AUDIO | {"clip_edit", "arrange", "track_add", "track_update"} | HISTORY,
          optional=True, tier="mid", hands=("mix",)),
    _role("mix", "Mix", "Mixing console",
          "Balance, panning, EQ, dynamics, space and automation, then the master chain and loudness target.",
          {MIX, MASTER}, HEAR | MIXING | {"track_add", "project_set"} | HISTORY, tier="high", hands=("review",)),
    _role("review", "Review", "Listening room",
          "Independent ears. Listens to the render against the brief and any reference, checks the notes and the "
          "audio, and sends specific, located problems back to the seat that owns them. Changes nothing itself.",
          set(), HEAR | {"task_add"}, tier="top", hands=("producer", "composer", "rhythm", "mix")),
]}

ORDER = list(ROLES)
# Not a seat: the user. Also what an MCP client is when it hasn't taken a seat (the user's own assistant).
YOU = "you"
EVERYONE = "all"


def get(seat: str) -> Role:
    r = ROLES.get(str(seat).strip().lower())
    if not r:
        raise KeyError(f"No seat {seat!r}. Seats: {', '.join(ORDER)}")
    return r


def tools_for(seat: str | None) -> frozenset | None:
    """The MCP tools a launched agent in this seat gets; None = everything (the user, or an unseated client)."""
    if not seat or seat == YOU:
        return None
    return get(seat).tools


def catalog() -> list[dict]:
    return [{"id": r.id, "title": r.title, "room": r.room, "purpose": r.purpose, "owns": sorted(r.owns),
             "optional": r.optional, "merges_into": r.merges_into, "tier": r.tier, "hands_to": list(r.hands)}
            for r in ROLES.values()]
