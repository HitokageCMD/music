"""Personalized recommendations from the listener's own history.

No account, no external profile. The seeds are the tracks this person actually
liked and played; the candidates are the radio stations YouTube Music builds
around each seed. The ranking signal is co-occurrence: a song that turns up in
several of your seeds' stations is a stronger pick than a one-off, so it floats
to the top. Everything is built from data already in the local SQLite db.
"""

import logging
import threading
import time
from collections import defaultdict

from . import db, ytdl

log = logging.getLogger("tunebox.recommend")

SEEDS = 8
PER_STATION = 25
TTL = 600  # 10 min — building a batch fans out 8 radio calls (~5s); don't redo it every visit

_cache: dict = {"sig": None, "at": 0.0, "recs": []}
_lock = threading.Lock()


def recommend(limit: int = 30) -> list[dict]:
    seeds = db.seed_tracks(SEEDS)
    if not seeds:
        return []

    # Reuse a recent batch as long as the seeds are the same. The signature is the
    # seed ids, so liking/playing something new invalidates it immediately;
    # otherwise it just expires after TTL.
    sig = tuple(s["id"] for s in seeds)
    with _lock:
        if _cache["sig"] == sig and time.time() - _cache["at"] < TTL and _cache["recs"]:
            return _cache["recs"][:limit]

    recs = _build(seeds)
    with _lock:
        _cache.update(sig=sig, at=time.time(), recs=recs)
    return recs[:limit]


def _build(seeds: list[dict]) -> list[dict]:
    # Never recommend something they already have, already like, disliked, or seeded from.
    exclude = db.cached_ids() | db.liked_ids() | db.disliked_ids() | {s["id"] for s in seeds}

    def station(seed: dict):
        return seed, ytdl.radio(seed["id"], PER_STATION)

    # One station per seed, fanned out on the shared pool.
    stations = list(ytdl._pool.map(station, seeds))

    scores: dict[str, float] = defaultdict(float)
    meta: dict[str, dict] = {}
    because: dict[str, str] = {}

    for rank, (seed, tracks) in enumerate(stations):
        # Earlier seeds (more liked / more played) carry more weight.
        seed_weight = 1.0 / (1 + rank)
        for pos, t in enumerate(tracks):
            vid = t.get("id")
            if not vid or vid in exclude:
                continue
            # Higher up a station = a touch more relevant.
            scores[vid] += seed_weight * (1.0 / (1 + pos * 0.1))
            if vid not in meta:
                meta[vid] = t
                because[vid] = seed.get("title") or ""

    # Build the full batch (capped); the caller slices to its requested limit.
    ranked = sorted(scores, key=lambda v: scores[v], reverse=True)[:50]
    out = []
    for vid in ranked:
        t = dict(meta[vid])
        t["because"] = because[vid]  # "because you played X"
        out.append(t)
    log.info("recommended %d tracks from %d seeds", len(out), len(seeds))
    return out
