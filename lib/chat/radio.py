"""Radio mode: keep playing, chosen from what this song sounds like.

A queue ends; the radio does not.  When the queue is about to run out the
engine asks this module for the next few tracks and appends them, so the
music keeps going without the listener having to say anything.

The pick is a blend, not a shuffle of the whole library:

- the **measured sound** of the seed track (`psycho.similar`) — a fast,
  aggressive song leads to fast, aggressive neighbours, whatever their tags
  say, because the profile is measured from the audio;
- the **genre and the old shelf** of the seed, so the drift stays plausible;
- a little of everything else, so the radio does not get stuck in one folder.

Nothing here writes to the library: the state is one small JSON file in
`library-work/app/chat/radio.json`, and every pick is re-checked against the
disk before it is queued, so a stale inventory row can never play.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from .catalog import AUDIO_EXT, fold, library_rel

#: the state file, relative to the library root
STATE = ("library-work", "app", "chat", "radio.json")

#: how many picks the engine appends when the queue gets low
BATCH = 6

#: how many of the newest picks are never repeated
HISTORY = 150

#: share of a batch that comes from the measured profile (the rest is genre
#: and a sprinkle of the whole library)
PROFILE_SHARE = 0.65

#: a track shorter than this is an interlude and a poor radio anchor
MIN_SECONDS = 20


def path(root: Path | str) -> Path:
    return Path(root).joinpath(*STATE)


def load(root: Path | str) -> dict:
    """The stored radio state; an empty, off state when there is none."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"on": False}
    if not isinstance(data, dict):
        return {"on": False}
    if not isinstance(data.get("played"), list):
        data["played"] = []
    data["on"] = bool(data.get("on"))
    return data


