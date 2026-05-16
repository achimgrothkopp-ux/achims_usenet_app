from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from core.dates import parse_nntp_date_unix

if TYPE_CHECKING:
    from core.nntp_client import HeaderRow

log = logging.getLogger(__name__)


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    name              TEXT PRIMARY KEY,
    low_number        INTEGER NOT NULL DEFAULT 0,
    high_number       INTEGER NOT NULL DEFAULT 0,
    last_article_seen INTEGER NOT NULL DEFAULT 0,
    subscribed        INTEGER NOT NULL DEFAULT 0,
    status            TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    group_name  TEXT NOT NULL,
    number      INTEGER NOT NULL,
    message_id  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    from_addr   TEXT NOT NULL,
    date        TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    lines       INTEGER NOT NULL DEFAULT 0,
    date_unix   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_name, number)
);

CREATE INDEX IF NOT EXISTS idx_articles_msgid ON articles(message_id);
-- idx_articles_date_unix wird in _migrate_date_unix_locked() erzeugt,
-- erst nachdem die Spalte sicher existiert (alte DBs brauchen ein
-- ALTER TABLE davor).

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    subject,
    from_addr,
    group_name UNINDEXED,
    number     UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- FTS automatisch synchron halten. Beim INSERT OR IGNORE feuert der
-- Trigger nur wenn tatsächlich eine Zeile eingefügt wurde, also
-- bekommt FTS keine Duplikate bei Re-Syncs.
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(subject, from_addr, group_name, number)
    VALUES (new.subject, new.from_addr, new.group_name, new.number);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    DELETE FROM articles_fts WHERE group_name = old.group_name AND number = old.number;
