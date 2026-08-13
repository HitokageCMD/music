"""HTTP server on the standard library — no FastAPI, no uvicorn, no pydantic.

Those three are gone for one reason: pydantic's core is compiled Rust and cannot
be packaged into an Android APK (Chaquopy) or built cleanly on a phone. The whole
runtime is now pure Python — http.server + yt-dlp + ytmusicapi + httpx — so the
same code runs on the PC and inside the phone app.

ThreadingHTTPServer gives one thread per request, which is exactly what the
threaded cache in cache.py expects: a reader can block waiting for bytes without
stalling anything else. The HTTP API is byte-for-byte the same as the old
FastAPI version, so the web frontend and smoke.py are unchanged.
"""

import json
import logging
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from . import cache, db, lyrics, recommend, ytdl
from .config import ART, WEB
from .ytdl import MIME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tunebox")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
}

_VID = r"([A-Za-z0-9_-]{6,20})"


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")


def _annotate(results: list[dict]) -> list[dict]:
    """Remember the metadata, then flag what we already hold locally."""
    db.upsert_tracks(results)
    have, loved, nope = db.cached_ids(), db.liked_ids(), db.disliked_ids()
    for r in results:
        r["cached"] = r["id"] in have
        r["liked"] = r["id"] in loved
        r["disliked"] = r["id"] in nope
    return results


