from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import header_cache, nntp_client
from gui.dialogs import confirm_async, warn_later

log = logging.getLogger(__name__)

SUBSCRIBE_WARN_THRESHOLD = 1_000_000


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
        name, ok = QInputDialog.getText(
            self,
            "Gruppe abonnieren",
            "Gruppen-Name (z.B. de.alt.test):",
        )
        if not ok or not name.strip():
            return
        asyncio.ensure_future(self._subscribe_async(name.strip()))

    async def _subscribe_async(self, name: str) -> None:
        try:
            count, low, high = await self._pool.group_info(name)
        except Exception as exc:
            log.warning("Subscribe %s fehlgeschlagen: %s", name, exc)
            warn_later(self, "Subscribe", f"Server kennt {name!r} nicht:\n{exc}")
            return

        if count > SUBSCRIBE_WARN_THRESHOLD:
            est_min = count / 200_000
            est_gb = count * 0.7 / 1_000_000
            ok = await confirm_async(
                self,
                "Große Gruppe",
                f"{name} hat {count:,} Artikel.\n\n"
                f"Vollsync würde geschätzt ~{est_min:,.0f} Minuten brauchen "
                f"und ~{est_gb:,.1f} GB Cache produzieren.\n\n"
                "Trotzdem abonnieren? (Du kannst danach selbst entscheiden, "
                "ob du syncen willst.)",
            )
            if not ok:
                self._statusbar_msg("Abo abgebrochen")
                return

        await asyncio.to_thread(self._cache.upsert_group, name, low, high, None, None)
        await asyncio.to_thread(self._cache.set_subscribed, name, True)
        log.info("Abonniert: %s (low=%s high=%s, %s Artikel)", name, low, high, count)
        self.refresh()

    def _statusbar_msg(self, text: str) -> None:
        # GroupPanel kennt das MainWindow nicht direkt; wir holen uns den
        # nächstgelegenen QMainWindow-Vorfahren, falls vorhanden.
        from PySide6.QtWidgets import QMainWindow
        w = self.parent()
        while w is not None and not isinstance(w, QMainWindow):
            w = w.parent()
        if isinstance(w, QMainWindow):
            w.statusBar().showMessage(text, 3000)

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
