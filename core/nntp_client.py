from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

from nntp import NNTPClient, NNTPError
from nntp.types import Newsgroup, SSLMode

from config import NNTPConfig
from config import load as load_config
from core import header_cache
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

    return HeaderRow(
        number=int(number),
        message_id=g("message-id"),
        subject=g("subject"),
        from_addr=g("from"),
        date=g("date"),
        bytes=gi("bytes") or gi(":bytes"),
        lines=gi("lines") or gi(":lines"),
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


async def sync_group(
    pool: NNTPPool,
    cache: header_cache.HeaderCache,
    group: str,
    *,
    full: bool = False,
    progress: callable | None = None,
) -> int:
    """Inkrementeller Header-Sync. Liefert Anzahl neu eingefügter Artikel."""
    count, low, high = await pool.group_info(group)
    log.info("GROUP %s: count=%s low=%s high=%s", group, count, low, high)

    if full:
        log.info("Voller Resync angefordert → Cache für %s wird geleert", group)
        await asyncio.to_thread(cache.clear_group, group)
        last_seen = 0
    else:
        last_seen = await asyncio.to_thread(cache.get_last_seen, group)
    start = max(low, last_seen + 1)
    if start > high:
        log.info("Gruppe %s ist aktuell (last_seen=%s, high=%s)", group, last_seen, high)
        await asyncio.to_thread(cache.upsert_group, group, low, high, last_seen, None)
        return 0

    await asyncio.to_thread(cache.upsert_group, group, low, high, last_seen, None)

    inserted_total = 0
    chunk_start = start
    t0 = time.monotonic()

    while chunk_start <= high:
        chunk_end = min(chunk_start + BATCH_SIZE - 1, high)
        log.info("xover %s [%s..%s]", group, chunk_start, chunk_end)
        rows = await pool.fetch_headers(group, chunk_start, chunk_end)
        if rows:
            inserted = await asyncio.to_thread(cache.insert_articles, group, rows)
            inserted_total += inserted
            highest = max(r.number for r in rows)
            await asyncio.to_thread(cache.set_last_seen, group, highest)
        else:
            await asyncio.to_thread(cache.set_last_seen, group, chunk_end)

        if progress is not None:
            progress(chunk_end, high, inserted_total)

        chunk_start = chunk_end + 1

    dt = time.monotonic() - t0
    log.info(
        "Sync %s fertig: %s neue Artikel in %.1fs (%.0f/s)",
        group,
        inserted_total,
        dt,
        inserted_total / dt if dt > 0 else 0.0,
    )
    return inserted_total


# ---- CLI-Smoketest -------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m core.nntp_client", description="NNTP/Header-Cache Smoketest")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-active", help="Aktive Gruppen vom Server zählen")

    sp = sub.add_parser("sync", help="Header-Sync für eine Gruppe")
    sp.add_argument("group", help="z.B. de.alt.test")
    sp.add_argument("--full", action="store_true", help="Komplett-Sync (ignoriert last_seen)")

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

            n = await sync_group(pool, cache, args.group, full=args.full, progress=_show)
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
