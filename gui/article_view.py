from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from core import header_cache, nntp_client

log = logging.getLogger(__name__)


def _looks_yenc(body: bytes) -> bool:
    head = body[:200].lstrip()
    return head.startswith(b"=ybegin") or b"\n=ybegin" in body[:1024]


def _decode_body(body: bytes) -> str:
    # Newsgroup-Bodies sind oft latin-1 oder utf-8. Wir versuchen utf-8
    # mit replace, das deckt beide Fälle pragmatisch ab.
    return body.decode("utf-8", errors="replace")


class ArticleView(QWidget):
    def __init__(self, pool: nntp_client.NNTPPool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._current_msgid: str | None = None
        self._task: asyncio.Task | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._meta = QLabel("Doppelklick auf einen Header lädt den Artikel.", self)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._meta.setWordWrap(True)
        layout.addWidget(self._meta)

        self._body = QPlainTextEdit(self)
        self._body.setReadOnly(True)
        self._body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        font = self._body.font()
        font.setFamily("monospace")
        self._body.setFont(font)
        layout.addWidget(self._body, 1)

    def show_article(self, article: header_cache.ArticleRow) -> None:
        # Vorherige Ladung abbrechen, falls nötig
        if self._task and not self._task.done():
            self._task.cancel()

        self._current_msgid = article.message_id
        self._meta.setText(
            f"<b>{_html_escape(article.subject)}</b><br>"
            f"<span style='color:#888'>{_html_escape(article.from_addr)} · "
            f"{_html_escape(article.date)} · #{article.number} · "
            f"{article.bytes:,} bytes · {article.lines} lines</span>"
        )
        self._body.setPlainText("Lade …")

        self._task = asyncio.ensure_future(self._fetch_async(article))

    async def _fetch_async(self, article: header_cache.ArticleRow) -> None:
        try:
            _hdr, body = await self._pool.article(article.message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ARTICLE %s fehlgeschlagen: %s", article.message_id, exc)
            self._body.setPlainText(f"Fehler beim Laden:\n{exc}")
            return

        # Falls inzwischen ein anderer Artikel gewählt wurde → ignorieren.
        if self._current_msgid != article.message_id:
            return

        if _looks_yenc(body):
            self._body.setPlainText(
                f"[Binär-Posting (yEnc), {len(body):,} bytes]\n\n"
                "Ein Decoder folgt mit Phase 4 (NZB-Builder + SABnzbd-Übergabe).\n"
                "Auszug der ersten 500 Bytes:\n\n"
                + _decode_body(body[:500])
            )
        else:
            self._body.setPlainText(_decode_body(body))


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
