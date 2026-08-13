import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    artist      TEXT DEFAULT '',
    album       TEXT DEFAULT '',
    duration    INTEGER DEFAULT 0,
    thumb       TEXT DEFAULT '',
    size        INTEGER DEFAULT 0,
    ext         TEXT DEFAULT 'm4a',
    cached      INTEGER DEFAULT 0,
    liked       INTEGER DEFAULT 0,
    disliked    INTEGER DEFAULT 0,
    play_count  INTEGER DEFAULT 0,
    added_at    INTEGER DEFAULT 0,
    last_played INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'yt',   -- yt | bili — which extractor resolves this
    page_url    TEXT DEFAULT ''      -- exact webpage URL for non-YouTube sources
);
CREATE INDEX IF NOT EXISTS idx_tracks_added ON tracks(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_cached ON tracks(cached);

CREATE TABLE IF NOT EXISTS lyrics (
    id         TEXT PRIMARY KEY,
    synced     TEXT DEFAULT '',
    plain      TEXT DEFAULT '',
    source     TEXT DEFAULT 'none',
    fetched_at INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    source     TEXT DEFAULT '',     -- yt | bili
    url        TEXT DEFAULT '',     -- the imported collection URL
    created_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    track_id    TEXT NOT NULL,
    pos         INTEGER DEFAULT 0,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_pltracks ON playlist_tracks(playlist_id, pos);

CREATE TABLE IF NOT EXISTS followed_artists (
    name        TEXT PRIMARY KEY,
    followed_at INTEGER DEFAULT 0
);
"""

# Columns added after the tracks table already shipped; ALTER on old DBs.
_MIGRATIONS = [
    ("tracks", "source", "TEXT DEFAULT 'yt'"),
    ("tracks", "page_url", "TEXT DEFAULT ''"),
    ("tracks", "disliked", "INTEGER DEFAULT 0"),
]


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(DB_PATH, check_same_thread=False)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.executescript(SCHEMA)
                have = {r["name"] for r in c.execute("PRAGMA table_info(tracks)")}
                for table, col, decl in _MIGRATIONS:
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                c.commit()
                _conn = c
    return _conn


def _write(sql: str, params: tuple = ()) -> None:
    c = conn()
    with _lock:
        c.execute(sql, params)
        c.commit()


_COLS = "(id, title, artist, album, duration, thumb, added_at, source, page_url)"
_VALS = "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"


def _row(m: dict, now: int) -> tuple:
    return (
        m["id"],
        m.get("title") or m["id"],
        m.get("artist") or "",
        m.get("album") or "",
        int(m.get("duration") or 0),
        m.get("thumb") or "",
        now,
        m.get("source") or "yt",
        m.get("page_url") or "",
    )


def upsert_tracks(metas: list[dict]) -> None:
    """Write display metadata from YouTube Music; leave local state alone.

    Every search result lands here, so the table doubles as a metadata cache:
    artwork lookups can then find the real square cover. Library views filter on
    cached=1, so un-downloaded rows stay invisible.

    Only title/artist/album/duration/thumb are touched. cached, liked,
    play_count, size and added_at describe *this* machine's relationship to the
    track and must survive a re-search.
    """
    if not metas:
        return
    now = int(time.time())
    rows = [_row(m, now) for m in metas if m.get("id")]
    c = conn()
    with _lock:
        c.executemany(
            f"""
            INSERT INTO tracks {_COLS} {_VALS}
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                artist=excluded.artist,
                album=excluded.album,
                duration=CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE tracks.duration END,
                thumb=CASE WHEN excluded.thumb != '' THEN excluded.thumb ELSE tracks.thumb END
            """,
            rows,
        )
        c.commit()


def upsert_track(meta: dict) -> None:
    upsert_tracks([meta])


def ensure_track(meta: dict) -> None:
    """Insert only if absent — the downloader's last-resort metadata.

    yt-dlp reads the *video* page, which has no album name and no square cover
    (checked: all 42 thumbnails it offers are 16:9 or 4:3). So this must never
    overwrite what search already knows; it only keeps a row from being missing
    entirely for a track played by id.
    """
    _write(f"INSERT INTO tracks {_COLS} {_VALS} ON CONFLICT(id) DO NOTHING",
           _row(meta, int(time.time())))


def get_source(vid: str) -> tuple[str, str]:
    """(source, page_url) for a track — how the streamer should resolve it."""
    row = conn().execute("SELECT source, page_url FROM tracks WHERE id=?", (vid,)).fetchone()
    if not row:
        return "yt", ""
    return (row["source"] or "yt"), (row["page_url"] or "")


# ---------- playlists (imported 清单) ----------

def create_playlist(name: str, source: str, url: str) -> int:
    c = conn()
    with _lock:
        cur = c.execute(
            "INSERT INTO playlists (name, source, url, created_at) VALUES (?, ?, ?, ?)",
            (name, source, url, int(time.time())),
        )
        c.commit()
        return cur.lastrowid


def add_playlist_tracks(pid: int, track_ids: list[str]) -> None:
    if not track_ids:
        return
    rows = [(pid, tid, i) for i, tid in enumerate(track_ids)]
    c = conn()
    with _lock:
        c.executemany(
            "INSERT INTO playlist_tracks (playlist_id, track_id, pos) VALUES (?, ?, ?) "
            "ON CONFLICT(playlist_id, track_id) DO UPDATE SET pos=excluded.pos",
            rows,
        )
        c.commit()


def add_track_to_playlist(pid: int, track_id: str) -> None:
    """Append one track to a playlist (no dup, keeps order)."""
    c = conn()
    with _lock:
        row = c.execute(
            "SELECT COALESCE(MAX(pos), -1) + 1 AS n FROM playlist_tracks WHERE playlist_id=?",
            (pid,),
        ).fetchone()
        c.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_id, pos) VALUES (?, ?, ?) "
            "ON CONFLICT(playlist_id, track_id) DO NOTHING",
            (pid, track_id, row["n"]),
        )
        c.commit()


def remove_playlist_track(pid: int, track_id: str) -> None:
    _write("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pid, track_id))


def rename_playlist(pid: int, name: str) -> None:
    _write("UPDATE playlists SET name=? WHERE id=?", (name, pid))


def list_playlists() -> list[dict]:
    rows = conn().execute(
        """
        SELECT p.*, COUNT(pt.track_id) AS count
        FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
        GROUP BY p.id ORDER BY p.created_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def playlist_tracks(pid: int) -> list[dict]:
    rows = conn().execute(
        """
        SELECT t.* FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
        WHERE pt.playlist_id = ? ORDER BY pt.pos
        """,
        (pid,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_playlist(pid: int) -> None:
    c = conn()
    with _lock:
        c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
        c.commit()


def mark_cached(vid: str, size: int, ext: str) -> None:
    # added_at is refreshed here, not at insert: the library sorts by it, and what
    # matters is when a track was downloaded, not when it drifted past in a search.
    _write(
        "UPDATE tracks SET cached=1, size=?, ext=?, added_at=? WHERE id=?",
        (size, ext, int(time.time()), vid),
    )


def mark_uncached(vid: str) -> None:
    _write("UPDATE tracks SET cached=0, size=0 WHERE id=?", (vid,))


def bump_play(vid: str) -> None:
    _write(
        "UPDATE tracks SET play_count=play_count+1, last_played=? WHERE id=?",
        (int(time.time()), vid),
    )


def set_liked(vid: str, liked: bool) -> None:
    # 喜欢和不喜欢互斥:喜欢一首歌时清掉它的不喜欢标记。
    if liked:
        _write("UPDATE tracks SET liked=1, disliked=0 WHERE id=?", (vid,))
    else:
        _write("UPDATE tracks SET liked=0 WHERE id=?", (vid,))


def set_disliked(vid: str, disliked: bool) -> None:
    # 不喜欢时也顺手取消喜欢/收藏,两者互斥。
    if disliked:
        _write("UPDATE tracks SET disliked=1, liked=0 WHERE id=?", (vid,))
    else:
        _write("UPDATE tracks SET disliked=0 WHERE id=?", (vid,))


def disliked_ids() -> set[str]:
    return {r["id"] for r in conn().execute("SELECT id FROM tracks WHERE disliked=1")}


def follow_artist(name: str) -> None:
    _write("INSERT OR IGNORE INTO followed_artists (name, followed_at) VALUES (?, ?)",
           (name, int(time.time())))


def unfollow_artist(name: str) -> None:
    _write("DELETE FROM followed_artists WHERE name=?", (name,))


def followed_artists() -> list[str]:
    return [r["name"] for r in
            conn().execute("SELECT name FROM followed_artists ORDER BY followed_at DESC")]


def is_following(name: str) -> bool:
    return conn().execute("SELECT 1 FROM followed_artists WHERE name=?", (name,)).fetchone() is not None


def get_track(vid: str) -> dict | None:
    row = conn().execute("SELECT * FROM tracks WHERE id=?", (vid,)).fetchone()
    return dict(row) if row else None


def list_tracks(only_cached: bool = True, liked: bool = False) -> list[dict]:
    sql = "SELECT * FROM tracks WHERE 1=1"
    if only_cached:
        sql += " AND cached=1"
    if liked:
        sql += " AND liked=1"
    sql += " ORDER BY added_at DESC"
    return [dict(r) for r in conn().execute(sql).fetchall()]


def cached_ids() -> set[str]:
    return {r["id"] for r in conn().execute("SELECT id FROM tracks WHERE cached=1")}


def liked_ids() -> set[str]:
    return {r["id"] for r in conn().execute("SELECT id FROM tracks WHERE liked=1")}


def seed_tracks(limit: int = 8) -> list[dict]:
    """Best tracks to seed recommendations from — this listener's actual taste.

    Ordered by strength of signal: liked first, then most-played, then most
    recent. These become the centres of the radio stations we blend.
    """
    rows = conn().execute(
        """
        SELECT * FROM tracks
        WHERE (liked=1 OR play_count>0 OR cached=1) AND disliked=0
        ORDER BY last_played DESC, liked DESC, play_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_lyrics(vid: str) -> dict | None:
    row = conn().execute("SELECT * FROM lyrics WHERE id=?", (vid,)).fetchone()
    if not row:
        return None
    return {
        "synced": json.loads(row["synced"]) if row["synced"] else [],
        "plain": row["plain"] or "",
        "source": row["source"] or "none",
    }


def put_lyrics(vid: str, data: dict) -> None:
    # Misses are stored too (source='none'). Plenty of tracks genuinely have no
    # lyrics anywhere, and without this every play would re-ask both providers.
    _write(
        """
        INSERT INTO lyrics (id, synced, plain, source, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            synced=excluded.synced, plain=excluded.plain,
            source=excluded.source, fetched_at=excluded.fetched_at
        """,
        (
            vid,
            json.dumps(data.get("synced") or [], ensure_ascii=False),
            data.get("plain") or "",
            data.get("source") or "none",
            int(time.time()),
        ),
    )


def search_cached_lyrics(q: str, limit: int = 30) -> list[dict]:
    """Lyric search across tracks already in the library — instant, works offline."""
    rows = conn().execute(
        """
        SELECT t.*, l.plain FROM lyrics l JOIN tracks t ON t.id = l.id
        WHERE t.cached = 1 AND l.plain != '' AND l.plain LIKE ?
        ORDER BY t.play_count DESC LIMIT ?
        """,
        (f"%{q}%", limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        plain = d.pop("plain", "")
        hit = next((ln for ln in plain.splitlines() if q.lower() in ln.lower()), "")
        d["snippet"] = hit.strip()
        out.append(d)
    return out


def stats() -> dict:
    row = conn().execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM tracks WHERE cached=1"
    ).fetchone()
    return {"tracks": row["n"], "bytes": row["bytes"]}
