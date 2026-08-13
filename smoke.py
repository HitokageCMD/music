"""End-to-end smoke test. Start the server, then: uv run python smoke.py

Exercises the parts that are easy to break and hard to notice: Range replies
while a track is still downloading, mid-download seeks, and the .part -> .m4a
promotion that Windows file locking likes to interfere with.
"""

import asyncio
import sys
import time

import httpx

BASE = "http://localhost:8730"
QUERY = "daft punk instant crush"

ok_count = 0
fail_count = 0


def jpeg_size(b: bytes) -> tuple[int, int]:
    """Width/height straight out of the JPEG SOF marker — avoids a Pillow dep."""
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(b[i + 7 : i + 9], "big"), int.from_bytes(b[i + 5 : i + 7], "big")
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(b[i + 2 : i + 4], "big")
    return 0, 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        fail_count += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
    return cond


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as c:
        print("\n[1] search")
        t0 = time.perf_counter()
        r = await c.get("/api/search", params={"q": QUERY, "limit": 5})
        check("200", r.status_code == 200, f"{(time.perf_counter()-t0)*1000:.0f} ms")
        hits = r.json()
        if not check("returned songs", bool(hits), f"{len(hits)} hits"):
            return 1
        top = hits[0]
        print(f"        -> {top['title']} — {top['artist']}  [{top['id']}]")
        for k in ("id", "title", "artist", "duration", "thumb"):
            check(f"field {k!r}", k in top and top[k] not in (None, ""))

        vid = top["id"]
        await c.delete(f"/api/library/{vid}")  # start from a cold cache

        print("\n[2] cold stream — first byte latency")
        t0 = time.perf_counter()
        req = c.build_request("GET", f"/api/stream/{vid}", headers={"Range": "bytes=0-65535"})
        resp = await c.send(req, stream=True)
        first_chunk = b""
        async for chunk in resp.aiter_bytes():
            first_chunk = chunk
            break
        ttfb = (time.perf_counter() - t0) * 1000
        await resp.aclose()

        check("206 Partial Content", resp.status_code == 206, str(resp.status_code))
        cr = resp.headers.get("content-range", "")
        check("Content-Range present", cr.startswith("bytes 0-65535/"), cr)
        check("Content-Length is 64 KiB", resp.headers.get("content-length") == "65536",
              resp.headers.get("content-length", "?"))
        check("audio content type", resp.headers.get("content-type", "").startswith("audio/"),
              resp.headers.get("content-type", "?"))
        check("first byte under 8 s", ttfb < 8000, f"{ttfb:.0f} ms")
        check("looks like MP4/M4A", first_chunk[4:8] == b"ftyp", repr(first_chunk[4:8]))

        total = int(cr.split("/")[-1]) if "/" in cr else 0
        print(f"        -> track is {total/1e6:.1f} MB")

        print("\n[3] mid-download seek (the part that blocks until bytes arrive)")
        far = int(total * 0.85)
        t0 = time.perf_counter()
        req = c.build_request("GET", f"/api/stream/{vid}", headers={"Range": f"bytes={far}-{far+16383}"})
        resp = await c.send(req, stream=True)
        got = b""
        async for chunk in resp.aiter_bytes():
            got += chunk
        await resp.aclose()
        check("206 for a deep range", resp.status_code == 206, str(resp.status_code))
        check("delivered 16 KiB", len(got) == 16384, f"{len(got)} bytes in {(time.perf_counter()-t0)*1000:.0f} ms")

        print("\n[4] unsatisfiable range")
        r = await c.get(f"/api/stream/{vid}", headers={"Range": f"bytes={total+999}-"})
        check("416", r.status_code == 416, str(r.status_code))

        print("\n[5] background download finishes on its own")
        deadline = time.time() + 150
        pct = 0
        while time.time() < deadline:
            p = (await c.get(f"/api/progress/{vid}")).json()
            if p["state"] == "cached":
                break
            pct = p.get("pct", 0)
            await asyncio.sleep(1)
        p = (await c.get(f"/api/progress/{vid}")).json()
        check("reached cached state", p["state"] == "cached", f"last saw {pct}%")

        print("\n[6] warm path serves from disk")
        t0 = time.perf_counter()
        r = await c.get(f"/api/stream/{vid}", headers={"Range": "bytes=0-65535"})
        warm = (time.perf_counter() - t0) * 1000
        check("206 from cache", r.status_code == 206, f"{warm:.0f} ms")
        check("warm is faster than cold", warm < ttfb, f"{warm:.0f} ms vs {ttfb:.0f} ms cold")

        print("\n[7] library + artwork")
        lib = (await c.get("/api/library")).json()
        check("track is in the library", any(t["id"] == vid for t in lib), f"{len(lib)} tracks")
        row = next((t for t in lib if t["id"] == vid), {})
        check("size recorded", row.get("size", 0) == total, f"{row.get('size')} vs {total}")
        check("artist survived", bool(row.get("artist")), row.get("artist", ""))
        r = await c.get(f"/api/art/{vid}")
        check("artwork 200", r.status_code == 200, f"{len(r.content)} bytes")
        check("artwork is a JPEG", r.content[:2] == b"\xff\xd8")
        w, h = jpeg_size(r.content)
        check("artwork is square album art", w == h and w >= 400, f"{w}x{h}")

        # A track nobody has played: artwork must still resolve to the square
        # cover from search, not a 16:9 video grab.
        cold = next(t for t in hits[1:] if t["id"] != vid)
        r = await c.get(f"/api/art/{cold['id']}")
        w, h = jpeg_size(r.content)
        check("unplayed search hit has square art", w == h and w >= 400, f"{w}x{h} — {cold['title']}")

        print("\n[8] stats")
        s = (await c.get("/api/stats")).json()
        check("stats counts it", s["tracks"] >= 1 and s["bytes"] >= total, str(s))

        print("\n[9] LRC parsing")
        from app.lyrics import parse_lrc

        got = parse_lrc("[00:26.31]hello\n[01:02.5]world\n[00:12][02:00] repeat\nnot a line\n")
        check("stamps -> ms", [x["t"] for x in got] == [12000, 26310, 62500, 120000],
              str([x["t"] for x in got]))
        check("multi-stamp line duplicates",
              sum(1 for x in got if x["text"] == "repeat") == 2)
        check("untimed lines dropped", all(x["text"] != "not a line" for x in got))
        check("two-digit fraction is centiseconds", got[1]["t"] == 26310, str(got[1]["t"]))

        print("\n[10] lyrics fetch")
        # force=true so this measures a real provider round trip even when a
        # previous run already populated the cache.
        t0 = time.perf_counter()
        ly = (await c.get(f"/api/lyrics/{vid}", params={"force": "true"})).json()
        cold = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        await c.get(f"/api/lyrics/{vid}")
        warm = (time.perf_counter() - t0) * 1000
        check("has synced lyrics", len(ly["synced"]) > 10,
              f"{len(ly['synced'])} lines from {ly['source']}")
        check("lines are ordered", all(
            ly["synced"][i]["t"] <= ly["synced"][i + 1]["t"] for i in range(len(ly["synced"]) - 1)))
        check("timings fit the track", ly["synced"][-1]["t"] / 1000 <= (row.get("duration") or 1e9),
              f"last line at {ly['synced'][-1]['t']/1000:.0f}s of {row.get('duration')}s")
        check("second fetch is cached", warm < cold / 5, f"{cold:.0f}ms -> {warm:.0f}ms")

        print("\n[11] search by lyric text")
        r = await c.get("/api/search/lyrics", params={"q": "从前从前有个人爱你很久"})
        hits = r.json()
        check("finds 晴天 from its lyric", any("晴天" in h["title"] for h in hits),
              f"{len(hits)} hits: " + ", ".join(h["title"] for h in hits[:3]))
        check("every hit carries the matched line", all(h.get("snippet") for h in hits))
        check("hits are playable ids", all(len(h["id"]) == 11 for h in hits))

        r = await c.get("/api/search/lyrics", params={"q": "I didn't want to be the one to forget"})
        hits = r.json()
        check("own library answers first", hits and hits[0].get("local") is True,
              hits[0]["title"] if hits else "no hits")

        # QQ answers a nonsense query with five confidently <em>-tagged rows, so
        # this is the check that the phrase verification is still doing its job.
        r = await c.get("/api/search/lyrics", params={"q": "zzzxqv nonexistent gibberish lyric"})
        check("nonsense finds nothing", r.json() == [], f"{len(r.json())} hits")

        print("\n[12] recommendations from listening history")
        # [5] cached a track and [6..] played it, so there's at least one seed.
        recs = (await c.get("/api/recommend", params={"limit": 15})).json()
        check("returns a batch", len(recs) >= 5, f"{len(recs)} recs")
        check("every rec explains itself", recs and all(r.get("because") for r in recs))
        check("never recommends what you already have", not any(r.get("cached") for r in recs))
        check("recs are playable ids", all(len(r["id"]) == 11 for r in recs))
        check("no duplicate recs", len({r["id"] for r in recs}) == len(recs))
        check("seeds excluded from own recs", vid not in {r["id"] for r in recs})

        print("\n[13] import a collection as a playlist (source-aware)")
        # A single Bilibili video imports as a 1-track list and exercises the
        # non-YouTube resolve+stream path (Referer header, bili extractor).
        imp = await c.post("/api/import",
                           json={"url": "https://www.bilibili.com/video/BV1uv411q7Mv"})
        if imp.status_code == 200:
            pl = imp.json()
            check("import created a playlist", pl.get("count", 0) >= 1, str(pl))
            check("detected bilibili source", pl.get("source") == "bili", pl.get("source"))
            pls = (await c.get("/api/playlists")).json()
            check("playlist is listed", any(p["id"] == pl["id"] for p in pls))
            tracks = (await c.get(f"/api/playlist/{pl['id']}")).json()
            check("playlist has tracks", len(tracks) >= 1, f"{len(tracks)} tracks")
            check("track carries bili source", tracks and tracks[0].get("source") == "bili")
            # the real proof: a bili track streams through the cache engine
            bid = tracks[0]["id"]
            r = await c.get(f"/api/stream/{bid}", headers={"Range": "bytes=0-65535"})
            check("bilibili track streams", r.status_code == 206 and len(r.content) > 1000,
                  f"{r.status_code}, {len(r.content)}B")
            await c.delete(f"/api/playlist/{pl['id']}")  # keep smoke idempotent
            check("playlist deletes", not any(
                p["id"] == pl["id"] for p in (await c.get("/api/playlists")).json()))
        else:
            # Bilibili can be geo-blocked from some IPs; don't fail the suite on that.
            check("import reachable (bili geo-block tolerated)", imp.status_code in (200, 502, 404),
                  str(imp.status_code))

    print(f"\n{'='*46}\n  {ok_count} passed, {fail_count} failed\n{'='*46}\n")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
