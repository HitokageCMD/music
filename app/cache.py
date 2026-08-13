"""Progressive download cache — the "stream while you download" engine.

A background thread pulls the whole track down to `<id>.part` sequentially and
never stops early, even if the listener skips away. Playback requests read from
that same growing file: when a reader catches up to the write head it parks on
an event until more bytes land. One download feeds every listener, the first
play starts within a second, and the file is still there afterwards.

Threaded, not async: the server is a ThreadingHTTPServer (one thread per
request), and pure blocking code is what runs cleanly inside an Android APK.
The wake mechanism is unchanged from the async version — swapping a threading
Event for an asyncio one is the whole difference — because the "replace the
event on every pulse" trick behaves identically either way.

Windows constraint: a file that's open for reading cannot be renamed. So the
`.part` -> `.<ext>` promotion is refcounted and only happens once the last
reader has let go.
"""

import logging
import threading
from pathlib import Path

import httpx

from . import db, ytdl
from .config import AUDIO, CHUNK

log = logging.getLogger("tunebox.cache")

_downloads: dict[str, "Download"] = {}
_ensure_lock = threading.Lock()


def _upstream_total(resp: httpx.Response) -> int:
    """Full asset size. Content-Length is only the range's length on a 206."""
    cr = resp.headers.get("content-range", "")
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    return int(resp.headers.get("content-length") or 0)


class Download:
    def __init__(self, vid: str) -> None:
        self.vid = vid
        self.part = AUDIO / f"{vid}.part"
        self.total = 0
        self.written = 0
        self.done = False
        self.error: Exception | None = None
        self.ext = "m4a"
        self.mime = "audio/mp4"
        self.meta: dict = {}
        self.readers = 0
        self.ready = threading.Event()
        self._progress = threading.Event()
        self._finalized = False
        self._lock = threading.Lock()  # guards readers + finalize across threads
        self.thread: threading.Thread | None = None

    def _pulse(self) -> None:
        """Wake every reader parked on the current event, then arm a fresh one.

        Readers must grab `self._progress` *before* they test `self.written`,
        otherwise a write landing in between would leave them waiting on an
        event that has already fired.
        """
        ev = self._progress
        self._progress = threading.Event()
        ev.set()

    def _run(self) -> None:
        try:
            source, page_url = db.get_source(self.vid)
            meta = ytdl.resolve(self.vid, source, page_url)
            self.meta = meta
            self.ext = meta["ext"]
            self.mime = meta["mime"]
            self.part = AUDIO / f"{self.vid}.part"
            # Guarantee a row exists before mark_cached (an UPDATE) runs, or a
            # track downloaded outside the search flow would never surface in
            # the library.
            db.ensure_track(meta)

            # `Range: bytes=0-` asks for the exact same bytes as a plain GET, but
            # googlevideo treats the two completely differently. These URLs carry no
            # ratebypass flag, so a rangeless GET gets throttled to about playback
            # speed; any explicit range sidesteps it. Measured on one track:
            # 31 KB/s without the header, 12 MB/s with it. Do not remove.
            req_headers = {**meta["headers"], "Range": "bytes=0-"}
            timeout = httpx.Timeout(30.0, read=60.0)
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                with client.stream("GET", meta["url"], headers=req_headers) as resp:
                    resp.raise_for_status()
                    self.total = _upstream_total(resp) or meta["size_hint"]
                    if not self.total:
                        raise RuntimeError("upstream did not report a content length")
                    with open(self.part, "wb") as f:
                        self.ready.set()
                        for chunk in resp.iter_bytes(CHUNK):
                            f.write(chunk)
                            self.written += len(chunk)
                            self._pulse()

            if self.written != self.total:
                raise RuntimeError(f"short read: {self.written}/{self.total} bytes")
            log.info("downloaded %s — %.1f MB", self.vid, self.total / 1e6)
        except Exception as e:
            self.error = e
            log.warning("download failed for %s: %s", self.vid, e)
            self.part.unlink(missing_ok=True)
        finally:
            self.done = True
            self.ready.set()
            self._pulse()
            if self.error:
                with _ensure_lock:
                    _downloads.pop(self.vid, None)
            else:
                self._finalize()

    def _finalize(self) -> None:
        """Promote .part -> .<ext>. No-op until the download is done and idle."""
        with self._lock:
            if self._finalized or self.error or not self.done or self.readers > 0:
                return
            self._finalized = True
        final = AUDIO / f"{self.vid}.{self.ext}"
        try:
            self.part.replace(final)
        except OSError as e:
            # Almost certainly a straggling Windows file handle. Leave it for
            # whichever reader unwinds next.
            with self._lock:
                self._finalized = False
            log.debug("finalize deferred for %s: %s", self.vid, e)
            return
        db.mark_cached(self.vid, self.total, self.ext)
        with _ensure_lock:
            _downloads.pop(self.vid, None)

    def reader(self, start: int, end: int):
        """Yield bytes [start, end], blocking when the write head is behind us."""
        with self._lock:
            self.readers += 1
        try:
            pos = start
            with open(self.part, "rb") as f:
                f.seek(start)
                while pos <= end:
                    ev = self._progress  # capture before testing state — see _pulse
                    if self.error:
                        raise self.error
                    if self.written > pos:
                        want = min(CHUNK, end - pos + 1, self.written - pos)
                        data = f.read(want)
                        if data:
                            pos += len(data)
                            yield data
                            continue
                    if self.done:
                        break
                    ev.wait(timeout=30)  # timeout only re-checks state; harmless
        finally:
            with self._lock:
                self.readers -= 1
            self._finalize()


def ensure(vid: str) -> Download:
    """Get the in-flight download for `vid`, starting one if needed."""
    with _ensure_lock:
        dl = _downloads.get(vid)
        if dl is None:
            dl = Download(vid)
            _downloads[vid] = dl
            dl.thread = threading.Thread(target=dl._run, name=f"dl-{vid}", daemon=True)
            dl.thread.start()
    dl.ready.wait()
    if dl.error:
        raise dl.error
    return dl


def cached_path(vid: str) -> Path | None:
    """Local file for `vid`, or None. Self-heals if the file vanished."""
    t = db.get_track(vid)
    if not t or not t["cached"]:
        return None
    p = AUDIO / f"{vid}.{t['ext'] or 'm4a'}"
    if p.exists():
        return p
    db.mark_uncached(vid)
    return None


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """Parse a Range header. None means "no range"; ValueError means unsatisfiable."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    lo, _, hi = spec.partition("-")
    if lo:
        start = int(lo)
        end = int(hi) if hi else total - 1
    elif hi:
        start = max(0, total - int(hi))
        end = total - 1
    else:
        raise ValueError("malformed range")
    end = min(end, total - 1)
    if start > end or start >= total:
        raise ValueError("range not satisfiable")
    return start, end


def sweep() -> None:
    """Startup hygiene: drop half-written files and re-sync the DB with disk."""
    for p in AUDIO.glob("*.part"):
        p.unlink(missing_ok=True)
        log.info("removed stale partial %s", p.name)
    for vid in db.cached_ids():
        if cached_path(vid) is None:
            log.info("re-flagged %s as uncached (file missing)", vid)


def evict(vid: str) -> bool:
    p = cached_path(vid)
    if p:
        p.unlink(missing_ok=True)
    db.mark_uncached(vid)
    return p is not None
