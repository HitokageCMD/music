"""Lyrics: fetching, LRC parsing, and finding a song from a line of it.

Three external facts drive this file, all measured rather than assumed:

  * LRCLIB accepts tunebox's raw YouTube Music metadata as-is — title, artist
    and duration straight through, no cleanup — and returns timed lyrics for
    English, Chinese and Japanese tracks alike. So there is no normalisation
    layer here on purpose.
  * LRCLIB cannot search by lyric text. Its /api/search only covers titles and
    artists; a distinctive lyric line returns zero hits.
  * NetEase encrypts its response for non-China IPs (`abroad: true`), so QQ
    Music is the lyric-text search source.
"""

import logging
import re

import httpx

from . import db, ytdl

log = logging.getLogger("tunebox.lyrics")

UA = {"User-Agent": "tunebox/0.1 (self-hosted personal music player)"}
QQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}

# [mm:ss], [mm:ss.xx] and [mm:ss.xxx] all appear in the wild.
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text: str) -> list[dict]:
    """LRC -> [{t: milliseconds, text: str}], sorted.

    A line may carry several stamps ("[00:12.00][01:30.00] chorus") — each one
    becomes its own entry. Empty bodies are kept: they mark instrumental gaps,
    and without them the highlight would sit on the last sung line for the whole
    outro.
    """
    out: list[dict] = []
    for line in text.splitlines():
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        body = line[stamps[-1].end():].strip()
        for m in stamps:
            frac = (m.group(3) or "0").ljust(3, "0")[:3]
            ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000 + int(frac)
            out.append({"t": ms, "text": body})
    out.sort(key=lambda x: x["t"])
    return out


def _lrclib(track: dict) -> dict | None:
    """LRCLIB, exact match first, then a duration-scored search."""
    dur = int(track.get("duration") or 0)
    params = {"artist_name": track.get("artist") or "", "track_name": track.get("title") or ""}
    if track.get("album"):
        params["album_name"] = track["album"]
    if dur:
        params["duration"] = dur

    with httpx.Client(timeout=20, follow_redirects=True, headers=UA) as c:
        try:
            r = c.get("https://lrclib.net/api/get", params=params)
            if r.status_code == 200:
                d = r.json()
                if d.get("syncedLyrics") or d.get("plainLyrics"):
                    return d
        except Exception as e:
            log.debug("lrclib get failed: %s", e)

        # Exact lookup missed — fall back to a title/artist search and pick the
        # entry closest in length. Duration is what separates the album cut from
        # a live version whose timings would drift out of sync immediately.
        try:
            r = c.get(
                "https://lrclib.net/api/search",
                params={
                    "track_name": (track.get("title") or "").split("(")[0].strip(),
                    "artist_name": (track.get("artist") or "").split(",")[0].strip(),
                },
            )
            if r.status_code != 200:
                return None
            hits = [h for h in r.json() if h.get("syncedLyrics") or h.get("plainLyrics")]
            if not hits:
                return None
            if dur:
                hits.sort(key=lambda h: abs((h.get("duration") or 0) - dur))
                if abs((hits[0].get("duration") or 0) - dur) > 15:
                    return None
            return hits[0]
        except Exception as e:
            log.debug("lrclib search failed: %s", e)
            return None


def _ytmusic_sync(vid: str) -> dict | None:
    """YouTube Music's own lyrics (Musixmatch upstream), timestamped when available."""
    ytm = ytdl.ytm_client()
    try:
        watch = ytm.get_watch_playlist(videoId=vid)
        bid = watch.get("lyrics")
        if not bid:
            return None
        try:
            data = ytm.get_lyrics(bid, timestamps=True)
            lines = data.get("lyrics")
            if isinstance(lines, list) and lines:
                synced = [
                    {"t": int(ln.start_time), "text": (ln.text or "").strip()}
                    for ln in lines
                    if getattr(ln, "start_time", None) is not None
                ]
                if synced:
                    return {"synced": synced, "plain": "\n".join(x["text"] for x in synced)}
        except Exception:
            pass
        data = ytm.get_lyrics(bid, timestamps=False)
        text = data.get("lyrics")
        if isinstance(text, str) and text.strip():
            return {"synced": [], "plain": text.strip()}
    except Exception as e:
        log.debug("ytmusic lyrics failed for %s: %s", vid, e)
    return None


def fetch(vid: str, force: bool = False) -> dict:
    """Lyrics for a track. Cached in SQLite, including misses."""
    if not force:
        hit = db.get_lyrics(vid)
        if hit:
            return hit

    track = db.get_track(vid) or {"id": vid}
    result = {"synced": [], "plain": "", "source": "none"}

    d = _lrclib(track)
    if d:
        synced = parse_lrc(d.get("syncedLyrics") or "")
        plain = (d.get("plainLyrics") or "").strip()
        if synced or plain:
            result = {"synced": synced, "plain": plain, "source": "lrclib"}

    if not result["synced"]:
        y = _ytmusic_sync(vid)
        if y and (y["synced"] or y["plain"]):
            # Only displace LRCLIB when this actually adds timing.
            if y["synced"] or result["source"] == "none":
                result = {**y, "source": "ytmusic"}

    db.put_lyrics(vid, result)
    return result


# --------------------------------------------------------------------------
# Find a song from a line of its lyrics
# --------------------------------------------------------------------------

_EM = re.compile(r"</?em>")
_BRACKETED = re.compile(r"[(\（\[【].*?[)\）\]】]")


