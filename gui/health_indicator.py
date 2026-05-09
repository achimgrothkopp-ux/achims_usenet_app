from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QWidget

from backend.sabnzbd import SABError, SABnzbdClient

log = logging.getLogger(__name__)

POLL_MS = 5000


class HealthIndicator(QLabel):
    """Statusbar-Widget mit grün/rot/grau-Punkt für SABnzbd-Verbindung."""

    def __init__(self, sab: SABnzbdClient | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sab = sab
        self._task: asyncio.Task | None = None
        self.setMargin(2)
        self._set_state(state="unknown", info="")
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if self._sab is None:
            self._set_state("disabled", "Kein SAB-Client")
            return
        if not self._sab.configured:
            self._set_state(
                "disabled",
                "Kein API-Key in config.toml [sabnzbd].api_key",
            )
            return
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        if self._sab is None or not self._sab.configured:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._probe())

    async def _probe(self) -> None:
        assert self._sab is not None
        try:
            version = await self._sab.version()
        except SABError as exc:
            self._set_state("down", str(exc))
            return
        except Exception as exc:
            self._set_state("down", repr(exc))
            return
        self._set_state("up", f"SABnzbd v{version}")

    def _set_state(self, state: str, info: str) -> None:
        if state == "up":
            self.setText("● SAB")
            self.setStyleSheet("color: #2ecc71; font-weight: bold;")
        elif state == "down":
            self.setText("● SAB")
            self.setStyleSheet("color: #e74c3c; font-weight: bold;")
        elif state == "disabled":
            self.setText("○ SAB")
            self.setStyleSheet("color: #888;")
        else:
            self.setText("◌ SAB")
            self.setStyleSheet("color: #888;")
        self.setToolTip(f"SABnzbd-Status: {state}\n{info}" if info else f"SABnzbd-Status: {state}")
