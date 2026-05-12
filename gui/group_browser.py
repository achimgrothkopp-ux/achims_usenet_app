"""Group-Browser: flache Liste aller bekannten Newsgroups mit Filter
und Mehrfach-Subscribe.

Lädt initial den DB-Cache (groups-Tabelle) und kann per "Aktualisieren"
ein LIST ACTIVE vom Server ziehen und in die DB schreiben – ab da
auch ohne Server arbeitbar.

Bewusst flach statt Hierarchie-Tree: bei ~100k Gruppen liefert ein
einfacher String-Filter schneller das gesuchte Ergebnis, als sich
durch alt.* / de.* / comp.* zu klicken. Sortierung nach Name clustert
Hierarchien ohnehin alphabetisch.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core import header_cache, nntp_client

log = logging.getLogger(__name__)


_COL_NAME, _COL_COUNT, _COL_STATUS, _COL_SUB = range(4)
_COLUMNS = ("Gruppe", "≈ Artikel", "Status", "Abonniert")
_SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class _GroupModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[header_cache.GroupRow] = []

    def set_rows(self, rows: list[header_cache.GroupRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_NAME: return row.name
            if col == _COL_COUNT:
                count = max(0, row.high - row.low + 1) if row.high else 0
                return f"{count:,}" if count else ""
            if col == _COL_STATUS: return row.status or ""
            if col == _COL_SUB: return "ja" if row.subscribed else ""
        elif role == _SORT_ROLE:
            if col == _COL_NAME: return row.name
            if col == _COL_COUNT: return max(0, row.high - row.low + 1) if row.high else 0
            if col == _COL_STATUS: return row.status or ""
            if col == _COL_SUB: return 1 if row.subscribed else 0
        elif role == Qt.ItemDataRole.UserRole:
            return row
        return None

    def name_at(self, source_row: int) -> str | None:
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row].name
        return None


class GroupBrowser(QDialog):
    subscribed_changed = Signal()  # emittiert wenn neue Gruppen abonniert wurden

    def __init__(
        self,
        cache: header_cache.HeaderCache,
        pool: nntp_client.NNTPPool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        self._pool = pool
        self._changed = False  # ob wir subscribed_changed nochmal feuern müssen

        self.setWindowTitle("Gruppen-Browser")
        self.resize(720, 540)
        self._build_ui()
        self._reload_from_cache()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Top-Bar: Filter + Refresh + Status
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Filter:", self))
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("z.B. alt.binaries oder de.alt")
        self._filter.textChanged.connect(self._on_filter_changed)
        bar.addWidget(self._filter, 1)
        self._btn_refresh = QPushButton("Aktualisieren (LIST ACTIVE)", self)
        self._btn_refresh.setToolTip("Komplette Gruppenliste vom Server holen (kann ~5–10 MB sein)")
        self._btn_refresh.clicked.connect(self._on_refresh_clicked)
        bar.addWidget(self._btn_refresh)
        layout.addLayout(bar)

        self._lbl_status = QLabel("", self)
        layout.addWidget(self._lbl_status)

        # Table
        self._model = _GroupModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(_SORT_ROLE)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Filter sucht in Name-Spalte
        self._proxy.setFilterKeyColumn(_COL_NAME)

        self._view = QTableView(self)
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(_COL_NAME, Qt.SortOrder.AscendingOrder)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)
        hh = self._view.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_COL_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_SUB, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._view, 1)

        # Footer-Buttons
        foot = QHBoxLayout()
        self._chk_only_subscribed = QCheckBox("Nur Abonnierte zeigen", self)
        self._chk_only_subscribed.toggled.connect(self._reload_from_cache)
        foot.addWidget(self._chk_only_subscribed)
        foot.addStretch(1)
        self._btn_subscribe = QPushButton("Markierte abonnieren", self)
        self._btn_subscribe.clicked.connect(self._on_subscribe_clicked)
        self._btn_unsubscribe = QPushButton("Abbestellen", self)
        self._btn_unsubscribe.clicked.connect(self._on_unsubscribe_clicked)
        self._btn_close = QPushButton("Schließen", self)
        self._btn_close.clicked.connect(self.accept)
        foot.addWidget(self._btn_subscribe)
        foot.addWidget(self._btn_unsubscribe)
        foot.addWidget(self._btn_close)
        layout.addLayout(foot)

    # ---- Daten -----------------------------------------------------------

    def _reload_from_cache(self) -> None:
        rows = self._cache.list_all_groups()
        if self._chk_only_subscribed.isChecked():
            rows = [r for r in rows if r.subscribed]
        self._model.set_rows(rows)
        self._update_status(rows)

    def _update_status(self, rows: list[header_cache.GroupRow]) -> None:
        sub = sum(1 for r in rows if r.subscribed)
        self._lbl_status.setText(f"{len(rows):,} Gruppen im Cache · {sub:,} abonniert")

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text.strip())

    # ---- Server-Refresh --------------------------------------------------

    def _on_refresh_clicked(self) -> None:
        asyncio.ensure_future(self._refresh_async())

    async def _refresh_async(self) -> None:
        self._btn_refresh.setEnabled(False)
        self._lbl_status.setText("Lade LIST ACTIVE vom Server …")
        try:
            try:
                groups = await self._pool.list_active()
            except Exception as exc:
                log.exception("list_active fehlgeschlagen")
                self._lbl_status.setText(f"LIST ACTIVE fehlgeschlagen: {exc}")
                return
            payload = [(g.name, int(g.low), int(g.high), str(g.status) if g.status else None)
                       for g in groups]
            n = await asyncio.to_thread(self._cache.upsert_groups_bulk, payload)
            log.info("LIST ACTIVE: %d Gruppen vom Server, %d in DB getouched", len(groups), n)
            self._reload_from_cache()
        finally:
            self._btn_refresh.setEnabled(True)

    # ---- Subscribe-Actions ----------------------------------------------

    def _selected_names(self) -> list[str]:
        out: list[str] = []
        for idx in self._view.selectionModel().selectedRows(_COL_NAME):
            src = self._proxy.mapToSource(idx)
            name = self._model.name_at(src.row())
            if name:
                out.append(name)
        return out

    def _on_subscribe_clicked(self) -> None:
        names = self._selected_names()
        if not names:
            return
        for n in names:
            self._cache.set_subscribed(n, True)
        log.info("Abonniert via Browser: %s", names)
        self._changed = True
        self.subscribed_changed.emit()
        self._reload_from_cache()

    def _on_unsubscribe_clicked(self) -> None:
        names = self._selected_names()
        if not names:
            return
        for n in names:
            self._cache.set_subscribed(n, False)
        log.info("Abbestellt via Browser: %s", names)
        self._changed = True
        self.subscribed_changed.emit()
        self._reload_from_cache()

    # ---- Static-Factory --------------------------------------------------

    @staticmethod
    def show_for(
        parent: QWidget | None,
        cache: header_cache.HeaderCache,
        pool: nntp_client.NNTPPool,
    ) -> "GroupBrowser":
        """Non-modal in qasync-Loop integrierte Variante.

        Liefert den Dialog selbst zurück, damit der Aufrufer ggf. ein
        subscribed_changed-Signal abonnieren kann. show()-style, kein await.
        """
        dlg = GroupBrowser(cache, pool, parent=parent)
        dlg.setModal(True)
        dlg.show()
        return dlg