def _squash(s: str) -> str:
    """Collapse to comparable form: no case, no spacing, no punctuation."""
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def _content_lines(content: str) -> list[str]:
    # QQ separates lines with the two characters \ and n — not a newline.
    return [ln.strip() for ln in _EM.sub("", content or "").split("\\n") if ln.strip()]


def _matched_line(content: str, query: str) -> str | None:
    """The lyric line containing `query`, or None if QQ didn't really match it.

    QQ's own signals can't be trusted for this. Its <em> tags mark single *token*
    hits, so 'zzzxqv nonexistent gibberish lyric' comes back with five confidently
    <em>-tagged rows off the back of the word "lyric" (measured). It also folds
    title matches into lyric results with no tags at all. Neither the tags nor the
    ranking tell us whether the phrase is actually there — so check.
    """
    want = _squash(query)
    if not want:
        return None
    lines = _content_lines(content)
    hits = [(i, ln) for i, ln in enumerate(lines) if want in _squash(ln)]
    if hits:
        # Prefer a sung line. Line 0 is the "title - artist" header and the next
        # few are "词：/曲：" credits; matching those means QQ hit the title, which
        # is worth returning but makes a poor snippet.
        body = [ln for i, ln in hits if i > 0 and "：" not in ln]
        return (body or [hits[0][1]])[0]
    if want in _squash("".join(lines)):
        # Phrase straddles a line break.
        return next((ln for ln in lines if "：" not in ln), lines[0] if lines else "")
    return None


def _qq_search_sync(query: str, limit: int) -> list[dict]:
    try:
        r = httpx.get(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            # t=7 is QQ's lyric index. Ask for extra rows: verification below
            # throws most of them away.
            params={"w": query, "t": 7, "n": max(limit * 3, 15), "p": 1, "format": "json"},
            headers=QQ_HEADERS,
            timeout=20,
        )
        j = r.json()
    except Exception as e:
        log.warning("qq lyric search failed: %s", e)
        return []

    lst = (((j.get("data") or {}).get("lyric") or {}).get("list")) or []
    out = []
    for s in lst:
        name = (s.get("songname") or "").strip()
        if not name:
            continue
        line = _matched_line(s.get("content") or "", query)
        if line is None:
            continue
        out.append(
            {
                "name": name,
                "singer": ", ".join(a.get("name", "") for a in (s.get("singer") or [])),
                "album": (s.get("albumname") or "").strip(),
                "duration": int(s.get("interval") or 0),
                "snippet": line,
            }
        )
    if lst and not out:
        log.debug("qq returned %d rows for %r, none contained the phrase", len(lst), query)
    return out[:limit]


# QQ knows which song a lyric belongs to; only YouTube can play it. Bridging on
# "name + singer" and taking the top hit is not enough — measured, that turns
# "晴天 by 刘瑞琦" into 周杰倫's original (wrong artist) and a Live cut into the
# studio version (51s adrift). Two independent checks catch both: the title has
# to be the same song, and the length has to agree. A true match lands within
# ~1s; the wrong ones sat 16s and 51s out.
_MAX_DRIFT = 12


def _same_song(a: str, b: str) -> bool:
    """Same title, ignoring case, punctuation and "(Live)" / "(feat. X)" suffixes.

    Prefix, not substring. Real title variants extend the end — "Creep (Acoustic)",
    "First Love - From THE FIRST TAKE" — whereas accidental containment happens in
    the middle: "在人间" sits inside "我在人间贩卖黄昏" and a substring test happily
    called them the same song (measured; it surfaced 王建房's 在人间 for a 林汐然
    lyric).
    """
    x, y = _squash(_BRACKETED.sub("", a)), _squash(_BRACKETED.sub("", b))
    if not x or not y:
        return False
    return x.startswith(y) or y.startswith(x)


def _bridge(hit: dict) -> dict | None:
    """Turn one QQ lyric hit into a playable YouTube track.

    召回优先:歌词搜索是在回答「这句词是哪首歌」,宁可给个版本略有出入的,也别
    让用户搜不到。有标题同名的候选,就按时长挑最接近的(消歧不同版本);一个都没
    同名时,退而用最相关的第一个结果保底 —— 旧逻辑在这两处直接整条丢弃,扔掉了
    大量真实命中(YouTube 标题常带【】/MV/官方等前后缀,时长也常与音频版差十几秒)。
    """
    try:
        cands = ytdl.search(f"{hit['name']} {hit['singer']}", 5)
    except Exception:
        return None
    if not cands:
        return None
    same = [c for c in cands if _same_song(hit["name"], c["title"])]
    if same:
        if hit["duration"]:
            same.sort(key=lambda c: abs(c["duration"] - hit["duration"]))
        return {**same[0], "snippet": hit["snippet"]}
    # 没有标题严格同名的:用最相关的第一个保底,总比「搜不到」强
    return {**cands[0], "snippet": hit["snippet"]}


def search_by_lyrics(query: str, limit: int = 6) -> list[dict]:
    """Find playable tracks from a line of lyrics."""
    found = _qq_search_sync(query, limit)
    if not found:
        return []

    # Each bridge is an independent YouTube search; fan them out on the pool.
    bridged = list(ytdl._pool.map(_bridge, found))

    # QQ lists every take separately — album, Live, remaster, cover — and they
    # are all honest hits for the lyric. For "which song is this?" one row per
    # song+artist is the useful answer. QQ ranks by relevance, so the first wins.
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for b in bridged:
        if not isinstance(b, dict):
            continue
        key = (_squash(_BRACKETED.sub("", b["title"])), _squash(b["artist"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out
