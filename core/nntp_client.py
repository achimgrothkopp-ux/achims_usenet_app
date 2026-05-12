from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable, Literal

from nntp import NNTPClient, NNTPError
from nntp.types import Newsgroup, SSLMode

from config import NNTPConfig
from config import load as load_config
from core import header_cache
from core.dates import parse_nntp_date, parse_nntp_date_unix
from core.logging_setup import configure as configure_logging

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeaderRow:
    number: int
    message_id: str
    subject: str
    from_addr: str
    date: str
    bytes: int
    lines: int
    date_unix: int = 0  # parsed aus date, 0 = nicht parsbar


def _sanitize(s: str) -> str:
    """Surrogates und nicht-encodable Sequenzen entfernen.

    Einige Newsserver liefern Header mit kaputter Codierung; pynntp
    reicht das als surrogateescape-Strings durch. SQLite würde beim
    UTF-8-Encode crashen, also normalisieren wir hier einmal hart.
    """
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def _parse_header(number: int, hd) -> HeaderRow:
    """pynntp HeaderDict → HeaderRow (Strings normalisiert, fehlende Felder leer)."""
    def g(key: str, default: str = "") -> str:
        v = hd.get(key, default)
        if isinstance(v, str):
            return _sanitize(v.strip())
        return _sanitize(str(v)) if v is not None else default

    def gi(key: str) -> int:
        v = hd.get(key, 0)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    date_str = g("date")
    return HeaderRow(
        number=int(number),
        message_id=g("message-id"),
        subject=g("subject"),
        from_addr=g("from"),
        date=date_str,
        bytes=gi("bytes") or gi(":bytes"),
        lines=gi("lines") or gi(":lines"),
        date_unix=parse_nntp_date_unix(date_str),
    )


class NNTPPool:
    """Async-freundlicher Pool über pynntps blockierende NNTPClient.

    pynntp ist synchron; wir verheiraten ihn mit asyncio durch
    `asyncio.to_thread`. Eine NNTPClient-Instanz ist *nicht*
    thread-safe, also gibt der Pool jeden Client exklusiv heraus.
    """

    def __init__(self, cfg: NNTPConfig) -> None:
        self._cfg = cfg
        self._max = max(1, cfg.connections)
        self._idle: asyncio.Queue[NNTPClient] = asyncio.Queue()
        self._created = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def max_connections(self) -> int:
        return self._max

    def _connect_blocking(self) -> NNTPClient:
        log.debug("NNTP connect → %s:%s (TLS=%s)", self._cfg.host, self._cfg.port, self._cfg.use_tls)
        client = NNTPClient(
            host=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.username,
            password=self._cfg.password,
            use_ssl=self._cfg.use_tls,
            ssl_mode=SSLMode.IMPLICIT,
            reader=True,
            timeout=30,
        )
        try:
            caps = list(client.capabilities())
            log.debug("NNTP capabilities: %s", ", ".join(caps[:10]))
        except NNTPError as exc:
            log.debug("CAPABILITIES nicht unterstützt: %s", exc)
        return client

    async def _new_client(self) -> NNTPClient:
        return await asyncio.to_thread(self._connect_blocking)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[NNTPClient]:
        if self._closed:
            raise RuntimeError("NNTPPool wurde geschlossen")

        client: NNTPClient | None = None
        try:
            client = self._idle.get_nowait()
        except asyncio.QueueEmpty:
            async with self._lock:
                if self._created < self._max:
                    self._created += 1
                    try:
                        client = await self._new_client()
                    except Exception:
                        self._created -= 1
                        raise
            if client is None:
                client = await self._idle.get()

        try:
            yield client
        except Exception:
            # Verbindung könnte zerschossen sein → wegwerfen
            await asyncio.to_thread(self._safe_quit, client)
            async with self._lock:
                self._created -= 1
            raise
        else:
            await self._idle.put(client)

    @staticmethod
    def _safe_quit(client: NNTPClient) -> None:
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass

    def close(self) -> None:
        """Synchron schließen – wird beim Shutdown nach Loop-Exit aufgerufen."""
        self._closed = True
        while True:
            try:
                client = self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._safe_quit(client)

    # ---- High-Level-Helfer --------------------------------------------------

    async def list_active(self, pattern: str | None = None) -> list[Newsgroup]:
        async with self.acquire() as c:
            return await asyncio.to_thread(lambda: list(c.list_active(pattern)))

    async def group_info(self, name: str) -> tuple[int, int, int]:
        """(count, low, high) der Gruppe."""
        async with self.acquire() as c:
            count, low, high, _name = await asyncio.to_thread(c.group, name)
            return count, low, high

    async def fetch_headers(self, group: str, start: int, end: int) -> list[HeaderRow]:
        """xover für [start, end] inkl. Vorab-GROUP-Selektion."""
        async with self.acquire() as c:
            def _run() -> list[HeaderRow]:
                c.group(group)
                rows: list[HeaderRow] = []
                for number, hd in c.xover((start, end)):
                    rows.append(_parse_header(number, hd))
                return rows
            return await asyncio.to_thread(_run)

    async def article(self, message_id: str) -> tuple[dict, bytes]:
        """ARTICLE per Message-ID → (Header-Dict, Body-Bytes)."""
        async with self.acquire() as c:
            def _run() -> tuple[dict, bytes]:
                _num, hd, body = c.article(message_id)
                return dict(hd), body
            return await asyncio.to_thread(_run)