END;
"""


@dataclass(frozen=True)
class GroupRow:
    name: str
    low: int
    high: int
    last_seen: int
    subscribed: bool
    status: str | None


@dataclass(frozen=True)
class SearchHit:
    group: str
    number: int
    subject: str
    from_addr: str


@dataclass(frozen=True)
class ArticleRow:
    group: str
    number: int
    message_id: str
    subject: str
    from_addr: str
    date: str
    bytes: int
    lines: int
    date_unix: int = 0


class HeaderCache:
    """SQLite-Wrapper. Sync; Aufrufer wickelt I/O ggf. mit asyncio.to_thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Eine sqlite3.Connection ist nicht thread-safe; wir greifen aus
        # GUI-Thread und Worker-Threads (asyncio.to_thread) zu.
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_date_unix_locked()

    def _migrate_date_unix_locked(self) -> None:
        """Migration für DBs, die vor der date_unix-Spalte angelegt wurden.

        Idempotent: ADD COLUMN nur, wenn fehlt; Backfill nur für Zeilen,
        die noch nicht aus dem Date-String geparst wurden. Unparsbare
        Datümer bekommen -1, damit der Backfill sie beim nächsten Start
        nicht endlos wiederholt.
        """
        import time

        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(articles)")}
        if "date_unix" not in cols:
            log.info("Migration: ALTER TABLE articles ADD COLUMN date_unix")
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN date_unix INTEGER NOT NULL DEFAULT 0"
            )

        # Index für frische DBs ebenso wie für migrierte DBs.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_date_unix "
            "ON articles(group_name, date_unix)"
        )

        cnt = self._conn.execute(
            "SELECT COUNT(*) FROM articles WHERE date_unix = 0 AND date != ''"
        ).fetchone()[0]
        if cnt == 0:
            return

        log.info("Backfill date_unix für %d Zeilen …", cnt)
        t0 = time.monotonic()
        BATCH = 10_000
        last_rowid = 0
        done = 0
        while True:
            rows = self._conn.execute(
                "SELECT rowid, date FROM articles "
                "WHERE date_unix = 0 AND date != '' AND rowid > ? "
                "ORDER BY rowid LIMIT ?",
                (last_rowid, BATCH),
            ).fetchall()
            if not rows:
                break
            updates = []
            for r in rows:
                unix = parse_nntp_date_unix(r["date"])
                # -1 = "versucht, nicht parsbar" → beim nächsten Start überspringen
                updates.append((unix if unix > 0 else -1, r["rowid"]))
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany(
                    "UPDATE articles SET date_unix = ? WHERE rowid = ?", updates
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            done += len(updates)
            last_rowid = rows[-1]["rowid"]

        dt = time.monotonic() - t0
        log.info("Backfill date_unix fertig: %d Zeilen in %.1fs", done, dt)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- Groups ----------------------------------------------------------

    def upsert_group(
        self,
        name: str,
        low: int,
        high: int,
        last_seen: int | None,
        status: str | None,
    ) -> None:
        with self._lock:
            if last_seen is None:
                row = self._conn.execute(
                    "SELECT last_article_seen FROM groups WHERE name = ?", (name,)
                ).fetchone()
                last_seen = row["last_article_seen"] if row else 0
            self._conn.execute(
                """
                INSERT INTO groups(name, low_number, high_number, last_article_seen, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    low_number  = excluded.low_number,
                    high_number = excluded.high_number,
                    last_article_seen = excluded.last_article_seen,
                    status = COALESCE(excluded.status, groups.status)
                """,
                (name, low, high, last_seen, status),
            )

    def set_subscribed(self, name: str, subscribed: bool) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO groups(name, subscribed) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET subscribed = excluded.subscribed
                """,
                (name, 1 if subscribed else 0),
            )

    def list_subscribed(self) -> list[GroupRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, low_number, high_number, last_article_seen, subscribed, status "
                "FROM groups WHERE subscribed = 1 ORDER BY name"
            ).fetchall()
        return [self._group_row(r) for r in rows]

    def list_all_groups(self, *, name_like: str | None = None) -> list[GroupRow]:
        """Alle bekannten Gruppen (subscribed + nur-aus-LIST-ACTIVE).

        `name_like` ist eine SQL-LIKE-Pattern (z.B. '%binaries%'). None liefert alles.
        """
        sql = (
            "SELECT name, low_number, high_number, last_article_seen, subscribed, status "
            "FROM groups"
        )
        params: list = []
        if name_like:
            sql += " WHERE name LIKE ?"
            params.append(name_like)
        sql += " ORDER BY name"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._group_row(r) for r in rows]

    def upsert_groups_bulk(self, rows: Iterable[tuple[str, int, int, str | None]]) -> int:
        """Bulk-Insert/Update vieler Gruppen aus LIST ACTIVE.

        Erwartet Tupel (name, low, high, status). subscribed und
        last_article_seen werden NICHT angefasst – das gehört dem
        Abonnenten-Workflow.
        Liefert die Anzahl getouchter Zeilen (insgesamt eingespielter Rows).
        """
        payload = list(rows)
        if not payload:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany(
                    """
                    INSERT INTO groups(name, low_number, high_number, status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        low_number  = excluded.low_number,
                        high_number = excluded.high_number,
                        status      = COALESCE(excluded.status, groups.status)
                    """,
                    payload,
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return len(payload)

    def get_group(self, name: str) -> GroupRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, low_number, high_number, last_article_seen, subscribed, status "
                "FROM groups WHERE name = ?",
                (name,),
            ).fetchone()
        return self._group_row(row) if row else None

    @staticmethod
    def _group_row(r: sqlite3.Row) -> GroupRow:
        return GroupRow(
            name=r["name"],
            low=r["low_number"],
            high=r["high_number"],
            last_seen=r["last_article_seen"],
            subscribed=bool(r["subscribed"]),
            status=r["status"],
        )

    def get_last_seen(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_article_seen FROM groups WHERE name = ?", (name,)
            ).fetchone()
        return int(row["last_article_seen"]) if row else 0

    def set_last_seen(self, name: str, value: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO groups(name, last_article_seen) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET last_article_seen =
                    MAX(groups.last_article_seen, excluded.last_article_seen)
                """,
                (name, int(value)),
            )

    # ---- Articles --------------------------------------------------------

    def insert_articles(self, group: str, rows: Iterable["HeaderRow"]) -> int:
        payload = [
            (
                group,
                r.number,
                r.message_id,
                r.subject,
                r.from_addr,
                r.date,
                r.bytes,
                r.lines,
                # -1 = "Datum vorhanden, aber nicht parsbar" – konsistent
                # zur Migration, vermeidet Backfill-Loops für solche Rows.
                r.date_unix if r.date_unix > 0 else (-1 if r.date else 0),
            )
            for r in rows
        ]
        if not payload:
            return 0

        # Chunk-Range für die COUNT-Begrenzung: payload kommt aus einem
        # konsekutiven xover-Range, also reicht ein Lookup auf den
        # (group_name, number)-PK in dieser Fensterspanne (O(chunk_size)
        # statt O(group_size) wie der frühere Full-Group-Scan).
        nums = [p[1] for p in payload]
        lo, hi = min(nums), max(nums)

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                before = self._conn.execute(
                    "SELECT COUNT(*) FROM articles "
                    "WHERE group_name = ? AND number BETWEEN ? AND ?",
                    (group, lo, hi),
                ).fetchone()[0]
                cur.executemany(
                    """
                    INSERT INTO articles(group_name, number, message_id, subject, from_addr, date, bytes, lines, date_unix)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_name, number) DO NOTHING
                    """,
                    payload,
                )
                after = self._conn.execute(
                    "SELECT COUNT(*) FROM articles "
                    "WHERE group_name = ? AND number BETWEEN ? AND ?",
                    (group, lo, hi),
                ).fetchone()[0]
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return int(after - before)

    def clear_group(self, group: str) -> None:
        """Alles für eine Gruppe löschen.

        Der per-row AFTER-DELETE-Trigger auf articles würde bei großen
        Gruppen (Mio Zeilen) jede Löschung einzeln im FTS-Index suchen,
        was Stunden dauern kann. Wir droppen den Trigger kurzzeitig,
        machen zwei Bulk-DELETEs und stellen den Trigger wieder her.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute("DROP TRIGGER IF EXISTS articles_ad")
                cur.execute("DELETE FROM articles_fts WHERE group_name = ?", (group,))
                cur.execute("DELETE FROM articles WHERE group_name = ?", (group,))
                cur.execute(
                    "UPDATE groups SET last_article_seen = 0 WHERE name = ?", (group,)
                )
                cur.execute(
                    """
                    CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
                        DELETE FROM articles_fts
                         WHERE group_name = old.group_name AND number = old.number;
                    END
                    """
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def article_count(self, group: str | None = None) -> int:
        with self._lock:
            if group:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM articles WHERE group_name = ?", (group,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()
        return int(row["c"])

    def get_article(self, group: str, number: int) -> ArticleRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT group_name, number, message_id, subject, from_addr, date, bytes, lines, date_unix "
                "FROM articles WHERE group_name = ? AND number = ?",
                (group, number),
            ).fetchone()
        return self._article_row(row) if row else None

    def fetch_articles(
        self,
        group: str,
        *,
        offset: int = 0,
        limit: int = 500,
        order: str = "desc",
    ) -> list[ArticleRow]:
        """Articles seitenweise abrufen. `limit <= 0` heißt 'alle ab offset'."""
        order_sql = "DESC" if order.lower() == "desc" else "ASC"
        if limit is None or int(limit) <= 0:
            # SQLite: LIMIT -1 = unbegrenzt; mit OFFSET kombinierbar.
            limit_sql = -1
        else:
            limit_sql = int(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT group_name, number, message_id, subject, from_addr, date, bytes, lines, date_unix "
                f"FROM articles WHERE group_name = ? ORDER BY number {order_sql} LIMIT ? OFFSET ?",
                (group, limit_sql, int(offset)),
            ).fetchall()
        return [self._article_row(r) for r in rows]

    def search(self, query: str, *, group: str | None = None, limit: int = 50) -> list[SearchHit]:
        params: list = [query]
        sql = (
            "SELECT group_name, number, subject, from_addr "
            "FROM articles_fts WHERE articles_fts MATCH ?"
        )
        if group:
            sql += " AND group_name = ?"
            params.append(group)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            SearchHit(
                group=r["group_name"],
                number=int(r["number"]),
                subject=r["subject"],
                from_addr=r["from_addr"],
            )
            for r in rows
        ]

    def search_articles(
        self, query: str, *, group: str | None = None, limit: int = 50
    ) -> list[ArticleRow]:
        """FTS-Suche, die direkt komplette ArticleRows liefert (ein JOIN statt N Selects).

        FTS5 MATCH muss auf den unaliassed Tabellennamen — Alias `f` wird
        von SQLite nicht akzeptiert ('no such column: f').
        """
        params: list = [query]
        sql = (
            "SELECT articles.group_name, articles.number, articles.message_id, "
            "       articles.subject, articles.from_addr, articles.date, "
            "       articles.bytes, articles.lines, articles.date_unix "
            "FROM articles_fts "
            "JOIN articles "
            "  ON articles.group_name = articles_fts.group_name "
            " AND articles.number     = articles_fts.number "
            "WHERE articles_fts MATCH ?"
        )
        if group:
            sql += " AND articles_fts.group_name = ?"
            params.append(group)
        sql += " ORDER BY articles_fts.rank LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._article_row(r) for r in rows]

    @staticmethod
    def _article_row(r: sqlite3.Row) -> ArticleRow:
        return ArticleRow(
            group=r["group_name"],
            number=int(r["number"]),
            message_id=r["message_id"],
            subject=r["subject"],
            from_addr=r["from_addr"],
            date=r["date"],
            bytes=int(r["bytes"]),
            lines=int(r["lines"]),
            date_unix=int(r["date_unix"]),
        )
