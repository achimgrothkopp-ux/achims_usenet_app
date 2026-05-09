from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from backend.sabnzbd import QueueSlot, QueueSnapshot, SABError, SABnzbdClient

log = logging.getLogger(__name__)

POLL_MS = 2000

_COL_NAME, _COL_STATUS, _COL_SIZE, _COL_LEFT, _COL_PROGRESS, _COL_ETA = range(6)
_COLUMNS = ("Name", "Status", "Größe", "verbleibend", "%", "ETA")


class QueueModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._slots: list[QueueSlot] = []

    def replace(self, slots: list[QueueSlot]) -> None:
        self.beginResetModel()
        self._slots = list(slots)
        self.endResetModel()

    def slot_at(self, row: int) -> QueueSlot | None:
        if 0 <= row < len(self._slots):
            return self._slots[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._slots)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        slot = self._slots[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_NAME: return slot.filename
            if col == _COL_STATUS: return slot.status
            if col == _COL_SIZE: return _fmt_mb(slot.size_mb)
            if col == _COL_LEFT: return _fmt_mb(slot.sizeleft_mb)
            if col == _COL_PROGRESS: return f"{slot.percentage}%"
            if col == _COL_ETA: return slot.eta
        elif role == Qt.ItemDataRole.ToolTipRole:
            return slot.nzo_id
        elif role == Qt.ItemDataRole.UserRole:
            return slot
        return None


def _fmt_mb(mb: float) -> str:
    if mb < 0.05:
        return ""
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


class QueuePanel(QWidget):
    def __init__(
        self,
        sab: SABnzbdClient | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sab = sab
        self._model = QueueModel(self)
        self._poll_task: asyncio.Task | None = None
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)
        if self._sab is not None and self._sab.configured:
            self._timer.start()
            self._tick()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("<b>SABnzbd-Queue</b>", self))
        self._summary = QLabel("", self)
        self._summary.setStyleSheet("color: #888;")
        title_row.addWidget(self._summary, 1)
        title_row.addStretch(1)
        self._btn_pause = QPushButton("Pause", self)
        self._btn_pause.clicked.connect(self._on_pause)
        self._btn_resume = QPushButton("Resume", self)
        self._btn_resume.clicked.connect(self._on_resume)
        self._btn_delete = QPushButton("Löschen", self)
        self._btn_delete.clicked.connect(self._on_delete)
        for b in (self._btn_pause, self._btn_resume, self._btn_delete):
            b.setEnabled(False)
            title_row.addWidget(b)
        layout.addLayout(title_row)

        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        for c in (_COL_STATUS, _COL_SIZE, _COL_LEFT, _COL_PROGRESS, _COL_ETA):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        layout.addWidget(self._table, 1)

    # ---- Public ---------------------------------------------------------

    def table(self) -> QTableView:
        return self._table

    def trigger_refresh(self) -> None:
        """Ruft auch außerhalb des Timers eine sofortige Aktualisierung ab."""
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    # ---- Polling --------------------------------------------------------

    def _tick(self) -> None:
        if self._sab is None or not self._sab.configured:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return  # vorheriger Poll läuft noch
        self._poll_task = asyncio.ensure_future(self._poll_async())

    async def _poll_async(self) -> None:
        assert self._sab is not None
        try:
            snap: QueueSnapshot = await self._sab.queue()
        except SABError as exc:
            self._summary.setText(f"Queue-Fehler: {exc}")
            return
        except Exception:
            log.exception("Queue-Poll fehlgeschlagen")
            return

        self._model.replace(snap.slots)
        paused = "PAUSE · " if snap.paused else ""
        speed = snap.speed or "0"
        self._summary.setText(
            f"{paused}{speed}/s · {len(snap.slots)} Jobs · "
            f"{_fmt_mb(snap.sizeleft_mb)} / {_fmt_mb(snap.size_mb)} offen"
        )

    # ---- Aktionen -------------------------------------------------------

    def _selected_slot(self) -> QueueSlot | None:
        idx = self._table.currentIndex()
        if not idx.isValid():
            return None
        return self._model.slot_at(idx.row())

    def _on_selection(self, *_args) -> None:
        slot = self._selected_slot()
        for b in (self._btn_pause, self._btn_resume, self._btn_delete):
            b.setEnabled(slot is not None)

    def _on_pause(self) -> None:
        slot = self._selected_slot()
        if slot:
            asyncio.ensure_future(self._action(self._sab.pause, slot.nzo_id))

    def _on_resume(self) -> None:
        slot = self._selected_slot()
        if slot:
            asyncio.ensure_future(self._action(self._sab.resume, slot.nzo_id))

    def _on_delete(self) -> None:
        slot = self._selected_slot()
        if slot:
            asyncio.ensure_future(self._action(self._sab.delete, slot.nzo_id))

    async def _action(self, fn, nzo_id: str) -> None:
        try:
            await fn(nzo_id)
        except SABError as exc:
            log.warning("SAB-Aktion fehlgeschlagen: %s", exc)
        finally:
            self._tick()