# ---- Header-Sync ---------------------------------------------------------------

BATCH_SIZE = 10_000

SyncMode = Literal["incremental", "last_n", "since_date", "full"]


@dataclass(frozen=True)
class SyncPlan:
    """Beschreibt, *was* ein sync_group()-Lauf tun soll.

    - incremental: ab last_seen+1 bis high
    - last_n:      die letzten N Article-Nummern (max(low, high-N+1, last_seen+1) bis high)
    - since_date:  ab Datum, gefunden per find_article_at_date()
    - full:        Cache leeren, ab low syncen

    `max_articles` deckelt zusätzlich jeden Lauf: nach so vielen Article-Numbers ist
    Schluss, der Rest geht beim nächsten Sync (last_seen wird pro Chunk persistiert).
    """

    mode: SyncMode = "incremental"
    last_n: int | None = None
    since: datetime | None = None
    max_articles: int | None = None
    # Anzahl paralleler NNTP-Connections für den Header-Fetch. 1 = sequentiell.
    # Inserts in SQLite bleiben serialisiert (state_lock); parallel ist hier
    # nur der netzwerk-bound XOVER-Roundtrip.
    parallel: int = 1

    @classmethod
    def incremental(
        cls, *, max_articles: int | None = None, parallel: int = 1
    ) -> "SyncPlan":
        return cls(mode="incremental", max_articles=max_articles, parallel=parallel)

    @classmethod
    def last_n_articles(
        cls, n: int, *, max_articles: int | None = None, parallel: int = 1
    ) -> "SyncPlan":
        return cls(
            mode="last_n", last_n=int(n), max_articles=max_articles, parallel=parallel
        )

    @classmethod
    def since_date(
        cls, dt: datetime, *, max_articles: int | None = None, parallel: int = 1
    ) -> "SyncPlan":
        return cls(
            mode="since_date", since=dt, max_articles=max_articles, parallel=parallel
        )

    @classmethod
    def full(cls, *, max_articles: int | None = None, parallel: int = 1) -> "SyncPlan":
        return cls(mode="full", max_articles=max_articles, parallel=parallel)


