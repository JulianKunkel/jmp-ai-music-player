"""Ease the end of a track out and the next one in — mpv's soft ending.

mpv has no crossfade feature, but an audio filter can take the last seconds of
every track down and the first fraction of the next one up.  mpv already plays
consecutive tracks without a gap, so the two touch and the join reads as a soft
dip instead of a cut.  It is a playback filter exactly like the loudness
levelling: no audio file is ever rewritten, and switching the setting off
restores the untouched sound.

The filter is built here (pure, no mpv) and applied by
`Engine._apply_audio_filter`, which also folds in the levelling gain, because
mpv has a single `af` property and the two must not overwrite each other.
"""

from __future__ import annotations

#: what the app fades with when the listener does not choose a length
DEFAULT_SECONDS = 3.0
#: a longer fade than this would swallow whole songs, not their ending
MAX_SECONDS = 10.0
#: the incoming track eases in over this; a fade, not a slow swell
FADE_IN_SECONDS = 0.6
#: shorter than this is an interlude, and a fade would swallow it whole
MIN_TRACK_SECONDS = 2.5

#: stored values that mean "off" (a profile written by hand or an older build)
_FALSE = {"0", "off", "false", "no", "aus", "ausgeschaltet", "disabled",
          "deactivated"}


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def enabled(profile) -> bool:
    """Whether the fading is switched on (`fade_songs`, default on)."""
    value = profile.setting("fade_songs", True)
    if isinstance(value, str):
        return value.strip().casefold() not in _FALSE
    return bool(value)


def seconds_from(profile) -> float:
    """The fade length in seconds; 0 when the fading is off."""
    if not enabled(profile):
        return 0.0
    value = _number(profile.setting("fade_seconds", DEFAULT_SECONDS))
    return max(0.0, min(MAX_SECONDS, value))


def length(seconds: float, duration: float) -> float:
    """The fade really applied: never more than a third of the track."""
    fade = max(0.0, min(MAX_SECONDS, _number(seconds)))
    if _number(duration) < MIN_TRACK_SECONDS:
        return 0.0
    return min(fade, max(0.5, _number(duration) / 3.0))


def start(seconds: float, duration: float) -> float:
    """Where the out-fade begins, or 0 when the track gets none."""
    fade = length(seconds, duration)
    return _number(duration) - fade if fade else 0.0


def inside(seconds: float, duration: float, position: float) -> bool:
    """Is the playhead already in the fade window?

    Adding the filter there would drop the level at once, like a cut, so the
    engine waits for the next track instead.
    """
    begin = start(seconds, duration)
    return bool(begin) and _number(position) >= begin


def filter_for(seconds: float, duration: float = 0.0) -> str:
    """The mpv `af` part that fades one track, or "" when it should not.

    Two `lavfi` entries instead of one graph with a comma, so mpv's option
    parser never has to unescape anything.  `afade` is an ffmpeg filter mpv
    reaches through `lavfi`; that is the only route to a fade in mpv.
    """
    if _number(seconds) <= 0:
        return ""
    parts = []
    duration = _number(duration)
    fade_in = (min(FADE_IN_SECONDS, duration / 4.0)
               if duration > 0 else FADE_IN_SECONDS)
    if fade_in >= 0.1:
        parts.append(f"lavfi=[afade=t=in:st=0:d={fade_in:.3f}]")
    fade = length(seconds, duration)
    if fade:
        parts.append(f"lavfi=[afade=t=out:st={start(seconds, duration):.3f}"
                     f":d={fade:.3f}]")
    return ",".join(parts)


def chain(*parts: str) -> str:
    """mpv's `af` value from the parts that are in use (fade, gain)."""
    return ",".join(part for part in parts if part)


def describe(profile) -> dict:
    """What the settings panel and a chat answer report."""
    return {"enabled": enabled(profile),
            "seconds": round(seconds_from(profile), 1),
            "default": DEFAULT_SECONDS,
            "max": MAX_SECONDS}