def save(root: Path | str, data: dict) -> dict:
    """Write the state atomically, trimmed to the history length."""
    target = path(root)
    payload = {
        "on": bool(data.get("on")),
        "seed": str(data.get("seed") or ""),
        "label": str(data.get("label") or ""),
        "genre": str(data.get("genre") or ""),
        "started": str(data.get("started") or ""),
        "played": [str(rel) for rel in (data.get("played") or [])
                   if str(rel).strip()][-HISTORY:],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        pass
    return payload


def active(root: Path | str) -> bool:
    """True while the radio is on (a dry run stores nothing, so it is off)."""
    return bool(load(root).get("on"))


def start(root: Path | str, seed: str, label: str = "",
          genre: str = "") -> dict:
    """Turn the radio on with a seed track."""
    data = load(root)
    data.update({
        "on": True,
        "seed": library_rel(seed),
        "label": str(label or ""),
        "genre": str(genre or ""),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    # the seed is already playing, so it counts as heard
    played = list(data.get("played") or [])
    if data["seed"] and data["seed"] not in played:
        played.append(data["seed"])
    data["played"] = played
    return save(root, data)


def stop(root: Path | str) -> bool:
    """Turn the radio off; the queue plays on.  True when it was on."""
    data = load(root)
    was = bool(data.get("on"))
    data["on"] = False
    save(root, data)
    return was


def remember(root: Path | str, rels) -> dict:
    """Add picks to the played history so they are not chosen again soon."""
    data = load(root)
    played = list(data.get("played") or [])
    for rel in rels:
        rel = library_rel(str(rel or ""))
        if rel and rel not in played:
            played.append(rel)
    data["played"] = played
    return save(root, data)


def info(root: Path | str, catalog=None) -> dict:
    """The small picture the window and `/api/state` show."""
    data = load(root)
    seed = str(data.get("seed") or "")
    track = catalog.track(seed) if (catalog is not None and seed) else None
    return {
        "on": bool(data.get("on")),
        "seed": seed,
        "seed_label": (track.label() if track is not None else ""),
        "label": str(data.get("label") or ""),
        "genre": str(data.get("genre") or ""),
        "started": str(data.get("started") or ""),
        "played": len(data.get("played") or []),
        "batch": BATCH,
    }


# --------------------------------------------------------------------------
# choosing the next tracks


def _weighted(items: list[str], weights: list[float], count: int,
              rng: random.Random) -> list[str]:
    """Sample without replacement, preferring the heavy items.

    The near neighbours are ordered by distance, so weight 1/(rank+1) turns
    that order into a gentle preference instead of a fixed top-N: two radio
    runs from the same song do not play the same six tracks.
    """
    pool = list(zip(items, weights))
    chosen: list[str] = []
    while pool and len(chosen) < count:
        total = sum(weight for _item, weight in pool)
        if total <= 0:
            break
        point = rng.random() * total
        running = 0.0
        pick = len(pool) - 1
        for index, (_item, weight) in enumerate(pool):
            running += weight
            if point <= running:
                pick = index
                break
        chosen.append(pool.pop(pick)[0])
    return chosen


def _pool(catalog) -> dict[str, dict]:
    """Every audio row whose file is really on disk, by relative path."""
    pool: dict[str, dict] = {}
    for row in catalog.rows:
        rel = library_rel(str(row.get("rel_path") or ""))
        if not rel or rel in pool:
            continue
        ext = str(row.get("ext") or Path(rel).suffix).lower()
        if ext not in AUDIO_EXT:
            continue
        duration = float(row.get("duration_s") or 0)
        if 0 < duration < MIN_SECONDS:
            continue                    # an interlude is a poor radio anchor
        try:
            if not catalog.path(rel).is_file():
                continue
        except OSError:
            continue
        pool[rel] = row
    return pool


def _genre_keys(row: dict) -> set[str]:
    keys: set[str] = set()
    for field in ("genre", "grouping"):
        for part in str(row.get(field) or "").replace(";", "/").split("/"):
            key = fold(part.strip())
            # "Soundtrack" alone is a shelf, not a style: it would group half
            # the library and make the radio drift everywhere
            if key and key not in {"soundtrack", "soundtracks", "various"}:
                keys.add(key)
    return keys


def candidates(catalog, psycho, seed_rel: str, count: int = BATCH,
               avoid=(), rng: random.Random | None = None) -> list[str]:
    """The next tracks for the radio, best first but not always the same.

    ``avoid`` holds the tracks that must not come back yet (the played
    history and whatever mpv already has queued).
    """
    rng = rng or random.Random()
    seed_rel = library_rel(str(seed_rel or ""))
    count = max(1, int(count or BATCH))
    blocked = {library_rel(str(rel)) for rel in (avoid or []) if str(rel)}
    blocked.add(seed_rel)

    pool = _pool(catalog)
    seed_row = pool.get(seed_rel)
    fresh = {rel: row for rel, row in pool.items()
             if rel != seed_rel and rel not in blocked}
    if not fresh:
        # everything has been played already: a radio that simply stops is
        # not a radio, so the history starts over (the seed still stays out,
        # because it is the one track the listener just heard)
        fresh = {rel: row for rel, row in pool.items() if rel != seed_rel}
    if not fresh:
        return []
    pool = fresh

    seed_genres = _genre_keys(seed_row) if seed_row else set()
    seed_artist = fold(str((seed_row or {}).get("albumartist")
                           or (seed_row or {}).get("artist") or ""))

    # 1. the measured sound: nearest profiles, gently weighted
    near: list[str] = []
    try:
        for distance_rank, item in enumerate(
                psycho.similar(seed_rel, limit=240) if seed_rel else []):
            rel = library_rel(str(item.get("rel") or ""))
            if rel in pool:
                near.append(rel)
    except Exception:                   # a missing or broken profile database
        near = []

    wanted_near = (math.ceil(count * PROFILE_SHARE) if near else 0)
    picks = _weighted(near, [1.0 / (rank + 1.0) for rank in range(len(near))],
                      wanted_near, rng)

    # 2. the same genre or old shelf, shuffled
    same = [rel for rel in pool
            if rel not in picks and seed_genres
            and _genre_keys(pool[rel]) & seed_genres]
    rng.shuffle(same)
    # a radio may stay with the artist for a moment, but it must not camp there
    same.sort(key=lambda rel: 1 if seed_artist and fold(str(
        pool[rel].get("albumartist") or pool[rel].get("artist") or ""
    )) == seed_artist else 0)
    picks += [rel for rel in same if rel not in picks][:count - len(picks)]

    # 3. everything else, so the radio can leave the folder
    rest = [rel for rel in pool if rel not in picks]
    rng.shuffle(rest)
    picks += rest[:count - len(picks)]
    return picks[:count]
