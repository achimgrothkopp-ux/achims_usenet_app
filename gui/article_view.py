from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import header_cache, nntp_client, yenc
from core.nzb_builder import FileSet, group_articles
from core.release_grouper import ReleaseRow

log = logging.getLogger(__name__)

_TEXT_PAGE, _IMAGE_PAGE = 0, 1
# Hard-Cap für die Bild-Vorschau. Multi-Part-Bilder darüber fetcht man
# sich besser über SABnzbd – die Vorschau soll schnell sein.
_MAX_PREVIEW_BYTES = 30 * 1024 * 1024


def _looks_yenc(body: bytes) -> bool:
    head = body[:200].lstrip()
    return head.startswith(b"=ybegin") or b"\n=ybegin" in body[:1024]


def _decode_text(body: bytes) -> str:
    # Newsgroup-Bodies sind oft latin-1 oder utf-8. utf-8 mit replace
    # deckt beide pragmatisch ab.
    return body.decode("utf-8", errors="replace")


class ArticleView(QWidget):
    def __init__(self, pool: nntp_client.NNTPPool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._current_key: str | None = None
        self._task: asyncio.Task | None = None
        # Volle Pixmap für Resize-Rescaling im Image-Modus.
        self._full_pixmap: QPixmap | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._meta = QLabel("Doppelklick auf einen Release lädt den Artikel.", self)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._meta.setWordWrap(True)
        layout.addWidget(self._meta)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, 1)

        # Page 0: Text
        self._body = QPlainTextEdit(self._stack)
        self._body.setReadOnly(True)
        self._body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        font = self._body.font()
        font.setFamily("monospace")
        self._body.setFont(font)
        self._stack.addWidget(self._body)

        # Page 1: Bild in scrollbarem Viewport
        self._image_area = QScrollArea(self._stack)
        self._image_area.setWidgetResizable(True)
        self._image_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label = QLabel(self._image_area)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background: #111;")
        self._image_area.setWidget(self._image_label)
        self._stack.addWidget(self._image_area)

        self._stack.setCurrentIndex(_TEXT_PAGE)

    # ---- Public API ----------------------------------------------------

    def show_release(self, release: ReleaseRow) -> None:
        """Eine Release-Zeile rendern.

        Wenn das Release ein Bild-File enthält (.jpg/.png/.gif/.webp/.bmp/
        .tiff), holen wir alle Segmente, dekodieren mit sabyenc3 und
        zeigen das Bild. Sonst: erstes File als Text (mit Hinweis bei yEnc).
        """
        if self._task and not self._task.done():
            self._task.cancel()
        self._full_pixmap = None

        if not release.articles:
            self._show_text_message("Release enthält keine Artikel.")
            return

        files = group_articles(release.articles)
        if not files:
            self._show_text_message("Keine Datei im Release erkannt.")
            return

        target = _pick_preview_file(files)
        # Schlüssel, an dem wir bei verspätet ankommenden Antworten erkennen,
        # ob inzwischen ein anderer Release gewählt wurde.
        self._current_key = f"{release.key}|{target.stem}"

        first_article = target.segments[0][1]
        self._meta.setText(
            f"<b>{_html_escape(target.stem)}</b><br>"
            f"<span style='color:#888'>{_html_escape(target.poster)} · "
            f"#{first_article.number} · {len(target.segments)} Segment(e)</span>"
        )
        self._body.setPlainText("Lade …")
        self._stack.setCurrentIndex(_TEXT_PAGE)

        self._task = asyncio.ensure_future(self._fetch_async(target, self._current_key))

    # ---- Backwards-compat (alte Aufrufer, falls noch wo verdrahtet) ---

    def show_article(self, article: header_cache.ArticleRow) -> None:
        """Legacy-Pfad: einzelnen Artikel anzeigen (kein Multi-Part, kein Bild)."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._full_pixmap = None
        self._current_key = article.message_id
        self._meta.setText(
            f"<b>{_html_escape(article.subject)}</b><br>"
            f"<span style='color:#888'>{_html_escape(article.from_addr)} · "
            f"{_html_escape(article.date)} · #{article.number}</span>"
        )
        self._body.setPlainText("Lade …")
        self._stack.setCurrentIndex(_TEXT_PAGE)
        self._task = asyncio.ensure_future(self._fetch_single(article))

    # ---- Internals -----------------------------------------------------

    async def _fetch_async(self, target: FileSet, key: str) -> None:
        total_est = sum(a.bytes for _, a in target.segments)
        if total_est > _MAX_PREVIEW_BYTES:
            self._show_text_message(
                f"Datei zu groß für Vorschau: ~{total_est / (1024*1024):.1f} MB.\n"
                f"Lade das Release über → SABnzbd herunter."
            )
            return

        try:
            tasks = [
                self._pool.article(a.message_id) for _, a in target.segments
            ]
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Multi-Part-Fetch fehlgeschlagen: %s", exc)
            self._show_text_message(f"Fehler beim Laden:\n{exc}")
            return

        if self._current_key != key:
            return  # ein anderer Release wurde inzwischen ausgewählt

        bodies = [body for _hdr, body in results]
        sizes = [len(b) for b in bodies]
        yenc_hits = [_looks_yenc(b) for b in bodies]
        log.info(
            "Fetched %d segment(s) for %r: sizes=%s, yenc_markers=%s, first_bytes=%r",
            len(bodies), target.stem, sizes, yenc_hits,
            bodies[0][:32] if bodies and bodies[0] else b"",
        )
        if not any(yenc_hits):
            # Kein yEnc – Text anzeigen, ggf. zusammengefügt.
            joined = b"\n".join(bodies)
            self._show_text(_decode_text(joined[: 32 * 1024]))
            return

        try:
            decoded = yenc.decode(bodies)
        except ValueError as exc:
            log.info("yEnc-Decode fehlgeschlagen, zeige Hexauszug: %s", exc)
            self._show_text_message(
                f"yEnc-Decode fehlgeschlagen: {exc}\n\n"
                f"Auszug:\n{_decode_text(bodies[0][:500])}"
            )
            return

        self._render_decoded(decoded, target.stem)

    def _render_decoded(self, decoded: yenc.DecodedFile, fallback_label: str) -> None:
        """Klassifiziert die dekodierten Bytes und wählt Bild- oder Text-Modus.

        Reihenfolge der Tests (am robustesten zuerst):
        1. Magic-Bytes deuten auf Bild → Bild rendern.
        2. Filename mit Bild-Endung → Bild versuchen (manche JPEGs haben
           ungewöhnliche Header).
        3. Filename mit bekannter Text-Endung (.nfo/.txt/…) → Text.
        4. Sonst: Binär-Hinweis statt 32 KB Bytecode in der Anzeige.
        """
        magic = yenc.detect_image_format(decoded.data)
        name = decoded.filename or fallback_label
        log.info(
            "Decoded %s: %d Bytes, magic=%s, name=%r, crc_ok=%s",
            fallback_label, len(decoded.data), magic, name, decoded.crc_ok,
        )

        if magic or yenc.is_image_filename(name):
            if self._display_image(decoded.data):
                return
            log.info("QImage konnte %s nicht parsen (magic=%s)", name, magic)
            self._show_text_message(
                f"Bild {name!r} konnte nicht angezeigt werden "
                f"(magic={magic or '–'}, {len(decoded.data):,} Bytes).\n"
                "Eventuell ist das Posting unvollständig oder das Format "
                "wird nicht unterstützt (z.B. HEIC/AVIF)."
            )
            return

        if yenc.is_text_filename(name):
            self._show_text(_decode_text(decoded.data[:32 * 1024]))
            return

        # Binäres Material ohne Bild-Magic – Bytecode wäre Müll, lieber Hinweis.
        self._show_text_message(
            f"Datei {name!r}: {len(decoded.data):,} Bytes, kein Bild.\n"
            f"Erste Bytes hex: {decoded.data[:16].hex(' ')}\n"
            "Zum Download → SABnzbd."
        )

    async def _fetch_single(self, article: header_cache.ArticleRow) -> None:
        try:
            _hdr, body = await self._pool.article(article.message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ARTICLE %s fehlgeschlagen: %s", article.message_id, exc)
            self._show_text_message(f"Fehler beim Laden:\n{exc}")
            return

        if self._current_key != article.message_id:
            return

        if _looks_yenc(body):
            try:
                decoded = yenc.decode([body])
            except ValueError as exc:
                self._show_text(f"[yEnc, Decode fehlgeschlagen: {exc}]")
                return
            self._render_decoded(decoded, article.subject)
        else:
            self._show_text(_decode_text(body))

    def _show_text(self, text: str) -> None:
        self._body.setPlainText(text)
        self._stack.setCurrentIndex(_TEXT_PAGE)

    def _show_text_message(self, text: str) -> None:
        self._show_text(text)

    def _display_image(self, data: bytes) -> bool:
        img = QImage.fromData(data)
        if img.isNull():
            return False
        self._full_pixmap = QPixmap.fromImage(img)
        self._rescale_image()
        self._stack.setCurrentIndex(_IMAGE_PAGE)
        return True

    def _rescale_image(self) -> None:
        if self._full_pixmap is None or self._full_pixmap.isNull():
            return
        viewport = self._image_area.viewport().size()
        # Nur runterskalieren – Bild nicht künstlich vergrößern.
        target = self._full_pixmap.size().boundedTo(viewport)
        scaled = self._full_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)
        self._image_label.resize(scaled.size())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._stack.currentIndex() == _IMAGE_PAGE:
            self._rescale_image()


def _pick_preview_file(files: list[FileSet]) -> FileSet:
    """Bevorzugt das erste Bild-File, sonst das erste File überhaupt."""
    for fs in files:
        if yenc.is_image_filename(_filename_from_stem(fs.stem)):
            return fs
    return files[0]


def _filename_from_stem(stem: str) -> str:
    """Aus dem nzb_builder-Stem den eigentlichen Dateinamen ziehen.

    Stems sehen aus wie `[1/4] - "Foo.jpg"` oder einfach `Foo.jpg`.
    Wir bevorzugen den letzten quoted-Block.
    """
    import re
    matches = re.findall(r'"([^"]+)"', stem)
    if matches:
        return matches[-1].strip()
    return stem.strip()


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
