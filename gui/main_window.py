from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from config import Config

log = logging.getLogger(__name__)


def _placeholder(title: str, hint: str) -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(8, 8, 8, 8)
    heading = QLabel(f"<b>{title}</b>")
    body = QLabel(hint)
    body.setWordWrap(True)
    body.setStyleSheet("color: #888;")
    layout.addWidget(heading)
    layout.addWidget(body)
    layout.addStretch(1)
    return container


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self.setWindowTitle("Usenet-App")
        self.resize(1200, 800)

        self._build_menubar()
        self._build_central()
        self._build_statusbar()

        log.info("MainWindow initialisiert")

    def _build_menubar(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&Datei")
        quit_action = QAction("&Beenden", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("&Ansicht")
        view_menu.setEnabled(False)

        help_menu = menubar.addMenu("&Hilfe")
        help_menu.setEnabled(False)

    def _build_central(self) -> None:
        # Vertikaler Outer-Splitter: oben = (Group | Header), unten = Queue
        outer = QSplitter(Qt.Orientation.Vertical, self)

        inner = QSplitter(Qt.Orientation.Horizontal, outer)
        inner.addWidget(
            _placeholder(
                "Gruppen",
                "Phase 3: Liste abonnierter Newsgroups. Subscribe-Toggle, Sync-Button.",
            )
        )
        inner.addWidget(
            _placeholder(
                "Header",
                "Phase 3: Header-Tabelle mit FTS5-Suche. Doppelklick → Artikel-Body.",
            )
        )
        inner.setStretchFactor(0, 1)
        inner.setStretchFactor(1, 4)
        inner.setSizes([260, 940])

        outer.addWidget(inner)
        outer.addWidget(
            _placeholder(
                "SABnzbd-Queue",
                "Phase 5: Live-Status der SAB-Download-Queue.",
            )
        )
        outer.setStretchFactor(0, 4)
        outer.setStretchFactor(1, 1)
        outer.setSizes([620, 180])

        self.setCentralWidget(outer)

    def _build_statusbar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        cfg_src = self._cfg.source_path
        msg = (
            f"Bereit · Config: {cfg_src}"
            if cfg_src
            else "Bereit · keine Config gefunden (Defaults aktiv)"
        )
        bar.showMessage(msg)
