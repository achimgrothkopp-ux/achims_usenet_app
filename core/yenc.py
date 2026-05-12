"""yEnc-Decoder-Wrapper.

Wir verlassen uns auf sabyenc3 (C-Extension von den SABnzbd-Entwicklern),
weil sie deutlich schneller und robuster gegen kaputte Frames ist als
ein reiner Python-Decoder. Eine Single-Part-Datei = ein NNTP-Article-Body,
ein Multi-Part-File = mehrere Article-Bodies in Part-Reihenfolge.

API:
    decode(article_bodies: list[bytes]) -> DecodedFile
    is_image_filename(name: str) -> bool

`article_bodies` ist eine Liste der rohen Bytes, wie sie aus
`NNTPPool.article()` zurückkommen — sabyenc3 erwartet das so.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import sabyenc3

log = logging.getLogger(__name__)


_IMAGE_EXTS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
)

# Sichere Klartextendungen, die wir auch ohne Bild-Magic als Text zeigen.
_TEXT_EXTS: frozenset[str] = frozenset(
    {".txt", ".nfo", ".sfv", ".srr", ".md", ".log", ".diz", ".cue"}
)


@dataclass(frozen=True)
class DecodedFile:
    """Ergebnis eines yEnc-Decodes."""
    filename: str
    data: bytes
    crc_ok: bool


def is_image_filename(name: str) -> bool:
    if not name:
        return False
    _, ext = os.path.splitext(name)
    return ext.lower() in _IMAGE_EXTS


def is_text_filename(name: str) -> bool:
    if not name:
        return False
    _, ext = os.path.splitext(name)
    return ext.lower() in _TEXT_EXTS


def detect_image_format(data: bytes) -> str | None:
    """Bildformat anhand der ersten Bytes erkennen.

    Robuster als Filename-Erkennung — Magic-Bytes lügen nicht. Liefert
    Kurz-Tag ("jpg", "png", "gif", "webp", "bmp", "tiff") oder None.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def decode(article_bodies: list[bytes]) -> DecodedFile:
    """Decodiere yEnc-Frames zu einer Datei.

    Wirft `ValueError`, wenn sabyenc3 keinen brauchbaren Output liefert
    (kein yEnc-Header, völlig kaputte Frames). Ein CRC-Mismatch reicht
    NICHT für einen Fehler — wir liefern die Daten trotzdem zurück
    (`crc_ok=False`); der Aufrufer kann selber entscheiden, ob er sie
    anzeigt. Bei Bild-Vorschau ist ein leicht-kaputtes JPEG oft noch
    sehenswert.
    """
    if not article_bodies:
        raise ValueError("Keine Artikel-Bodies übergeben")

    try:
        data, filename, crc_ok = sabyenc3.decode_usenet_chunks(article_bodies)
    except Exception as exc:
        raise ValueError(f"yEnc-Decode fehlgeschlagen: {exc}") from exc

    if not data:
        raise ValueError("yEnc-Decode: leere Ausgabe")
    return DecodedFile(filename=filename or "", data=bytes(data), crc_ok=bool(crc_ok))
