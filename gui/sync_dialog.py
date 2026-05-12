"""Dialog zur Auswahl des Sync-Modus.

Vier Modi: inkrementell (ab last_seen), letzte N, seit Datum, Vollsync.
Zusätzlich optionaler Pro-Lauf-Cap (max_articles), damit ein Sync gegen
eine Mio-Gruppe nicht stundenlang läuft – der Rest holt sich der nächste
Klick ab.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.nntp_client import SyncPlan


class SyncDialog(QDialog):
    """Sammelt einen SyncPlan vom User."""

    def __init__(
        self,
        group: str,
        *,
        low: int,
        high: int,
        count: int,
        last_seen: int,
        pool_max: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._group = group
        self._low = low
        self._high = high
        self._count = count
        self._last_seen = last_seen
        self._pool_max = max(1, pool_max)

        self.setWindowTitle(f"Sync: {group}")
        self.setModal(True)
        self._build_ui()
        self._select_default()
        self._update_enabled()
        self._update_estimate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- Statistik -------------------------------------------------
        gap_incremental = max(0, self._high - max(self._low, self._last_seen + 1) + 1)
        stats = QGroupBox("Status", self)
        form = QFormLayout(stats)
        form.addRow("Gruppe:", QLabel(self._group, self))
        form.addRow(
            "Server:",
            QLabel(
                f"#{self._low:,} – #{self._high:,}  ({self._count:,} Artikel)",
                self,
            ),
        )
        form.addRow(
            "Cache:",
            QLabel(
                f"last_seen = #{self._last_seen:,}  →  Gap inkrementell: {gap_incremental:,}",
                self,
            ),
        )
        layout.addWidget(stats)

        # ---- Modus -----------------------------------------------------
        modes = QGroupBox("Modus", self)
        mode_layout = QVBoxLayout(modes)
        self._group_modes = QButtonGroup(self)

        self._rb_incremental = QRadioButton(
            f"Inkrementell – ab #{self._last_seen + 1:,} (Gap: {gap_incremental:,})", self
        )
        self._group_modes.addButton(self._rb_incremental, 0)
        mode_layout.addWidget(self._rb_incremental)

        row_n = _row()
        self._rb_last_n = QRadioButton("Letzte", self)
        self._group_modes.addButton(self._rb_last_n, 1)
        self._spin_last_n = QSpinBox(self)
        self._spin_last_n.setRange(100, 100_000_000)
        self._spin_last_n.setSingleStep(10_000)
        self._spin_last_n.setValue(50_000)
        self._spin_last_n.setGroupSeparatorShown(True)
        self._lbl_last_n_suffix = QLabel("Artikel (ab dem Ende des Servers)", self)
        row_n.layout().addWidget(self._rb_last_n)
        row_n.layout().addWidget(self._spin_last_n)
        row_n.layout().addWidget(self._lbl_last_n_suffix, 1)
        mode_layout.addWidget(row_n)

        row_d = _row()
        self._rb_since = QRadioButton("Seit", self)
        self._group_modes.addButton(self._rb_since, 2)
        self._date = QDateEdit(self)
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("yyyy-MM-dd")
        default_date = QDate.currentDate().addDays(-30)
        self._date.setDate(default_date)
        self._date.setMaximumDate(QDate.currentDate())
        self._lbl_since_suffix = QLabel("(Datums-Bisection auf dem Server)", self)
        row_d.layout().addWidget(self._rb_since)
        row_d.layout().addWidget(self._date)
        row_d.layout().addWidget(self._lbl_since_suffix, 1)
        mode_layout.addWidget(row_d)

        self._rb_full = QRadioButton(
            f"Vollsync – Cache leeren und alle {self._count:,} Artikel laden", self
        )
        self._group_modes.addButton(self._rb_full, 3)
        mode_layout.addWidget(self._rb_full)

        layout.addWidget(modes)

        # ---- Begrenzung -----------------------------------------------
        limit_box = QGroupBox("Begrenzung", self)
        limit_layout = QVBoxLayout(limit_box)
        self._chk_max = QCheckBox("Pro Lauf höchstens", self)
        self._spin_max = QSpinBox(self)
        self._spin_max.setRange(1_000, 100_000_000)
        self._spin_max.setSingleStep(10_000)
        self._spin_max.setValue(200_000)
        self._spin_max.setGroupSeparatorShown(True)
        self._lbl_max_suffix = QLabel(
            "Article-Numbers (Rest holt der nächste Sync ab)", self
        )
        row_m = _row()
        row_m.layout().addWidget(self._chk_max)
        row_m.layout().addWidget(self._spin_max)
        row_m.layout().addWidget(self._lbl_max_suffix, 1)
        limit_layout.addWidget(row_m)

        # Parallele Connections – 1 ≈ altes Verhalten, mehr = Header-Fetch
        # über mehrere NNTP-Sockets gleichzeitig. Limitiert auf Pool-Größe.
        self._spin_parallel = QSpinBox(self)
        self._spin_parallel.setRange(1, self._pool_max)
        self._spin_parallel.setValue(self._pool_max)
        row_p = _row()
        row_p.layout().addWidget(QLabel("Parallele Connections:", self))
        row_p.layout().addWidget(self._spin_parallel)
        row_p.layout().addWidget(
            QLabel(f"(max {self._pool_max} laut config.toml)", self), 1
        )
        limit_layout.addWidget(row_p)
        layout.addWidget(limit_box)

        # ---- Schätzung -------------------------------------------------
        self._lbl_estimate = QLabel("", self)
        self._lbl_estimate.setWordWrap(True)
        self._lbl_estimate.setStyleSheet("color: #666;")
        layout.addWidget(self._lbl_estimate)

        # ---- Buttons --------------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Live-Update der Aktivierung & Schätzung
        for rb in (self._rb_incremental, self._rb_last_n, self._rb_since, self._rb_full):
            rb.toggled.connect(self._update_enabled)
            rb.toggled.connect(self._update_estimate)
        self._spin_last_n.valueChanged.connect(self._update_estimate)
        self._spin_max.valueChanged.connect(self._update_estimate)
        self._chk_max.toggled.connect(self._update_estimate)
        self._chk_max.toggled.connect(self._update_enabled)
        self._date.dateChanged.connect(self._update_estimate)

    def _select_default(self) -> None:
        # Cache leer → "Letzte N" als sicherer Standard für große Gruppen.
        # Sonst inkrementell.
        if self._last_seen <= 0:
            self._rb_last_n.setChecked(True)
        else:
            self._rb_incremental.setChecked(True)

    def _update_enabled(self) -> None:
        self._spin_last_n.setEnabled(self._rb_last_n.isChecked())
        self._date.setEnabled(self._rb_since.isChecked())
        self._spin_max.setEnabled(self._chk_max.isChecked())

    def _planned_range(self) -> int:
        """Erwartete Article-Number-Range nach Modus (vor Cap)."""
        if self._rb_incremental.isChecked():
            return max(0, self._high - max(self._low, self._last_seen + 1) + 1)
        if self._rb_last_n.isChecked():
            start = max(self._low, self._high - self._spin_last_n.value() + 1, self._last_seen + 1)
            return max(0, self._high - start + 1)
        if self._rb_since.isChecked():
            # ohne Server-Lookup nur grobe Schätzung über Range – wir nehmen
            # konservativ den Gap als Obergrenze.
            return max(0, self._high - max(self._low, self._last_seen + 1) + 1)
        if self._rb_full.isChecked():
            return self._count
        return 0

    def _update_estimate(self) -> None:
        n = self._planned_range()
        if self._chk_max.isChecked():
            n = min(n, self._spin_max.value())
        if n <= 0:
            self._lbl_estimate.setText("Nichts zu syncen.")
            return
        est_min = n / 200_000
        est_mb = n * 0.7 / 1024
        extra = ""
        if self._rb_since.isChecked():
            extra = " (Datum-Bisection liefert evtl. weniger)"
        self._lbl_estimate.setText(
            f"Schätzung: ~{n:,} Artikel · ~{est_min:.1f} Min · ~{est_mb:.0f} MB Cache" + extra
        )

    @property
    def plan(self) -> SyncPlan:
        max_articles = self._spin_max.value() if self._chk_max.isChecked() else None
        parallel = self._spin_parallel.value()
        if self._rb_full.isChecked():
            return SyncPlan.full(max_articles=max_articles, parallel=parallel)
        if self._rb_last_n.isChecked():
            return SyncPlan.last_n_articles(
                self._spin_last_n.value(), max_articles=max_articles, parallel=parallel
            )
        if self._rb_since.isChecked():
            qd = self._date.date()
            since = datetime(qd.year(), qd.month(), qd.day(), tzinfo=timezone.utc)
            return SyncPlan.since_date(since, max_articles=max_articles, parallel=parallel)
        return SyncPlan.incremental(max_articles=max_articles, parallel=parallel)

    @staticmethod
    def get_plan(
        parent: QWidget | None,
        group: str,
        *,
        low: int,
        high: int,
        count: int,
        last_seen: int,
        pool_max: int = 1,
    ) -> SyncPlan | None:
        dlg = SyncDialog(
            group, low=low, high=high, count=count, last_seen=last_seen,
            pool_max=pool_max, parent=parent,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.plan

    @staticmethod
    def show_for(
        parent: QWidget | None,
        group: str,
        *,
        low: int,
        high: int,
        count: int,
        last_seen: int,
        pool_max: int = 1,
    ) -> Awaitable["SyncPlan | None"]:
        """Non-modal in den qasync-Loop integrierte Variante von get_plan.

        Gibt ein awaitable zurück, das den gewählten Plan (oder None bei
        Abbruch) liefert, ohne die asyncio-Loop zu blockieren (kein .exec()).
        """
        dlg = SyncDialog(
            group, low=low, high=high, count=count, last_seen=last_seen,
            pool_max=pool_max, parent=parent,
        )
        fut: asyncio.Future[SyncPlan | None] = asyncio.get_event_loop().create_future()

        def _done(code: int) -> None:
            if fut.done():
                return
            fut.set_result(dlg.plan if code == QDialog.DialogCode.Accepted else None)

        dlg.finished.connect(_done)
        dlg.setModal(True)
        dlg.show()
        return fut


def _row() -> QWidget:
    """Horizontale Zeile als QWidget – simpler als QHBoxLayout direkt einfügen."""
    from PySide6.QtWidgets import QHBoxLayout

    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    return w
