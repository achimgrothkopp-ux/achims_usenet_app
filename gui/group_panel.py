from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import header_cache, nntp_client
from gui.group_browser import GroupBrowser

log = logging.getLogger(__name__)


class GroupPanel(QWidget):
    group_selected = Signal(str)
    sync_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(
        self,
        cache: header_cache.HeaderCache,
        pool: nntp_client.NNTPPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        self._pool = pool
        self._syncing: set[str] = set()
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._list = QListWidget(self)
        self._list.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._btn_subscribe = QPushButton("Abonnieren…", self)
        self._btn_subscribe.clicked.connect(self._on_subscribe_clicked)
        self._btn_unsubscribe = QPushButton("Abbestellen", self)
        self._btn_unsubscribe.clicked.connect(self._on_unsubscribe_clicked)
        self._btn_sync = QPushButton("Sync…", self)
        self._btn_sync.clicked.connect(self._on_sync_clicked)
        btn_row.addWidget(self._btn_subscribe)
        btn_row.addWidget(self._btn_unsubscribe)
        btn_row.addWidget(self._btn_sync)
        layout.addLayout(btn_row)

    def refresh(self) -> None:
        current = self.current_group()
        self._list.clear()
        for grp in self._cache.list_subscribed():
            label = grp.name + (" · sync läuft" if grp.name in self._syncing else "")
            item = QListWidgetItem(label, self._list)
            tip = (
                f"low={grp.low:,}  high={grp.high:,}\n"
                f"last_seen={grp.last_seen:,}"
            )
            item.setToolTip(tip)
            item.setData(Qt.ItemDataRole.UserRole, grp.name)
        if current:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == current:
                    self._list.setCurrentRow(i)
                    break
        self._update_buttons()

    def current_group(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def set_sync_running(self, name: str, running: bool) -> None:
        if running:
            self._syncing.add(name)
        else:
            self._syncing.discard(name)
        self.refresh()

    def _update_buttons(self) -> None:
        cur = self.current_group()
        running = cur is not None and cur in self._syncing
        self._btn_sync.setEnabled(cur is not None)
        self._btn_sync.setText("Stop" if running else "Sync…")
        self._btn_sync.setToolTip(
            "Laufenden Sync abbrechen (Fortschritt bleibt erhalten)"
            if running
            else "Sync-Dialog öffnen"
        )
        self._btn_unsubscribe.setEnabled(cur is not None and not running)

    def _on_selection(self) -> None:
        self._update_buttons()
        name = self.current_group()
        if name:
            self.group_selected.emit(name)

    def _on_subscribe_clicked(self) -> None:
        # Browser bietet Filter, Mehrfach-Subscribe und LIST-ACTIVE-Refresh.
        # show_for hält den Dialog am Leben (non-modal); subscribed_changed
        # → unsere refresh() für die linke Panel-Liste.
        self._browser = GroupBrowser.show_for(self, self._cache, self._pool)
        self._browser.subscribed_changed.connect(self.refresh)

    def _on_unsubscribe_clicked(self) -> None:
        name = self.current_group()
        if not name:
            return
        if QMessageBox.question(
            self, "Abbestellen",
            f"{name} wirklich abbestellen? (Header bleiben im Cache.)"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._cache.set_subscribed(name, False)
        self.refresh()

    def _on_sync_clicked(self) -> None:
        name = self.current_group()
        if not name:
            return
        if name in self._syncing:
            self.cancel_requested.emit(name)
        else:
            self.sync_requested.emit(name)
