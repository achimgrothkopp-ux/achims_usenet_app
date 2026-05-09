from __future__ import annotations

import logging
from dataclasses import dataclass

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

log = logging.getLogger(__name__)

_PAGE_SIZE = 500
_SEARCH_LIMIT = 1000

_COL_CHECK, _COL_NUMBER, _COL_SUBJECT, _COL_FROM, _COL_DATE, _COL_BYTES = range(6)
_COLUMNS = ("✓", "Nr.", "Subject", "From", "Datum", "Bytes")


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
        self._rows: list[header_cache.ArticleRow] = []
        self._total = 0
        # Markierungen überleben Mode-Wechsel: Schlüssel (group, number).
        self._marks: set[tuple[str, int]] = set()

    # ---- Mode-Switching --------------------------------------------------

    def set_group(self, group: str | None) -> None:
        self.beginResetModel()
        self._mode = _Mode(kind="browse", group=group)
        self._rows = []
        self._total = self._cache.article_count(group) if group else 0
        self.endResetModel()
        self._fill_initial()

    def set_search(self, group: str | None, query: str) -> None:
        self.beginResetModel()
        self._mode = _Mode(kind="search", group=group, query=query)
        self._rows = []
        self._total = 0
        try:
            hits = self._cache.search(query, group=group, limit=_SEARCH_LIMIT)
        except Exception as exc:
            log.warning("FTS-Query fehlgeschlagen: %s", exc)
            hits = []

        rows: list[header_cache.ArticleRow] = []
        for hit in hits:
            row = self._cache.get_article(hit.group, hit.number)
            if row is not None:
                rows.append(row)
        self._rows = rows
        self._total = len(rows)
        self.endResetModel()

    def current_group(self) -> str | None:
        return self._mode.group

    # ---- Lazy-Fetch (browse-Modus) --------------------------------------

    def _fill_initial(self) -> None:
        if self._mode.kind != "browse" or not self._mode.group or self._total == 0:
            return
        page = self._cache.fetch_articles(
            self._mode.group, offset=0, limit=_PAGE_SIZE, order="desc"
        )
        if not page:
            return
        self.beginInsertRows(QModelIndex(), 0, len(page) - 1)
        self._rows = page
        self.endInsertRows()

    def canFetchMore(self, parent: QModelIndex) -> bool:
        if parent.isValid() or self._mode.kind != "browse":
            return False
        return len(self._rows) < self._total

    def fetchMore(self, parent: QModelIndex) -> None:
        if parent.isValid() or self._mode.kind != "browse" or not self._mode.group:
            return
        offset = len(self._rows)
        page = self._cache.fetch_articles(
            self._mode.group, offset=offset, limit=_PAGE_SIZE, order="desc"
        )
        if not page:
            return
        self.beginInsertRows(QModelIndex(), offset, offset + len(page) - 1)
        self._rows.extend(page)
        self.endInsertRows()

    # ---- Markierungen ---------------------------------------------------

    def marked_articles(self) -> list[header_cache.ArticleRow]:
        """Alle markierten Artikel aus dem Cache holen, in stabiler Reihenfolge.

        Wir arbeiten direkt mit dem Cache statt mit den geladenen
        Pagen, weil Markierungen Mode-übergreifend gehalten werden und
        Rows in der UI evtl. noch gar nicht materialisiert sind.
        """
        out: list[header_cache.ArticleRow] = []
        for group, number in sorted(self._marks):
            row = self._cache.get_article(group, number)
            if row is not None:
                out.append(row)
        return out

    def mark_count(self) -> int:
        return len(self._marks)

    def clear_marks(self) -> None:
        if not self._marks:
            return
        self._marks.clear()
        # Alle sichtbaren Zeilen neu zeichnen (CheckStateRole)
        if self._rows:
            top = self.index(0, _COL_CHECK)
            bottom = self.index(len(self._rows) - 1, _COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.CheckStateRole])
        self.marks_changed.emit(0)

    # ---- Standard-Model-API ---------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

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
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == _COL_CHECK:
            key = (row.group, row.number)
            return Qt.CheckState.Checked if key in self._marks else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_NUMBER: return row.number
            if col == _COL_SUBJECT: return row.subject
            if col == _COL_FROM: return row.from_addr
            if col == _COL_DATE: return row.date
            if col == _COL_BYTES: return _fmt_bytes(row.bytes)
        elif role == Qt.ItemDataRole.ToolTipRole:
            return f"Message-ID: {row.message_id}\nLines: {row.lines}"
        elif role == Qt.ItemDataRole.UserRole:
            return row
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if (
            not index.isValid()
            or role != Qt.ItemDataRole.CheckStateRole
            or index.column() != _COL_CHECK
        ):
            return False
        row = self._rows[index.row()]
        key = (row.group, row.number)
        # PySide reicht hier sowohl Qt.CheckState als auch int durch.
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        if checked:
            self._marks.add(key)
        else:
            self._marks.discard(key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.marks_changed.emit(len(self._marks))
        return True

    def article_at(self, row: int) -> header_cache.ArticleRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def total_known(self) -> int:
        return self._total


def _fmt_bytes(n: int) -> str:
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class HeaderView(QWidget):
    article_activated = Signal(object)  # ArticleRow
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
        proxy.setSortRole(Qt.ItemDataRole.DisplayRole)
        self._table.setModel(proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_NUMBER, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_SUBJECT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(_COL_FROM, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.ResizeToContents)
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
        self._btn_submit_sab.setEnabled(False)
        self._btn_submit_sab.setToolTip("Phase 5: SABnzbd-Integration")
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
        total = self._model.total_known()
        if searching:
            self._status.setText(f"Suche {query!r} in {grp}: {total} Treffer")
        else:
            self._status.setText(f"{grp}: {total} Artikel im Cache")

    def _on_marks_changed(self, count: int) -> None:
        self._mark_status.setText(f"{count} markiert")
        self._btn_save_nzb.setEnabled(count > 0)
        self._btn_clear_marks.setEnabled(count > 0)

    def _on_double_clicked(self, proxy_index) -> None:
        proxy = self._table.model()
        src_index = proxy.mapToSource(proxy_index)
        # Doppelklick auf die Check-Spalte schaltet Häkchen, nicht Body
        if src_index.column() == _COL_CHECK:
            return
        article = self._model.article_at(src_index.row())
        if article is not None:
            self.article_activated.emit(article)

    def _on_save_nzb(self) -> None:
        marked = self._model.marked_articles()
        if marked:
            self.save_nzb_requested.emit(marked)

    def _on_submit_sab(self) -> None:
        marked = self._model.marked_articles()
        if marked:
            self.submit_sab_requested.emit(marked)