class Handler(BaseHTTPRequestHandler):
    server_version = "tunebox"
    protocol_version = "HTTP/1.1"

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, body=b"", ctype="application/octet-stream", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200, headers=None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers)

    def _err(self, status, msg):
        # Same envelope FastAPI used, so the client's `.detail` reader still works.
        self._json({"detail": msg}, status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _jsonbody(self):
        try:
            return json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return {}

    # --- dispatch -----------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            self._dispatch(method, path, q)
        except BrokenPipeError:
            pass  # client hung up mid-stream (skipped a track) — normal
        except Exception as e:
            log.exception("unhandled error on %s %s", method, path)
            try:
                self._err(500, str(e))
            except Exception:
                pass

    def _dispatch(self, method, path, q):
        if method == "GET" and path == "/api/search":
            return self._search(q)
        if method == "GET" and path == "/api/search/lyrics":
            return self._search_lyrics(q)
        if method == "GET" and (m := re.fullmatch(rf"/api/lyrics/{_VID}", path)):
            return self._json(lyrics.fetch(m[1], force=_truthy(q.get("force"))))
        if method == "GET" and (m := re.fullmatch(rf"/api/related/{_VID}", path)):
            rel = ytdl.related(m[1], min(int(q.get("limit", 25)), 50))
            nope = db.disliked_ids()
            return self._json(_annotate([t for t in rel if t.get("id") not in nope]))
        if method == "GET" and path == "/api/recommend":
            return self._recommend(q)
        if method == "POST" and path == "/api/import":
            return self._import()
        if method == "POST" and path == "/api/playlist":  # create an empty user playlist
            body = self._jsonbody()
            name = (body.get("name") or "").strip()
            if not name:
                return self._err(400, "name required")
            return self._json({"id": db.create_playlist(name, "user", ""), "name": name})
        if method == "POST" and (m := re.fullmatch(r"/api/playlist/(\d+)/add", path)):
            track = self._jsonbody().get("track") or {}
            if not track.get("id"):
                return self._err(400, "track required")
            db.upsert_tracks([track])  # make sure the metadata exists
            db.add_track_to_playlist(int(m[1]), track["id"])
            return self._json({"ok": True})
        if method == "DELETE" and (m := re.fullmatch(rf"/api/playlist/(\d+)/track/{_VID}", path)):
            db.remove_playlist_track(int(m[1]), m[2])
            return self._json({"ok": True})
        if method == "POST" and (m := re.fullmatch(r"/api/playlist/(\d+)/rename", path)):
            name = (self._jsonbody().get("name") or "").strip()
            if not name:
                return self._err(400, "name required")
            db.rename_playlist(int(m[1]), name)
            return self._json({"ok": True, "name": name})
        if method == "GET" and path == "/api/playlists":
            return self._json(db.list_playlists())
        if method == "GET" and (m := re.fullmatch(r"/api/playlist/(\d+)", path)):
            tracks = db.playlist_tracks(int(m[1]))
            have, loved = db.cached_ids(), db.liked_ids()
            for t in tracks:
                t["cached"] = t["id"] in have
                t["liked"] = t["id"] in loved
            return self._json(tracks)
        if method == "DELETE" and (m := re.fullmatch(r"/api/playlist/(\d+)", path)):
            db.delete_playlist(int(m[1]))
            return self._json({"ok": True})
        if method == "POST" and path == "/api/track":
            return self._track()
        if method == "GET" and (m := re.fullmatch(rf"/api/stream/{_VID}", path)):
            return self._stream(m[1])
        if method == "GET" and (m := re.fullmatch(rf"/api/progress/{_VID}", path)):
            return self._progress(m[1])
        if method == "GET" and (m := re.fullmatch(rf"/api/art/{_VID}", path)):
            return self._art(m[1])
        if method == "GET" and path == "/api/library":
            return self._json(db.list_tracks(only_cached=True, liked=_truthy(q.get("liked"))))
        if method == "POST" and (m := re.fullmatch(rf"/api/like/{_VID}", path)):
            liked = _truthy(q.get("liked", "true"))
            db.set_liked(m[1], liked)
            return self._json({"ok": True, "liked": liked})
        if method == "POST" and (m := re.fullmatch(rf"/api/dislike/{_VID}", path)):
            disliked = _truthy(q.get("disliked", "true"))
            db.set_disliked(m[1], disliked)
            return self._json({"ok": True, "disliked": disliked})
        if method == "POST" and path == "/api/follow":
            name = (q.get("name") or "").strip()
            if not name:
                return self._err(400, "name required")
            if _truthy(q.get("on", "true")):
                db.follow_artist(name)
            else:
                db.unfollow_artist(name)
            return self._json({"ok": True, "following": db.is_following(name)})
        if method == "GET" and path == "/api/artists":
            return self._json(db.followed_artists())
        if method == "DELETE" and (m := re.fullmatch(rf"/api/library/{_VID}", path)):
            return self._json({"ok": True, "removed": cache.evict(m[1])})
        if method == "GET" and path == "/api/stats":
            return self._json(db.stats())

        # static / shell
        if method == "GET" and path == "/":
            return self._file(WEB / "index.html", "text/html; charset=utf-8", "no-cache")
        if method == "GET" and path == "/manifest.json":
            return self._file(WEB / "manifest.json", "application/manifest+json")
        if method == "GET" and path == "/sw.js":
            # Must be root-scoped or the worker can't control the whole origin.
            return self._file(WEB / "sw.js", "application/javascript", "no-cache")
        if method == "GET" and path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        self._err(404, "not found")

    # --- API handlers -------------------------------------------------------

    def _search(self, q):
        query = (q.get("q") or "").strip()
        if not query:
            return self._json([])
        try:
            results = ytdl.search(query, min(int(q.get("limit", 25)), 50))
        except Exception as e:
            log.warning("search failed for %r: %s", query, e)
            return self._err(502, f"search failed: {e}")
        self._json(_annotate(results))

    def _search_lyrics(self, q):
        query = (q.get("q") or "").strip()
        if len(query) < 2:
            return self._json([])
        limit = min(int(q.get("limit", 8)), 10)
        local = db.search_cached_lyrics(query, limit)
        for r in local:
            r["local"] = True
        try:
            remote = lyrics.search_by_lyrics(query, limit)
        except Exception as e:
            log.warning("lyric search failed for %r: %s", query, e)
            remote = []
        seen = {r["id"] for r in local}
        self._json(_annotate(local + [r for r in remote if r["id"] not in seen]))

    def _import(self):
        try:
            body = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._err(400, "bad json")
        url = (body.get("url") or "").strip()
        # A pasted share is often "【标题-哔哩哔哩】 https://b23.tv/xxx" — extract the URL.
        m = re.search(r"https?://[^\s]+", url)
        if m:
            url = m.group(0).rstrip(")]，。、")
        if not url:
            return self._err(400, "url required")
        try:
            coll = ytdl.list_collection(url)
        except Exception as e:
            log.warning("import failed for %s: %s", url, e)
            return self._err(502, f"导入失败: {e}")
        entries = coll["entries"]
        if not entries:
            return self._err(404, "没找到可导入的内容（可能被地域限制或链接不对）")
        db.upsert_tracks(entries)  # metadata incl. source + page_url
        pid = db.create_playlist(coll["name"], coll["source"], url)
        db.add_playlist_tracks(pid, [e["id"] for e in entries])
        log.info("imported %d tracks from %s as playlist %d", len(entries), coll["source"], pid)
        self._json({"id": pid, "name": coll["name"], "count": len(entries), "source": coll["source"]})

    def _recommend(self, q):
        try:
            recs = recommend.recommend(min(int(q.get("limit", 30)), 50))
        except Exception as e:
            log.warning("recommend failed: %s", e)
            return self._err(502, f"recommend failed: {e}")
        # _annotate would drop the `because` field's companions? No — it only adds
        # cached/liked and upserts metadata, so recommended covers get remembered.
        annotated = _annotate(recs)
        return self._json(annotated)

    def _track(self):
        try:
            meta = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._err(400, "bad json")
        if not meta.get("id"):
            return self._err(400, "id required")
        db.upsert_track(meta)
        self._json({"ok": True})

    def _progress(self, vid):
        if cache.cached_path(vid):
            return self._json({"state": "cached", "pct": 100})
        dl = cache._downloads.get(vid)
        if not dl or not dl.total:
            return self._json({"state": "idle", "pct": 0})
        self._json({
            "state": "downloading",
            "pct": round(dl.written / dl.total * 100, 1),
            "written": dl.written,
            "total": dl.total,
        })

    def _art(self, vid):
        p = ART / f"{vid}.jpg"
        if p.exists():
            return self._file(p, "image/jpeg", "public, max-age=31536000")
        t = db.get_track(vid)
        url = (t or {}).get("thumb") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        try:
            with httpx.Client(follow_redirects=True, timeout=15) as c:
                r = c.get(url)
            r.raise_for_status()
        except Exception:
            return self._err(404, "no artwork")
        if "i.ytimg.com" in url:
            # 16:9 video frame, not cover art. Serve it so the row isn't blank but
            # never write it down — a later search supplies the real square cover,
            # and a file on disk would outrank it forever.
            return self._send(200, r.content, "image/jpeg", {"Cache-Control": "no-store"})
        tmp = ART / f"{vid}.jpg.tmp"
        tmp.write_bytes(r.content)
        tmp.replace(p)
        self._send(200, r.content, "image/jpeg", {"Cache-Control": "public, max-age=31536000"})

    def _stream(self, vid):
        local = cache.cached_path(vid)
        if local:
            if not self.headers.get("Range"):
                db.bump_play(vid)
            return self._file_ranged(local, MIME.get(local.suffix.lstrip("."), "audio/mp4"))

        try:
            dl = cache.ensure(vid)
        except Exception as e:
            return self._err(502, f"could not open audio stream: {e}")

        total = dl.total
        try:
            rng = cache.parse_range(self.headers.get("Range"), total)
        except ValueError:
            return self._send(416, b"", "audio/mp4", {"Content-Range": f"bytes */{total}"})

        if rng is None:
            start, end, status = 0, total - 1, 200
        else:
            start, end = rng
            status = 206
        if start == 0:
            db.bump_play(vid)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "no-store",  # a half-finished body must not be cached
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        self._stream_out(status, dl.mime, headers, dl.reader(start, end))

    # --- response helpers ---------------------------------------------------

    def _stream_out(self, status, ctype, headers, chunks):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        for chunk in chunks:
            self.wfile.write(chunk)

    def _file(self, path, ctype, cache_control=None):
        if not path.exists():
            return self._err(404, "not found")
        data = path.read_bytes()
        headers = {"Cache-Control": cache_control} if cache_control else None
        self._send(200, data, ctype, headers)

    def _file_ranged(self, path, ctype):
        """Serve a complete on-disk file with Range support — the cached-audio path."""
        size = path.stat().st_size
        try:
            rng = cache.parse_range(self.headers.get("Range"), size)
        except ValueError:
            return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})

        base = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=31536000"}
        if rng is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end, status = rng[0], rng[1], 206
            base["Content-Range"] = f"bytes {start}-{end}/{size}"
        base["Content-Length"] = str(end - start + 1)

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in base.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(cache.CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _static(self, rel):
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._err(404, "not found")
        ext = target.suffix.lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # Code must revalidate every load or an update never reaches the browser —
        # a long max-age here even satisfies the service worker's own network fetch
        # from HTTP cache, so a stale app.js survives SW updates too. Images are
        # content-addressed enough to cache hard.
        immutable = ext in (".png", ".jpg", ".jpeg", ".svg", ".ico")
        self._file(target, ctype, "public, max-age=31536000" if immutable else "no-cache")


def serve(host="0.0.0.0", port=8730):
    db.conn()
    cache.sweep()
    s = db.stats()
    log.info("library: %d tracks, %.1f MB", s["tracks"], s["bytes"] / 1e6)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log.info("tunebox on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        httpd.shutdown()
