from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core import header_cache
from core.release_grouper import ReleaseRow, group_releases

log = logging.getLogger(__name__)

_SEARCH_LIMIT = 5000

_COL_CHECK, _COL_SUBJECT, _COL_FROM, _COL_DATE, _COL_SEGS, _COL_BYTES = range(6)
_COLUMNS = ("✓", "Subject", "From", "Datum", "Segmente", "Bytes")

# Eigene Sort-Rolle, damit Datum/Bytes/Segmente numerisch sortieren statt
# lexikalisch auf der Display-String-Variante.
_SORT_ROLE = Qt.ItemDataRole.UserRole + 1


@dataclass
class _Mode:
    kind: str  # "browse" | "search"
    group: str | None
    query: str | None = None


class HeaderModel(QAbstractTableModel):
    marks_changed = Signal(int)

    def __init__(self, cache: header_cache.HeaderCache, parent=None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._mode: _Mode = _Mode(kind="browse", group=None)
        self._releases: list[ReleaseRow] = []
        # Markierungen überleben Mode-Wechsel. Schlüssel: (group, release_key).
        self._marks: set[tuple[str, str]] = set()

    # ---- Mode-Switching --------------------------------------------------

    def set_group(self, group: str | None) -> None:
        self.beginResetModel()
        self._mode = _Mode(kind="browse", group=group)
        if group:
            articles = self._cache.fetch_articles(group, limit=0, order="desc")
            self._releases = group_releases(articles)
        else:
            self._releases = []
        self.endResetModel()

    def set_search(self, group: str | None, query: str) -> None:
        self.beginResetModel()
        self._mode = _Mode(kind="search", group=group, query=query)
        try:
            articles = self._cache.search_articles(
                query, group=group, limit=_SEARCH_LIMIT
            )
        except Exception as exc:
            log.warning("FTS-Query fehlgeschlagen: %s", exc)
            articles = []
        self._releases = group_releases(articles)
        self.endResetModel()

    def current_group(self) -> str | None:
        return self._mode.group

    # ---- Markierungen ---------------------------------------------------

    def marked_articles(self) -> list[header_cache.ArticleRow]:
        """Alle Artikel der markierten Releases einsammeln.

        Reihenfolge: Release-Reihenfolge (DESC im Cache → neueste zuerst),
        innerhalb eines Releases die Artikel-Reihenfolge aus dem Grouper.
        Der NZB-Builder ordnet anschließend nochmal nach Subject-Stamm
        und Part-Nummer.
        """
        out: list[header_cache.ArticleRow] = []
        for r in self._releases:
            if r.articles and self._mark_key(r) in self._marks:
                out.extend(r.articles)
        return out

    def mark_count(self) -> int:
        return len(self._marks)

    def clear_marks(self) -> None:
        if not self._marks:
            return
        self._marks.clear()
        if self._releases:
            top = self.index(0, _COL_CHECK)
            bottom = self.index(len(self._releases) - 1, _COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.CheckStateRole])
        self.marks_changed.emit(0)

    # ---- Standard-Model-API ---------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._releases)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _COLUMNS[section]
        return section + 1

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _COL_CHECK:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._releases[index.row()]
        col = index.column()
        mark_key = self._mark_key(r)

        if role == Qt.ItemDataRole.CheckStateRole and col == _COL_CHECK:
            return Qt.CheckState.Checked if mark_key in self._marks else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_SUBJECT: return r.subject
            if col == _COL_FROM: return r.poster
            if col == _COL_DATE: return _fmt_date(r.date_unix)
            if col == _COL_SEGS: return _fmt_segments(r)
            if col == _COL_BYTES: return _fmt_bytes(r.total_bytes)
        elif role == _SORT_ROLE:
            if col == _COL_SUBJECT: return r.subject
            if col == _COL_FROM: return r.poster
            if col == _COL_DATE: return r.date_unix
            if col == _COL_SEGS: return r.segments_have
            if col == _COL_BYTES: return r.total_bytes
            if col == _COL_CHECK:
                return 1 if mark_key in self._marks else 0
        elif role == Qt.ItemDataRole.ToolTipRole:
            files = r.file_count or 1
            return (
                f"{files} Datei(en), {r.segments_have} Artikel\n"
                f"Erstes Subject: {r.articles[0].subject if r.articles else ''}"
            )
        elif role == Qt.ItemDataRole.UserRole:
            return r
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if (
            not index.isValid()
            or role != Qt.ItemDataRole.CheckStateRole
            or index.column() != _COL_CHECK
        ):
            return False
        r = self._releases[index.row()]
        mark_key = self._mark_key(r)
        # PySide reicht hier sowohl Qt.CheckState als auch int durch.
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        if checked:
            self._marks.add(mark_key)
        else:
            self._marks.discard(mark_key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.marks_changed.emit(len(self._marks))
        return True

    @staticmethod
    def _mark_key(r: ReleaseRow) -> tuple[str, str]:
        group = r.articles[0].group if r.articles else ""
        return (group, r.key)

    def release_at(self, row: int) -> ReleaseRow | None:
        if 0 <= row < len(self._releases):
            return self._releases[row]
        return None

    def total_releases(self) -> int:
        return len(self._releases)

    def total_articles(self) -> int:
        return sum(r.segments_have for r in self._releases)


def _fmt_bytes(n: int) -> str:
    if n <= 0:
        return ""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _fmt_date(unix_ts: int) -> str:
    if unix_ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _fmt_segments(r: ReleaseRow) -> str:
    if r.segments_expected and r.segments_expected != r.segments_have:
        return f"{r.segments_have}/{r.segments_expected}"
    if r.segments_expected:
        return f"{r.segments_have}/{r.segments_expected}"
    return str(r.segments_have)


class HeaderView(QWidget):
    release_activated = Signal(object)  # ReleaseRow
    save_nzb_requested = Signal(list)   # list[ArticleRow]
    submit_sab_requested = Signal(list)  # list[ArticleRow]

    def __init__(self, cache: header_cache.HeaderCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._model = HeaderModel(cache, self)
        self._model.marks_changed.connect(self._on_marks_changed)
        self._build_ui()
        self._on_marks_changed(0)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Suche:"))
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("FTS5-Query (subject + from). Enter sucht.")
        self._search.returnPressed.connect(self._run_search)
        search_row.addWidget(self._search, 1)
        self._btn_search = QPushButton("Suchen", self)
        self._btn_search.clicked.connect(self._run_search)
        self._btn_clear = QPushButton("Zurücksetzen", self)
        self._btn_clear.clicked.connect(self._clear_search)
        search_row.addWidget(self._btn_search)
        search_row.addWidget(self._btn_clear)
        layout.addLayout(search_row)

        self._table = QTableView(self)
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self._model)
        proxy.setSortRole(_SORT_ROLE)
        self._table.setModel(proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_SUBJECT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_COL_FROM, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_SEGS, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_BYTES, QHeaderView.ResizeMode.ResizeToContents)
        self._table.doubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self._table, 1)

        action_row = QHBoxLayout()
        self._mark_status = QLabel("0 markiert", self)
        action_row.addWidget(self._mark_status)
        action_row.addStretch(1)
        self._btn_clear_marks = QPushButton("Auswahl leeren", self)
        self._btn_clear_marks.clicked.connect(self._model.clear_marks)
        self._btn_save_nzb = QPushButton("NZB speichern…", self)
        self._btn_save_nzb.clicked.connect(self._on_save_nzb)
        self._btn_submit_sab = QPushButton("→ SABnzbd", self)
        self._btn_submit_sab.clicked.connect(self._on_submit_sab)
        action_row.addWidget(self._btn_clear_marks)
        action_row.addWidget(self._btn_save_nzb)
        action_row.addWidget(self._btn_submit_sab)
        layout.addLayout(action_row)

        self._status = QLabel("", self)
        self._status.setStyleSheet("color: #888;")
        layout.addWidget(self._status)

    def set_group(self, group: str | None) -> None:
        self._search.clear()
        self._model.set_group(group)
        self._update_status()

    def refresh_current(self) -> None:
        if self._model.current_group():
            self.set_group(self._model.current_group())

    def model(self) -> HeaderModel:
        return self._model

    def table(self) -> QTableView:
        return self._table

    def next_row(self) -> None:
        self._step(+1)

    def prev_row(self) -> None:
        self._step(-1)

    def _step(self, delta: int) -> None:
        proxy = self._table.model()
        rows = proxy.rowCount()
        if rows == 0:
            return
        cur = self._table.currentIndex()
        target = cur.row() + delta if cur.isValid() else (0 if delta > 0 else rows - 1)
        target = max(0, min(rows - 1, target))
        idx = proxy.index(target, _COL_SUBJECT)
        self._table.setCurrentIndex(idx)
        self._table.scrollTo(idx)
        # Body laden: ganzes Release an die ArticleView.
        src = proxy.mapToSource(idx)
        release = self._model.release_at(src.row())
        if release and release.articles:
            self.release_activated.emit(release)

    def toggle_mark_current(self) -> None:
        proxy = self._table.model()
        cur = self._table.currentIndex()
        if not cur.isValid():
            return
        check_idx = proxy.index(cur.row(), _COL_CHECK)
        src = proxy.mapToSource(check_idx)
        current = self._model.data(src, Qt.ItemDataRole.CheckStateRole)
        new_state = (
            Qt.CheckState.Unchecked
            if current == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self._model.setData(src, new_state, Qt.ItemDataRole.CheckStateRole)
        # Cursor auf nächste Zeile (J/Space-Workflow)
        self._step(+1)

    def _run_search(self) -> None:
        query = self._search.text().strip()
        if not query:
            self._clear_search()
            return
        self._model.set_search(self._model.current_group(), query)
        self._update_status(searching=True, query=query)

    def _clear_search(self) -> None:
        self._search.clear()
        self._model.set_group(self._model.current_group())
        self._update_status()

    def _update_status(self, searching: bool = False, query: str = "") -> None:
        grp = self._model.current_group()
        if not grp:
            self._status.setText("Keine Gruppe ausgewählt")
            return
        rel = self._model.total_releases()
        art = self._model.total_articles()
        if searching:
            self._status.setText(
                f"Suche {query!r} in {grp}: {rel} Releases ({art} Artikel)"
            )
        else:
            self._status.setText(
                f"{grp}: {rel} Releases ({art} Artikel im Cache)"
            )

    def _on_marks_changed(self, count: int) -> None:
        self._mark_status.setText(f"{count} markiert")
        self._btn_save_nzb.setEnabled(count > 0)
        self._btn_submit_sab.setEnabled(count > 0)
        self._btn_clear_marks.setEnabled(count > 0)

    def _on_double_clicked(self, proxy_index) -> None:
        proxy = self._table.model()
        src_index = proxy.mapToSource(proxy_index)
        # Doppelklick auf die Check-Spalte schaltet Häkchen, nicht Body
        if src_index.column() == _COL_CHECK:
            return
        release = self._model.release_at(src_index.row())
        if release and release.articles:
            self.release_activated.emit(release)

    def _on_save_nzb(self) -> None:
        marked = self._model.marked_articles()
        if marked:
            self.save_nzb_requested.emit(marked)

    def _on_submit_sab(self) -> None:
        marked = self._model.marked_articles()
        if marked:
            self.submit_sab_requested.emit(marked)
