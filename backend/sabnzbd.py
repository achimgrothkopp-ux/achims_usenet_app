"""Async-Client für die SABnzbd JSON-API.

API-Referenz: https://sabnzbd.org/wiki/configuration/4.5/api
Alle Calls verwenden ?output=json. Authentifizierung via apikey-Param.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from config import SABnzbdConfig

log = logging.getLogger(__name__)


class SABError(RuntimeError):
    """Server hat einen Fehler im JSON gemeldet oder HTTP nicht-2xx."""


@dataclass(frozen=True)
class QueueSlot:
    nzo_id: str
    filename: str
    status: str
    size_mb: float
    sizeleft_mb: float
    percentage: int
    eta: str

    @property
    def progress(self) -> float:
        if self.size_mb <= 0:
            return 0.0
        done = max(0.0, self.size_mb - self.sizeleft_mb)
        return min(1.0, done / self.size_mb)


@dataclass(frozen=True)
class QueueSnapshot:
    paused: bool
    speed: str          # z.B. "1.2 M"
    size_mb: float
    sizeleft_mb: float
    slots: list[QueueSlot]


class SABnzbdClient:
    def __init__(self, cfg: SABnzbdConfig, *, timeout: float = 10.0) -> None:
        self._base = cfg.url.rstrip("/")
        self._api_key = cfg.api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def base_url(self) -> str:
        return self._base

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- Auto-Start ------------------------------------------------------

    async def is_reachable(self, *, timeout: float = 1.0) -> bool:
        """TCP-Probe auf den konfigurierten SAB-Host. Schnell und ohne API-Key."""
        if not self._base:
            return False
        u = urlparse(self._base)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass
        return True

    async def ensure_running(
        self,
        *,
        bin_path: str = "sabnzbdplus",
        startup_timeout: float = 20.0,
    ) -> bool:
        """Startet sabnzbdplus als detachten Hintergrundprozess, falls nicht erreichbar.

        Idempotent: läuft SAB schon, no-op. Liefert True wenn SAB nach der
        Aktion antwortet, False wenn Binary fehlt oder Startup-Timeout reißt.
        SAB läuft bewusst weiter, wenn die App schließt — Downloads sollen
        im Hintergrund weiterlaufen.
        """
        if await self.is_reachable():
            return True

        log.info("SAB nicht erreichbar – starte %s --daemon", bin_path)
        try:
            # --daemon lässt SAB sich selbst doppel-forken, der ursprüngliche
            # Prozess exitet sofort. Wir wait()en darauf, damit asyncio den
            # Zombie reapen kann; der echte SAB-Daemon lebt unter neuer PID
            # weiter, auch wenn diese App schließt.
            proc = await asyncio.create_subprocess_exec(
                bin_path,
                "--daemon",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await proc.wait()
        except FileNotFoundError:
            log.warning("SAB-Binary nicht gefunden: %s (in PATH?)", bin_path)
            return False

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if await self.is_reachable():
                log.info("SAB ist hochgefahren")
                return True
        log.warning("SAB nach %.0fs nicht erreichbar", startup_timeout)
        return False

    # ---- Low-Level ------------------------------------------------------

    async def _call(
        self,
        mode: str,
        *,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
    ) -> dict:
        if not self._api_key:
            raise SABError("Kein SABnzbd-API-Key konfiguriert (config.toml [sabnzbd].api_key)")
        q = {"mode": mode, "output": "json", "apikey": self._api_key}
        if params:
            q.update(params)
        url = f"{self._base}/api"
        try:
            if files is not None:
                # Multipart-Upload (addfile). Form-Daten können via data
                # zusätzlich beigemischt werden.
                form = dict(q)
                if data:
                    form.update(data)
                resp = await self._client.post(url, data=form, files=files)
            else:
                resp = await self._client.get(url, params=q)
        except httpx.HTTPError as exc:
            raise SABError(f"HTTP-Fehler: {exc}") from exc

        if resp.status_code >= 400:
            raise SABError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError:
            raise SABError(f"Antwort ist kein JSON: {resp.text[:200]}")

        # SAB liefert bei Fehlern z.B. {"status": false, "error": "..."}
        if isinstance(payload, dict) and payload.get("status") is False:
            raise SABError(payload.get("error", "unbekannter Fehler"))
        return payload

    # ---- High-Level -----------------------------------------------------

    async def version(self) -> str:
        # `version` braucht eigentlich keinen api_key, aber wir gehen
        # konsistent vor. Antwort: {"version": "4.5.4"}
        payload = await self._call("version")
        return str(payload.get("version", "?"))

    async def queue(self) -> QueueSnapshot:
        payload = await self._call("queue")
        q = payload.get("queue", {})
        slots = [
            QueueSlot(
                nzo_id=s.get("nzo_id", ""),
                filename=s.get("filename", ""),
                status=s.get("status", ""),
                size_mb=_to_float(s.get("mb", 0)),
                sizeleft_mb=_to_float(s.get("mbleft", 0)),
                percentage=int(_to_float(s.get("percentage", 0))),
                eta=s.get("timeleft", "") or s.get("eta", ""),
            )
            for s in q.get("slots", [])
        ]
        return QueueSnapshot(
            paused=bool(q.get("paused", False)),
            speed=str(q.get("speed", "")),
            size_mb=_to_float(q.get("mb", 0)),
            sizeleft_mb=_to_float(q.get("mbleft", 0)),
            slots=slots,
        )

    async def add_nzb_bytes(
        self,
        nzb_bytes: bytes,
        *,
        filename: str,
        category: str | None = None,
        priority: int | None = None,
    ) -> str:
        params: dict = {"nzbname": filename}
        if category:
            params["cat"] = category
        if priority is not None:
            params["priority"] = str(priority)
        files = {"name": (filename, nzb_bytes, "application/x-nzb")}
        payload = await self._call("addfile", params=params, files=files)
        # Antwort: {"status": true, "nzo_ids": ["SABnzbd_nzo_xxx"]}
        ids = payload.get("nzo_ids") or []
        if not ids:
            raise SABError(f"SAB hat keine nzo_id zurückgegeben: {payload}")
        return ids[0]

    async def pause(self, nzo_id: str) -> None:
        await self._call("queue", params={"name": "pause", "value": nzo_id})

    async def resume(self, nzo_id: str) -> None:
        await self._call("queue", params={"name": "resume", "value": nzo_id})

    async def delete(self, nzo_id: str, *, delete_files: bool = True) -> None:
        await self._call(
            "queue",
            params={
                "name": "delete",
                "value": nzo_id,
                "del_files": "1" if delete_files else "0",
            },
        )


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