async def find_article_at_date(
    pool: "NNTPPool",
    group: str,
    target: datetime,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    """Binary Search nach der ersten Article-Number mit Date >= target.

    Rückgabe liegt in [low, high+1]. high+1 bedeutet: das Zieldatum liegt
    hinter dem neuesten verfügbaren Artikel, also nichts zu syncen.

    Lücken (Article-Nummern ohne Header) werden überbrückt, indem statt
    eines Punkt-Reads ein kleines Fenster gelesen wird; das erste
    parsbare Datum darin entscheidet den Bisection-Schritt.
    """
    if low is None or high is None:
        _, low2, high2 = await pool.group_info(group)
        if low is None:
            low = low2
        if high is None:
            high = high2
    if low > high:
        return high + 1
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    async def first_parsable(num: int, window: int = 100) -> tuple[int, datetime] | None:
        end = min(num + window, high)
        rows = await pool.fetch_headers(group, num, end)
        for r in rows:
            dt = parse_nntp_date(r.date)
            if dt is not None:
                return r.number, dt
        return None

    lo, hi = low, high
    # log2(1M) ≈ 20, Limit großzügig wegen Lücken-Bypass.
    for _ in range(50):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        result = await first_parsable(mid)
        if result is None:
            # Fenster ab mid liefert nichts → linke Hälfte einschränken
            hi = mid
            continue
        num, dt = result
        if dt < target:
            lo = num + 1
        else:
            hi = num

    end_check = await first_parsable(lo)
    if end_check is None or end_check[1] < target:
        return high + 1
    return end_check[0]


async def sync_group(
    pool: NNTPPool,
    cache: header_cache.HeaderCache,
    group: str,
    *,
    plan: SyncPlan | None = None,
    progress: callable | None = None,
    cancel: asyncio.Event | None = None,
) -> int:
    """Header-Sync nach Plan. Liefert Anzahl neu eingefügter Artikel.

    Ist `cancel` gesetzt, prüft die Schleife zwischen Chunks und kehrt
    sauber zurück; bereits eingefügte Chunks bleiben persistent
    (last_seen wird pro Chunk geschrieben).
    """
    if plan is None:
        plan = SyncPlan()

    count, low, high = await pool.group_info(group)
    log.info("GROUP %s: count=%s low=%s high=%s mode=%s", group, count, low, high, plan.mode)

    if plan.mode == "full":
        log.info("Vollsync angefordert → Cache für %s wird geleert", group)
        await asyncio.to_thread(cache.clear_group, group)
        last_seen = 0
    else:
        last_seen = await asyncio.to_thread(cache.get_last_seen, group)

    if plan.mode == "incremental":
        start = max(low, last_seen + 1)
    elif plan.mode == "last_n":
        if not plan.last_n or plan.last_n < 1:
            raise ValueError("plan.last_n muss eine positive Anzahl sein")
        start = max(low, high - plan.last_n + 1, last_seen + 1)
    elif plan.mode == "since_date":
        if plan.since is None:
            raise ValueError("plan.since muss ein Datum sein")
        # Schon up-to-date → Bisection überspringen (spart bis 20 Roundtrips)
        if last_seen >= high:
            start = high + 1
        else:
            date_start = await find_article_at_date(pool, group, plan.since, low=low, high=high)
            start = max(low, date_start, last_seen + 1)
    elif plan.mode == "full":
        start = low
    else:
        raise ValueError(f"Unbekannter sync mode: {plan.mode!r}")

    if start > high:
        log.info("Gruppe %s: nichts zu syncen (start=%s, high=%s)", group, start, high)
        await asyncio.to_thread(cache.upsert_group, group, low, high, last_seen, None)
        return 0

    end_limit = high
    if plan.max_articles is not None and plan.max_articles > 0:
        end_limit = min(high, start + plan.max_articles - 1)

    await asyncio.to_thread(cache.upsert_group, group, low, high, last_seen, None)

    # Chunk-Plan vorab: deterministische Liste konsekutiver Article-Ranges.
    chunks: list[tuple[int, int]] = []
    cs = start
    while cs <= end_limit:
        chunks.append((cs, min(cs + BATCH_SIZE - 1, end_limit)))
        cs = chunks[-1][1] + 1
    n_workers = max(1, plan.parallel)

    inserted_total = 0
    done = [False] * len(chunks)
    done_until = -1  # höchster Index, dessen Präfix lückenlos fertig ist
    state_lock = asyncio.Lock()
    fetch_error: Exception | None = None
    t0 = time.monotonic()

    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(len(chunks)):
        queue.put_nowait(i)

    async def commit_prefix_locked(idx_done: int) -> None:
        """state_lock muss gehalten sein. Markiert idx_done fertig und schiebt
        last_seen so weit vor, wie das Anfangs-Präfix lückenlos abgehakt ist."""
        nonlocal done_until
        done[idx_done] = True
        while done_until + 1 < len(chunks) and done[done_until + 1]:
            done_until += 1
            await asyncio.to_thread(cache.set_last_seen, group, chunks[done_until][1])
        if progress is not None:
            cur = chunks[done_until][1] if done_until >= 0 else start - 1
            progress(cur, end_limit, inserted_total)

    async def worker() -> None:
        nonlocal inserted_total, fetch_error
        while True:
            # Cancel stoppt nur das Ziehen neuer Chunks. Wer schon einen
            # gefetched hat, persistiert ihn fertig – sonst wäre der
            # Netzwerk-Roundtrip verschenkt und last_seen würde
            # unnötig hinter dem fetched-Stand hinterherhinken.
            if cancel is not None and cancel.is_set():
                return
            try:
                idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            cs_, ce_ = chunks[idx]
            log.info("xover %s [%s..%s]", group, cs_, ce_)
            try:
                rows = await pool.fetch_headers(group, cs_, ce_)
            except Exception as exc:
                log.exception("fetch_headers fehlgeschlagen [%s..%s]", cs_, ce_)
                fetch_error = exc
                if cancel is not None:
                    cancel.set()
                return
            async with state_lock:
                if rows:
                    inserted = await asyncio.to_thread(cache.insert_articles, group, rows)
                    inserted_total += inserted
                await commit_prefix_locked(idx)

    await asyncio.gather(*[worker() for _ in range(n_workers)])

    aborted = cancel is not None and cancel.is_set()
    dt = time.monotonic() - t0
    log.info(
        "Sync %s %s: %s neue Artikel in %.1fs (%.0f/s, parallel=%d)",
        group,
        "abgebrochen" if aborted else "fertig",
        inserted_total,
        dt,
        inserted_total / dt if dt > 0 else 0.0,
        n_workers,
    )
    if fetch_error is not None:
        raise fetch_error
    return inserted_total


# ---- CLI-Smoketest -------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m core.nntp_client", description="NNTP/Header-Cache Smoketest")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-active", help="Aktive Gruppen vom Server zählen")

    sp = sub.add_parser("sync", help="Header-Sync für eine Gruppe")
    sp.add_argument("group", help="z.B. de.alt.test")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Vollsync: Cache leeren, ab low syncen")
    mode.add_argument("--last-n", type=int, metavar="N", help="Nur die letzten N Article-Nummern syncen")
    mode.add_argument("--since", metavar="YYYY-MM-DD", help="Ab Datum syncen (Bisection)")
    sp.add_argument("--max-articles", type=int, metavar="N", help="Pro-Sync-Cap: höchstens N Article-Numbers")
    sp.add_argument("--parallel", type=int, default=1, metavar="N", help="Parallele Connections fürs Header-Fetching (Default 1)")

    sp = sub.add_parser("search", help="FTS5-Suche im Cache")
    sp.add_argument("query")
    sp.add_argument("--group", help="auf eine Gruppe einschränken")
    sp.add_argument("--limit", type=int, default=20)

    sub.add_parser("subscribe", help="Gruppe abonnieren").add_argument("group")
    return p


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_config()
    configure_logging(cfg.logging.level, cfg.logging.log_dir)

    pool = NNTPPool(cfg.nntp)
    cache = header_cache.HeaderCache(cfg.storage.header_cache_path)
    cache.init_schema()

    try:
        if args.cmd == "list-active":
            groups = await pool.list_active()
            print(f"{len(groups)} aktive Gruppen")
            for g in groups[:5]:
                print(f"  {g.name}  low={g.low} high={g.high} status={g.status}")
            print("  ...")

        elif args.cmd == "sync":
            cache.set_subscribed(args.group, True)

            def _show(cur: int, total: int, inserted: int) -> None:
                pct = 100.0 * cur / total if total else 100.0
                print(f"  {args.group}: {cur}/{total} ({pct:5.1f}%)  insgesamt neu: {inserted}", flush=True)

            par = max(1, args.parallel)
            if args.full:
                plan = SyncPlan.full(max_articles=args.max_articles, parallel=par)
            elif args.last_n is not None:
                plan = SyncPlan.last_n_articles(args.last_n, max_articles=args.max_articles, parallel=par)
            elif args.since:
                since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                plan = SyncPlan.since_date(since, max_articles=args.max_articles, parallel=par)
            else:
                plan = SyncPlan.incremental(max_articles=args.max_articles, parallel=par)

            n = await sync_group(pool, cache, args.group, plan=plan, progress=_show)
            print(f"Fertig. Neu eingefügt: {n}")

        elif args.cmd == "subscribe":
            cache.set_subscribed(args.group, True)
            print(f"Subscribed: {args.group}")

        elif args.cmd == "search":
            hits = cache.search(args.query, group=args.group, limit=args.limit)
            print(f"{len(hits)} Treffer")
            for hit in hits:
                print(f"  [{hit.group}] #{hit.number}  {hit.subject!r}  ({hit.from_addr})")

    finally:
        pool.close()
        cache.close()
    return 0


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
