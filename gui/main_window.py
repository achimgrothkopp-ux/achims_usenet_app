from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QWidget,
)

import time

from backend.sabnzbd import SABError, SABnzbdClient
from config import Config
from config import save as save_config
from config import with_updates as cfg_with_updates
from core import header_cache, nntp_client, nzb_builder
from gui.article_view import ArticleView
from gui.dialogs import critical_later
from gui.group_panel import GroupPanel
from gui.header_view import HeaderView
from gui.health_indicator import HealthIndicator
from gui.queue_panel import QueuePanel
from gui.settings_dialog import SettingsDialog
from gui.sync_dialog import SyncDialog


log = logging.getLogger(__name__)


def _suggest_filename(files: list) -> str:
    if not files:
        return "auswahl.nzb"
    stem = files[0].stem.strip().strip('"')
    for ch in '/\\:*?"<>|':
        stem = stem.replace(ch, "_")
    return (stem or "auswahl") + ".nzb"


class MainWindow(QMainWindow):
    def __init__(
        self,
        cfg: Config,
        cache: header_cache.HeaderCache,
        pool: nntp_client.NNTPPool,
        sab: SABnzbdClient | None = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._cache = cache
        self._pool = pool
        self._sab = sab
        self._settings = QSettings("local", "Usenet-App")
        # Pro laufendem Sync ein Cancel-Event; Stop-Button setzt das Event,
        # die sync_group-Schleife checkt es zwischen Chunks.
        self._syncing: dict[str, asyncio.Event] = {}
        self.setWindowTitle("Usenet-App")
        self.resize(1280, 860)

        self._build_menubar()
        self._build_central()
        self._build_statusbar()
        self._wire_signals()
        self._install_shortcuts()
        self._restore_layout()

        if self._sab is not None:
            self._health.start()

        log.info("MainWindow initialisiert")

    # ---- UI-Aufbau -----------------------------------------------------

    def _build_menubar(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&Datei")
        settings_action = QAction("&Einstellungen…", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()
        quit_action = QAction("&Beenden", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("&Ansicht")
        reset_layout = QAction("Layout zurücksetzen", self)
        reset_layout.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_layout)

        help_menu = menubar.addMenu("&Hilfe")
        about_action = QAction("&Über Usenet-App…", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_central(self) -> None:
        self._group_panel = GroupPanel(self._cache, self._pool, self)
        self._header_view = HeaderView(self._cache, self)
        self._article_view = ArticleView(self._pool, self)
        self._queue_panel = QueuePanel(self._sab, self)

        self._right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._right_splitter.addWidget(self._header_view)
        self._right_splitter.addWidget(self._article_view)
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 2)
        self._right_splitter.setSizes([520, 340])

        self._inner_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._inner_splitter.addWidget(self._group_panel)
        self._inner_splitter.addWidget(self._right_splitter)
        self._inner_splitter.setStretchFactor(0, 1)
        self._inner_splitter.setStretchFactor(1, 5)
        self._inner_splitter.setSizes([260, 1020])

        self._outer_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._outer_splitter.addWidget(self._inner_splitter)
        self._outer_splitter.addWidget(self._queue_panel)
        self._outer_splitter.setStretchFactor(0, 4)
        self._outer_splitter.setStretchFactor(1, 1)
        self._outer_splitter.setSizes([660, 200])

        self.setCentralWidget(self._outer_splitter)

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
        self._health = HealthIndicator(self._sab, self)
        bar.addPermanentWidget(self._health)

    def _wire_signals(self) -> None:
        self._group_panel.group_selected.connect(self._on_group_selected)
        self._group_panel.sync_requested.connect(self._on_sync_requested)
        self._group_panel.cancel_requested.connect(self._on_cancel_requested)
        self._header_view.article_activated.connect(self._article_view.show_article)
        self._header_view.save_nzb_requested.connect(self._on_save_nzb)
        self._header_view.submit_sab_requested.connect(self._on_submit_sab)

    def _install_shortcuts(self) -> None:
        # vi-style Newsreader-Shortcuts (gelten global, weil sie nur
        # Sinn ergeben wenn Header-View aktiv ist – sie no-op'en sonst).
        QShortcut(QKeySequence("J"), self, activated=self._header_view.next_row)
        QShortcut(QKeySequence("K"), self, activated=self._header_view.prev_row)
        QShortcut(QKeySequence("Space"), self, activated=self._header_view.toggle_mark_current)

    # ---- Layout-Persistenz ---------------------------------------------

    def _restore_layout(self) -> None:
        s = self._settings
        geom = s.value("window/geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        for splitter, key in (
            (self._outer_splitter, "splitter/outer"),
            (self._inner_splitter, "splitter/inner"),
            (self._right_splitter, "splitter/right"),
        ):
            state = s.value(key)
            if state is not None:
                splitter.restoreState(state)
        for header, key in self._persistable_headers():
            state = s.value(key)
            if state is not None:
                header.restoreState(state)

    def _save_layout(self) -> None:
        s = self._settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("splitter/outer", self._outer_splitter.saveState())
        s.setValue("splitter/inner", self._inner_splitter.saveState())
        s.setValue("splitter/right", self._right_splitter.saveState())
        for header, key in self._persistable_headers():
            s.setValue(key, header.saveState())

    def _persistable_headers(self):
        return [
            (self._header_view.table().horizontalHeader(), "header_table/header_state"),
            (self._queue_panel.table().horizontalHeader(), "queue_table/header_state"),
        ]

    def _reset_layout(self) -> None:
        for key in (
            "window/geometry",
            "splitter/outer",
            "splitter/inner",
            "splitter/right",
            "header_table/header_state",
            "queue_table/header_state",
        ):
            self._settings.remove(key)
        QMessageBox.information(
            self,
            "Layout zurückgesetzt",
            "Beim nächsten App-Start werden die Default-Größen wiederhergestellt.",
        )

    # ---- Slots ---------------------------------------------------------

    def _on_group_selected(self, name: str) -> None:
        log.info("Gruppe gewählt: %s", name)
        self._header_view.set_group(name)
        self._statusbar.showMessage(f"Gruppe: {name}", 3000)

    def _on_sync_requested(self, name: str) -> None:
        if name in self._syncing:
            self._statusbar.showMessage(f"Sync für {name} läuft bereits", 3000)
            return
        # Lock + Event SYNCHRON setzen, sonst gewinnt ein Doppelklick das
        # Rennen, bevor der erste Task überhaupt geschedult wurde.
        event = asyncio.Event()
        self._syncing[name] = event
        self._group_panel.set_sync_running(name, True)
        asyncio.ensure_future(self._sync_flow(name, event))

    def _on_cancel_requested(self, name: str) -> None:
        event = self._syncing.get(name)
        if event is None:
            return
        event.set()
        self._statusbar.showMessage(f"Sync-Stop für {name} angefordert …", 3000)

    async def _sync_flow(self, name: str, cancel: asyncio.Event) -> None:
        try:
            try:
                count, low, high = await self._pool.group_info(name)
            except Exception as exc:
                log.exception("group_info %s fehlgeschlagen", name)
                self._statusbar.showMessage(f"Sync {name}: {exc}", 5000)
                return

            if cancel.is_set():
                return

            last_seen = await asyncio.to_thread(self._cache.get_last_seen, name)

            plan = await SyncDialog.show_for(
                self, name,
                low=low, high=high, count=count, last_seen=last_seen,
            )
            if plan is None or cancel.is_set():
                self._statusbar.showMessage("Sync abgebrochen", 3000)
                return

            self._statusbar.showMessage(f"Sync läuft: {name} …")
            progress = self._make_sync_progress(name)
            try:
                n = await nntp_client.sync_group(
                    self._pool, self._cache, name,
                    plan=plan, cancel=cancel, progress=progress,
                )
            except Exception as exc:
                log.exception("Sync %s fehlgeschlagen", name)
                self._statusbar.showMessage(f"Sync {name} fehlgeschlagen: {exc}", 5000)
                return

            if cancel.is_set():
                self._statusbar.showMessage(
                    f"Sync {name} abgebrochen ({n:,} neue Artikel)", 5000
                )
            else:
                self._statusbar.showMessage(f"Sync {name}: {n:,} neue Artikel", 5000)
            self._group_panel.refresh()
            if self._header_view.model().current_group() == name:
                self._header_view.refresh_current()
        finally:
            self._syncing.pop(name, None)
            self._group_panel.set_sync_running(name, False)

    def _make_sync_progress(self, name: str):
        """Throttled Progress-Callback: höchstens alle 500 ms ein Statusbar-Update."""
        last = [0.0]

        def cb(cur: int, total: int, inserted: int) -> None:
            now = time.monotonic()
            if now - last[0] < 0.5:
                return
            last[0] = now
            pct = 100.0 * cur / total if total else 100.0
            self._statusbar.showMessage(
                f"Sync {name}: bei #{cur:,} von #{total:,} ({pct:.1f}%) – {inserted:,} neu"
            )

        return cb

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
        if self._sab is None or not self._sab.configured:
            QMessageBox.warning(
                self,
                "SABnzbd nicht konfiguriert",
                "Trage in ~/.config/usenet-app/config.toml unter "
                "[sabnzbd] api_key ein und starte die App neu.",
            )
            return
        if not articles:
            return
        asyncio.ensure_future(self._submit_sab_async(articles))

    async def _submit_sab_async(self, articles: list) -> None:
        files = nzb_builder.group_articles(articles)
        default_name = _suggest_filename(files)
        try:
            xml = nzb_builder.build_nzb_xml(articles, title=Path(default_name).stem)
            nzb_builder.validate_nzb(xml)
        except Exception as exc:
            log.exception("NZB-Erstellung fehlgeschlagen")
            critical_later(self, "NZB-Fehler", str(exc))
            return

        self._statusbar.showMessage(f"Sende NZB an SABnzbd: {default_name} …")
        try:
            assert self._sab is not None
            nzo_id = await self._sab.add_nzb_bytes(xml, filename=default_name)
        except SABError as exc:
            log.warning("SAB-Submit fehlgeschlagen: %s", exc)
            self._statusbar.showMessage(f"SAB-Fehler: {exc}", 6000)
            return
        log.info("NZB an SAB übergeben: nzo_id=%s", nzo_id)
        self._statusbar.showMessage(
            f"In SABnzbd-Queue: {default_name} (nzo_id={nzo_id})",
            5000,
        )
        self._queue_panel.trigger_refresh()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._cfg, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new_cfg = cfg_with_updates(
            self._cfg,
            nntp=dlg.collected_nntp(),
            sabnzbd=dlg.collected_sab(),
        )
        try:
            target = self._cfg.source_path
            if target is None:
                from config import CONFIG_PATH
                target = CONFIG_PATH
            save_config(new_cfg, target)
        except Exception as exc:
            log.exception("Settings speichern fehlgeschlagen")
            QMessageBox.critical(self, "Fehler beim Speichern", str(exc))
            return
        self._cfg = new_cfg
        QMessageBox.information(
            self,
            "Einstellungen gespeichert",
            "Die Änderungen greifen beim nächsten App-Start.",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "Über Usenet-App",
            "<h3>Usenet-App</h3>"
            "<p>Newsreader + NZB-Builder mit SABnzbd-Anbindung.</p>"
            "<p>Phase 6-Build · Python 3.13 · PySide6 · pynntp · httpx · lxml</p>",
        )

    # ---- Lifecycle -----------------------------------------------------

    def closeEvent(self, event) -> None:
        self._save_layout()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Vom main-Loop aufgerufen, bevor die Loop schließt."""
        self._queue_panel.stop()
        self._health.stop()
