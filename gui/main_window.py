from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from config import Config
from core import header_cache, nntp_client, nzb_builder
from gui.article_view import ArticleView
from gui.group_panel import GroupPanel
from gui.header_view import HeaderView

log = logging.getLogger(__name__)


def _suggest_filename(files: list) -> str:
    """Aus dem ersten File-Stem einen sinnvollen Default-Dateinamen bauen."""
    if not files:
        return "auswahl.nzb"
    stem = files[0].stem.strip().strip('"')
    # Zeichen, die im FS nervig sind, ersetzen
    for ch in '/\\:*?"<>|':
        stem = stem.replace(ch, "_")
    return (stem or "auswahl") + ".nzb"


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
    def __init__(
        self,
        cfg: Config,
        cache: header_cache.HeaderCache,
        pool: nntp_client.NNTPPool,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._cache = cache
        self._pool = pool
        self.setWindowTitle("Usenet-App")
        self.resize(1280, 860)

        self._build_menubar()
        self._build_central()
        self._build_statusbar()
        self._wire_signals()

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
        self._group_panel = GroupPanel(self._cache, self._pool, self)
        self._header_view = HeaderView(self._cache, self)
        self._article_view = ArticleView(self._pool, self)

        # Header oben, Artikel unten
        right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        right_splitter.addWidget(self._header_view)
        right_splitter.addWidget(self._article_view)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 2)
        right_splitter.setSizes([520, 340])

        # Gruppen links, (Header/Artikel) rechts
        inner = QSplitter(Qt.Orientation.Horizontal, self)
        inner.addWidget(self._group_panel)
        inner.addWidget(right_splitter)
        inner.setStretchFactor(0, 1)
        inner.setStretchFactor(1, 5)
        inner.setSizes([260, 1020])

        # Queue-Panel als Platzhalter unten (Phase 5)
        outer = QSplitter(Qt.Orientation.Vertical, self)
        outer.addWidget(inner)
        outer.addWidget(
            _placeholder(
                "SABnzbd-Queue",
                "Phase 5: Live-Status der SAB-Download-Queue.",
            )
        )
        outer.setStretchFactor(0, 5)
        outer.setStretchFactor(1, 1)
        outer.setSizes([700, 140])

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
        self._statusbar = bar

    def _wire_signals(self) -> None:
        self._group_panel.group_selected.connect(self._on_group_selected)
        self._group_panel.sync_requested.connect(self._on_sync_requested)
        self._header_view.article_activated.connect(self._article_view.show_article)
        self._header_view.save_nzb_requested.connect(self._on_save_nzb)
        self._header_view.submit_sab_requested.connect(self._on_submit_sab)

    def _on_group_selected(self, name: str) -> None:
        log.info("Gruppe gewählt: %s", name)
        self._header_view.set_group(name)
        self._statusbar.showMessage(f"Gruppe: {name}", 3000)

    def _on_sync_requested(self, name: str) -> None:
        asyncio.ensure_future(self._sync_async(name))

    async def _sync_async(self, name: str) -> None:
        self._statusbar.showMessage(f"Sync läuft: {name} …")
        try:
            n = await nntp_client.sync_group(self._pool, self._cache, name)
        except Exception as exc:
            log.exception("Sync %s fehlgeschlagen", name)
            self._statusbar.showMessage(f"Sync {name} fehlgeschlagen: {exc}", 5000)
            return
        self._statusbar.showMessage(f"Sync {name}: {n} neue Artikel", 5000)
        self._group_panel.refresh()
        if self._header_view.model().current_group() == name:
            self._header_view.refresh_current()

    def _on_save_nzb(self, articles: list) -> None:
        if not articles:
            return
        files = nzb_builder.group_articles(articles)
        default_name = _suggest_filename(files)
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "NZB speichern",
            str(Path.home() / default_name),
            "NZB-Dateien (*.nzb)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".nzb":
            path = path.with_suffix(".nzb")
        try:
            xml = nzb_builder.build_nzb_xml(articles, title=path.stem)
            nzb_builder.validate_nzb(xml)
            path.write_bytes(xml)
        except Exception as exc:
            log.exception("NZB-Erstellung fehlgeschlagen")
            QMessageBox.critical(self, "NZB-Fehler", str(exc))
            return
        log.info(
            "NZB geschrieben: %s (%s Files, %s Segmente)",
            path,
            len(files),
            sum(len(f.segments) for f in files),
        )
        self._statusbar.showMessage(
            f"NZB gespeichert: {path.name} ({len(files)} Files, "
            f"{sum(len(f.segments) for f in files)} Segmente)",
            5000,
        )

    def _on_submit_sab(self, articles: list) -> None:
        QMessageBox.information(
            self,
            "Phase 5",
            "SABnzbd-Integration kommt in Phase 5. Bitte vorerst über "
            "'NZB speichern…' und manuell einreichen.",
        )
