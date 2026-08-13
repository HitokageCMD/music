"""yt-dlp / ytmusicapi wrappers.

Search goes through ytmusicapi because it returns real song metadata
(artist, album, duration, cover art) instead of yt-dlp's generic video results.
yt-dlp is used only to resolve a video id into a direct audio stream URL.

Everything here is plain blocking code. The server runs one thread per request
(ThreadingHTTPServer), so a slow yt-dlp call blocks only its own request. `_pool`
stays around purely for fan-out — e.g. lyric search bridging many hits at once.

Pure Python by design: yt-dlp, ytmusicapi and httpx have no compiled
dependencies, which is what lets the whole backend run inside an Android APK
(Chaquopy) with no ffmpeg and no Rust.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from yt_dlp import YoutubeDL
from ytmusicapi import YTMusic

log = logging.getLogger("tunebox.ytdl")

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ytdl")
_ytm: YTMusic | None = None


def ytm_client() -> YTMusic:
    """Lazily built, then shared. Constructing one costs a network round trip."""
    global _ytm
    if _ytm is None:
        _ytm = YTMusic()
    return _ytm

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "no_color": True,
    # No ffmpeg post-processing on purpose: we want the raw stream, unmodified,
    # so the byte count yt-dlp reports matches what we actually cache. That is
    # what makes HTTP Range (seeking) work while the file is still downloading.
    "postprocessors": [],
}

MIME = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "mp3": "audio/mpeg",
}


def _thumb(thumbs: list | None, size: int = 544) -> str:
    """Pick the largest thumbnail, then ask Google for a bigger render of it."""
    if not thumbs:
        return ""
    best = max(thumbs, key=lambda t: t.get("width", 0) or 0)
    url = best.get("url", "")
    # ytmusicapi hands back tiny =w60-h60 crops; the host will happily serve
    # any size we ask for, so rewrite the dimensions upward.
    if "=w" in url and "-h" in url:
        head, _, tail = url.partition("=w")
        rest = tail.split("-", 2)
        if len(rest) >= 2:
            suffix = rest[2] if len(rest) > 2 else "l90-rj"
            url = f"{head}=w{size}-h{size}-{suffix}"
    return url


def _search_sync(query: str, limit: int) -> list[dict]:
    out = []
    for r in ytm_client().search(query, filter="songs", limit=limit):
        vid = r.get("videoId")
        if not vid:
            continue
        out.append(
            {
                "id": vid,
                "title": r.get("title") or "",
                "artist": ", ".join(a["name"] for a in (r.get("artists") or []) if a.get("name")),
                "album": (r.get("album") or {}).get("name") or "",
                "duration": int(r.get("duration_seconds") or 0),
                "thumb": _thumb(r.get("thumbnails")),
            }
        )
    return out


def search(query: str, limit: int = 25) -> list[dict]:
    return _search_sync(query, limit)


def _pick_audio(info: dict) -> dict:
    """Choose the best audio-only format, preferring m4a for browser compat."""
    fmts = [
        f
        for f in info.get("formats", [])
        if f.get("url")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]
    if not fmts:
        raise RuntimeError("no audio-only format available")
    # m4a/AAC beats a higher-bitrate opus stream: Safari and iOS cannot play opus,
    # and this has to work on a phone.
    fmts.sort(key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), reverse=True)
    return fmts[0]


def page_url_for(vid: str, source: str = "yt", page_url: str = "") -> str:
    """The webpage URL yt-dlp should extract, per source."""
    if page_url:
        return page_url
    if source == "bili":
        return f"https://www.bilibili.com/video/{vid}"
    return f"https://www.youtube.com/watch?v={vid}"


def _resolve_sync(vid: str, source: str, page_url: str) -> dict:
    url = page_url_for(vid, source, page_url)
    with YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):  # a multi-part page resolved to a list — take the first part
        info = info["entries"][0]
    f = _pick_audio(info)
    ext = f.get("ext") or "m4a"
    # YouTube: the square-ish mqdefault. Others: whatever thumbnail yt-dlp found.
    thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if source == "yt" else (info.get("thumbnail") or "")
    return {
        "id": vid,
        "url": f["url"],
        "ext": ext,
        "mime": MIME.get(ext, "application/octet-stream"),
        "abr": f.get("abr") or 0,
        # Upstreams reject requests without the exact headers yt-dlp negotiated —
        # googlevideo's throttle keys off them, and Bilibili needs the Referer.
        "headers": f.get("http_headers") or {},
        "size_hint": f.get("filesize") or f.get("filesize_approx") or 0,
        "title": info.get("track") or info.get("title") or vid,
        "artist": info.get("artist") or info.get("uploader") or "",
        "album": info.get("album") or "",
        "duration": int(info.get("duration") or 0),
        "thumb": thumb,
    }


def resolve(vid: str, source: str = "yt", page_url: str = "") -> dict:
    return _resolve_sync(vid, source, page_url)


def _source_of(url: str) -> str:
    return "bili" if "bilibili.com" in url or "b23.tv" in url else "yt"


_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")
_PART_PREFIX = re.compile(r"^\s*\d+[.\s、)_-]+")
_BILI_API_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}


def _split_part_title(part: str) -> tuple[str, str]:
    """"001. 郁可唯 - 去有风的地方" -> ("郁可唯", "去有风的地方"). Best-effort."""
    t = _PART_PREFIX.sub("", (part or "").strip())
    for sep in (" - ", " – ", " — ", "-", "—"):
        if sep in t:
            a, _, b = t.partition(sep)
            a, b = a.strip(), b.strip()
            if a and b:
                return a, b
    return "", t or part


def _bili_collection(bvid: str, limit: int) -> dict | None:
    """All parts of a Bilibili 分P video via the public view API — one fast call.

    yt-dlp's extract_flat returns empty placeholder entries for bilibili multi-part
    videos (measured: 10 rows, all id=None). The view API returns every part with
    real titles and durations instantly, so we use it directly and let each part
    play through the normal resolve path (page_url carries ?p=N).
    """
    try:
        r = httpx.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                      headers=_BILI_API_H, timeout=20)
        j = r.json()
    except Exception as e:
        log.warning("bili view api failed for %s: %s", bvid, e)
        return None
    if j.get("code") != 0:
        return None
    data = j.get("data") or {}
    pages = data.get("pages") or []
    if not pages:
        return None
    pic = data.get("pic") or ""
    entries = []
    for p in pages[:limit]:
        page = p.get("page")
        artist, title = _split_part_title(p.get("part") or "")
        entries.append({
            "id": f"{bvid}_p{page}",
            "title": title or (data.get("title") or bvid),
            "artist": artist,
            "album": "",
            "duration": int(p.get("duration") or 0),
            "thumb": pic,
            "source": "bili",
            "page_url": f"https://www.bilibili.com/video/{bvid}?p={page}",
        })
    return {"name": data.get("title") or "导入的清单", "source": "bili", "entries": entries}


def list_collection(url: str, limit: int = 500) -> dict:
    """List a YouTube/Bilibili playlist or collection URL.

    Returns {name, source, entries:[{id, title, artist, duration, thumb, source, page_url}]}.
    No stream resolution here — entries play on demand through the normal path.
    """
    source = _source_of(url)

    # Bilibili 分P videos: the view API lists every part fast and correctly,
    # where yt-dlp's flat extractor returns empty rows.
    if source == "bili":
        m = _BVID.search(url)
        if not m and ("b23.tv" in url or "/video/" not in url):
            # A b23.tv share link has no BV id in it — follow the redirect to the
            # real page URL, which does.
            try:
                r = httpx.get(url, headers=_BILI_API_H, follow_redirects=True, timeout=15)
                m = _BVID.search(str(r.url))
            except Exception as e:
                log.warning("b23 resolve failed for %s: %s", url, e)
        if m:
            coll = _bili_collection(m.group(1), limit)
            if coll and coll["entries"]:
                return coll
        # fall through to yt-dlp for non-BV bilibili URLs (space collections, favlists)

    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "skip_download": True, "playlistend": limit,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    raw = info.get("entries")
    if raw is None:  # a single video, not a collection — import it as a one-track list
        raw = [info]

    entries = []
    for e in raw:
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        purl = e.get("url") or e.get("webpage_url") or ""
        if purl and not purl.startswith("http"):
            purl = ""  # extract_flat sometimes puts a bare id in 'url'
        entries.append({
            "id": vid,
            "title": e.get("title") or vid,
            "artist": e.get("uploader") or e.get("channel") or e.get("artist") or "",
            "album": "",
            "duration": int(e.get("duration") or 0),
            "thumb": (e.get("thumbnails") or [{}])[-1].get("url", "") if source != "yt"
                     else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "source": source,
            "page_url": purl,
        })
    return {"name": info.get("title") or "导入的清单", "source": source, "entries": entries}


def _watch_track(r: dict, seed_vid: str) -> dict | None:
    """Map one get_watch_playlist track to our shape, or None to skip."""
    rid = r.get("videoId")
    if not rid or rid == seed_vid:
        return None
    return {
        "id": rid,
        "title": r.get("title") or "",
        "artist": ", ".join(a["name"] for a in (r.get("artists") or []) if a.get("name")),
        "album": (r.get("album") or {}).get("name") or "",
        "duration": int(r.get("length_seconds") or 0),
        "thumb": _thumb(r.get("thumbnail")),
    }


def _watch(vid: str, limit: int, radio: bool) -> list[dict]:
    try:
        watch = ytm_client().get_watch_playlist(videoId=vid, radio=radio, limit=limit + 1)
    except Exception as e:
        log.warning("watch lookup failed for %s: %s", vid, e)
        return []
    out = [t for r in watch.get("tracks", []) if (t := _watch_track(r, vid))]
    return out[:limit]


def related(vid: str, limit: int = 25) -> list[dict]:
    """自动续歌用的相似歌。用 radio=True 的电台(围绕种子的同风格歌),而不是
    radio=False 的 up-next 队列 —— 后者会「演进」,放着放着就漂到不相关的老歌
    (用户反馈:听薛之谦突然跳老歌)。电台更贴近当前这首的风格/年代。"""
    return _watch(vid, limit, radio=True)


def radio(vid: str, limit: int = 25) -> list[dict]:
    """A full station around one song — broader than related; the rec engine's fuel."""
    return _watch(vid, limit, radio=True)
