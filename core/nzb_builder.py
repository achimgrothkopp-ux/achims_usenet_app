"""NZB-Builder: markierte Header-Cache-Artikel → gültiges NZB-XML."""
from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from lxml import etree

from core.header_cache import ArticleRow

log = logging.getLogger(__name__)

NZB_NAMESPACE = "http://www.newzbin.com/DTD/2003/nzb"
DOCTYPE = (
    '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" '
    '"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">'
)

# Inline-DTD für Sanity-Validierung. Bewusst minimal gehalten – die
# offizielle DTD hat denselben Aufbau.
_NZB_DTD_SOURCE = """
<!ELEMENT nzb (head?, file+)>
<!ATTLIST nzb xmlns CDATA #IMPLIED>
<!ELEMENT head (meta*)>
<!ELEMENT meta (#PCDATA)>
<!ATTLIST meta type CDATA #REQUIRED>
<!ELEMENT file (groups, segments)>
<!ATTLIST file
    poster  CDATA #REQUIRED
    date    CDATA #REQUIRED
    subject CDATA #REQUIRED>
<!ELEMENT groups (group+)>
<!ELEMENT group (#PCDATA)>
<!ELEMENT segments (segment+)>
<!ELEMENT segment (#PCDATA)>
<!ATTLIST segment
    bytes  CDATA #REQUIRED
    number CDATA #REQUIRED>
"""
_NZB_DTD = etree.DTD(io.BytesIO(_NZB_DTD_SOURCE.encode("ascii")))


# (N/M) am Ende, optional in Klammern, mit oder ohne Leerzeichen.
_PART_RE = re.compile(r"\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)\s*$")
# yEnc-Marker am Ende.
_YENC_RE = re.compile(r"\s*yEnc\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedSubject:
    stem: str          # Subject ohne (N/M) und ohne 'yEnc'
    part_no: int | None
    part_total: int | None


def parse_subject(subject: str) -> ParsedSubject:
    """Heuristik: trailing (N/M) und 'yEnc' abschneiden.

    Multipart-Subjects haben oft die Form
        "Foo.rar" yEnc (1/42)
    oder bei Multi-File-Posts
        [42/100] - "Foo.rar" yEnc (1/123)
    Wir interessieren uns für die *Segment*-Zahl, also das letzte
    `(part/total)` am Ende. Das `[file/total]` davor lassen wir im
    Stem stehen – es identifiziert die Datei.
    """
    # yEnc zuerst strippen — manche Poster setzen den Marker hinter das
    # `(N/M)`-Suffix ("Foo.rar (1/1) yEnc"), dann muss `(N/M)` als neuer
    # Trailer erkennbar werden.
    s = _YENC_RE.sub("", subject.strip()).rstrip()
    m = _PART_RE.search(s)
    part_no: int | None = None
    part_total: int | None = None
    if m:
        part_no = int(m.group(1))
        part_total = int(m.group(2))
        s = s[: m.start()].rstrip()
    # Falls der Poster yEnc *zwischen* "...." und (N/M) gesetzt hat:
    # nach dem (N/M)-Strip kann noch ein zweites yEnc übrig sein.
    s = _YENC_RE.sub("", s).rstrip()
    return ParsedSubject(stem=s, part_no=part_no, part_total=part_total)


@dataclass
class FileSet:
    """Ein File = mehrere Article-Segmente, gruppiert nach Subject-Stamm."""
    stem: str
    segments: list[tuple[int, ArticleRow]]  # (part_no, ArticleRow)
    poster: str
    date_unix: int
    groups: list[str]

    @property
    def display_subject(self) -> str:
        return self.stem


def group_articles(articles: list[ArticleRow]) -> list[FileSet]:
    """Articles zu File-Sets nach Subject-Stamm gruppieren.

    Reihenfolge der Files: Reihenfolge des ersten Auftretens.
    Innerhalb eines Files: Sortierung nach Part-Nummer (fehlt → 0).
    """
    files: dict[str, FileSet] = {}
    order: list[str] = []
    for a in articles:
        parsed = parse_subject(a.subject)
        key = parsed.stem or a.subject
        if key not in files:
            order.append(key)
            files[key] = FileSet(
                stem=key,
                segments=[],
                poster=a.from_addr,
                date_unix=_to_unix(a.date),
                groups=[],
            )
        fs = files[key]
        fs.segments.append((parsed.part_no or len(fs.segments) + 1, a))
        if a.group not in fs.groups:
            fs.groups.append(a.group)
        ts = _to_unix(a.date)
        if ts and (fs.date_unix == 0 or ts < fs.date_unix):
            fs.date_unix = ts

    for fs in files.values():
        fs.segments.sort(key=lambda pair: pair[0])

    return [files[k] for k in order]


def _to_unix(date_str: str) -> int:
    if not date_str:
        return 0
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return 0
    if dt is None:
        return 0
    return int(dt.timestamp())


def _strip_msgid_brackets(msgid: str) -> str:
    s = msgid.strip()
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1]
    return s


def build_nzb_xml(
    articles: list[ArticleRow],
    *,
    title: str | None = None,
    pretty: bool = True,
) -> bytes:
    """Liste markierter Articles → NZB-XML als bytes (UTF-8, mit Doctype)."""
    if not articles:
        raise ValueError("Keine Artikel angegeben")

    files = group_articles(articles)
    if not files:
        raise ValueError("Gruppierung lieferte keine Files")

    nsmap = {None: NZB_NAMESPACE}
    nzb_el = etree.Element("nzb", nsmap=nsmap)

    if title:
        head = etree.SubElement(nzb_el, "head")
        meta = etree.SubElement(head, "meta", type="title")
        meta.text = title

    for fs in files:
        file_el = etree.SubElement(
            nzb_el,
            "file",
            poster=fs.poster or "unknown",
            date=str(fs.date_unix or int(time.time())),
            subject=fs.display_subject,
        )
        groups_el = etree.SubElement(file_el, "groups")
        for grp in fs.groups:
            g = etree.SubElement(groups_el, "group")
            g.text = grp
        segs_el = etree.SubElement(file_el, "segments")
        for part_no, art in fs.segments:
            seg = etree.SubElement(
                segs_el,
                "segment",
                bytes=str(art.bytes),
                number=str(part_no),
            )
            seg.text = _strip_msgid_brackets(art.message_id)

    body = etree.tostring(
        nzb_el,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=pretty,
        doctype=DOCTYPE,
    )
    return body


def validate_nzb(xml_bytes: bytes) -> None:
    """Wirft etree.DocumentInvalid wenn die NZB nicht zur DTD passt."""
    parser = etree.XMLParser(load_dtd=False, no_network=True)
    doc = etree.fromstring(xml_bytes, parser)
    # DTD-Validation gegen die eigene Inline-DTD (Namespace ignorieren).
    # Wir bauen eine namespace-freie Kopie und prüfen die.
    stripped = _strip_namespace(doc)
    if not _NZB_DTD.validate(stripped):
        raise etree.DocumentInvalid(_NZB_DTD.error_log.filter_from_errors())


def _strip_namespace(elem: etree._Element) -> etree._Element:
    new_root = etree.Element(etree.QName(elem).localname)
    new_root.text = elem.text
    new_root.tail = elem.tail
    for k, v in elem.attrib.items():
        new_root.set(etree.QName(k).localname, v)
    for child in elem:
        new_root.append(_strip_namespace(child))
    return new_root
