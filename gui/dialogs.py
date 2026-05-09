"""Async-sichere Wrapper um QMessageBox.

Hintergrund: QMessageBox.exec()/.warning()/... sind modal und pumpen
die Qt-Eventloop. Aus einem qasync-Task heraus aufgerufen führt das
zu RuntimeError "Cannot enter into task X while another task Y is
being executed", weil andere Tasks im verschachtelten Loop wieder
laufen wollen.

Diese Helper umgehen das so:
- *_later() schiebt die Box auf den nächsten Event-Tick (QTimer).
  Der ursprüngliche Task ist dann längst durch.
- confirm_async() zeigt die Box nicht-modal und liefert per Future
  zurück, was der User geklickt hat – das await ist re-entrancy-frei.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget


def warn_later(parent: QWidget | None, title: str, text: str) -> None:
    QTimer.singleShot(0, lambda: QMessageBox.warning(parent, title, text))


def info_later(parent: QWidget | None, title: str, text: str) -> None:
    QTimer.singleShot(0, lambda: QMessageBox.information(parent, title, text))


def critical_later(parent: QWidget | None, title: str, text: str) -> None:
    QTimer.singleShot(0, lambda: QMessageBox.critical(parent, title, text))


def confirm_async(parent: QWidget | None, title: str, text: str) -> Awaitable[bool]:
    """Zeigt eine Yes/No-Frage non-modal und liefert ein awaitable bool."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QMessageBox.StandardButton.No)

    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

    def _on_clicked(btn) -> None:
        if fut.done():
            return
        role = box.standardButton(btn)
        fut.set_result(role == QMessageBox.StandardButton.Yes)

    box.buttonClicked.connect(_on_clicked)
    box.finished.connect(lambda _: None)  # hält box am Leben bis der Click feuert
    box.setModal(False)
    box.show()
    return fut
