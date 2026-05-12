"""Release-Grouper: aggregiert Artikel zu Releases.

Hintergrund: In Binär-Gruppen kommen typischerweise hunderte bis tausende
Einzel-Artikel pro Datei (yEnc-Segmente), und ein Release besteht aus
mehreren Dateien (.partNN.rar, .par2, .vol000+NN.par2 …). Die UI soll
diese Artikel zu *einer* Zeile pro Release zusammenfassen, damit der
Nutzer einmal markieren kann statt 1000-fach.

Release-Erkennung (Heuristik):
1. parse_subject() liefert den Stamm ohne yEnc-Marker und Segment-Suffix.
2. Aus dem Stamm den Datei-Basisnamen extrahieren — bevorzugt aus
   `"..."` in Anführungszeichen, sonst der Stamm minus optionalem
   `[file/total] -` Prefix.
3. Vom Basisnamen typische Multi-Volume-/PAR2-Endungen abstreifen →
   release_base.
4. release_key = (poster, release_base) — gleicher Poster, gleicher
   "blanker" Dateiname ⇒ gleiches Release.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.header_cache import ArticleRow
from core.nzb_builder import parse_subject

# Dateiname in Anführungszeichen, möglichst das letzte Vorkommen.
_QUOTED_RE = re.compile(r'"([^"]+)"')
# Führender [file/total] - Prefix wie "[3/42] -" oder "[ 03 / 42 ] -".
_FILE_PREFIX_RE = re.compile(r"^\s*\[\s*\d+\s*/\s*\d+\s*\]\s*-?\s*")

# Trailing-Endungen, die wir als "Volume/PAR-Suffix" einer Datei werten
# und für die Release-Erkennung wegstreifen. Reihenfolge: spezifisch zuerst.
# Die `.\d{3}`-Regel ist BEWUSST auf exakt 3 Ziffern beschränkt, damit
# Jahreszahlen wie "Movie.2024" nicht mitgestrippt werden.
_TRAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # .vol000+01.par2, .vol12+34.PAR2
    re.compile(r"\.vol\d+\+\d+\.par2$", re.IGNORECASE),
    # .par2
    re.compile(r"\.par2$", re.IGNORECASE),
    # .part001.rar, .part12.rar
    re.compile(r"\.part\d+\.rar$", re.IGNORECASE),
    # alte Split-RARs: .r00, .r01, .r123
    re.compile(r"\.r\d{2,3}$", re.IGNORECASE),
    # split-archives ohne Endung: foo.001, foo.002 … (exakt 3 Ziffern)
    re.compile(r"\.\d{3}$"),
    # Endung des Haupt-Archivs — strippt zuletzt, damit `.partNN.rar`
    # nicht hier hängen bleibt. Sorgt dafür, dass `Foo.rar` und `Foo.r00`
    # zum selben Release-Key zusammenfallen.
    re.compile(r"\.(?:rar|zip|7z)$", re.IGNORECASE),
)


@dataclass
class ReleaseRow:
    """Ein Release = mehrere ArticleRows, die UI als eine Zeile darstellt."""
    key: str                       # stabiler Identifier (release_base|poster)
    subject: str                   # Anzeige-Stamm (release_base)
    poster: str
    date_unix: int                 # frühester Zeitstempel der enthaltenen Artikel
    total_bytes: int
    articles: list[ArticleRow] = field(default_factory=list)
    # Segmente: tatsächlich gesehen vs. laut (N/M) erwartet.
    # Wenn keine Subject Part-Info bekannt ist, bleibt expected = 0.
    segments_have: int = 0
    segments_expected: int = 0
    # Anzahl verschiedener Datei-Basisnamen (vor Trail-Strip) im Release.
    file_count: int = 0


def extract_filename(stem: str) -> str:
    """Aus dem parse_subject-Stamm den eigentlichen Dateinamen ziehen.

    Bevorzugt der LETZTE in `"..."` eingeklammerte Block — Subjects mit
    Releasername in Klammern vor dem File legen den File-Namen typisch
    ans Ende. Fällt zurück auf den Stamm ohne `[N/M] -` Prefix.
    """
    matches = _QUOTED_RE.findall(stem)
    if matches:
        return matches[-1].strip()
    return _FILE_PREFIX_RE.sub("", stem).strip()


def release_base(filename: str) -> str:
    """Dateinamen auf Release-Basis reduzieren.

    Entfernt iterativ Trail-Patterns (z.B. `.part01.rar`, `.par2`,
    `.vol000+01.par2`, `.r00`, `.001`). Stoppt wenn keines mehr matcht
    — bei einer einzelnen `.mkv` bleibt der Name unverändert.
    """
    name = filename.strip()
    while True:
        for pat in _TRAIL_PATTERNS:
            new = pat.sub("", name)
            if new != name:
                name = new
                break
        else:
            return name


def group_releases(articles: list[ArticleRow]) -> list[ReleaseRow]:
    """Artikel zu Releases zusammenfassen.

    Reihenfolge: Reihenfolge des ersten Auftretens (stabil), passend zur
    chronologischen DESC-Sortierung im Cache.
    """
    by_key: dict[str, ReleaseRow] = {}
    order: list[str] = []
    # Pro Release tracken wir, welche Datei-Basisnamen schon gesehen wurden
    # und welche part_total-Werte (für segments_expected) eingegangen sind.
    per_file_totals: dict[str, dict[str, int]] = {}

    for a in articles:
        parsed = parse_subject(a.subject)
        stem = parsed.stem or a.subject
        fname = extract_filename(stem)
        base = release_base(fname)
        # Poster ist Teil des Keys, damit zeitlich getrennte Releases mit
        # zufällig gleichem Namen nicht kollidieren.
        key = f"{a.from_addr}|{base}"

        rr = by_key.get(key)
        if rr is None:
            rr = ReleaseRow(
                key=key,
                subject=base or fname or stem,
                poster=a.from_addr,
                date_unix=a.date_unix if a.date_unix > 0 else 0,
                total_bytes=0,
            )
            by_key[key] = rr
            order.append(key)
            per_file_totals[key] = {}

        rr.articles.append(a)
        rr.total_bytes += a.bytes
        rr.segments_have += 1
        if a.date_unix > 0 and (rr.date_unix == 0 or a.date_unix < rr.date_unix):
            rr.date_unix = a.date_unix

        # Erwartete Segment-Zahl: pro distinct file-name den höchsten
        # gesehenen part_total addieren. Files ohne (N/M) tragen nichts bei.
        if parsed.part_total:
            file_totals = per_file_totals[key]
            prev = file_totals.get(fname, 0)
            if parsed.part_total > prev:
                file_totals[fname] = parsed.part_total

    for key, file_totals in per_file_totals.items():
        rr = by_key[key]
        rr.file_count = len(file_totals) if file_totals else _distinct_filenames(rr)
        rr.segments_expected = sum(file_totals.values())

    return [by_key[k] for k in order]


def _distinct_filenames(rr: ReleaseRow) -> int:
    """Fallback: keine (N/M)-Infos verfügbar → Dateien nachzählen."""
    seen: set[str] = set()
    for a in rr.articles:
        parsed = parse_subject(a.subject)
        seen.add(extract_filename(parsed.stem or a.subject))
    return len(seen)
