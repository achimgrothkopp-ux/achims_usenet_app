# Achims Usenet-App – Projektplan

Desktop-App für Kali Linux: Newsgroup-Reader (Text + Binär) mit NZB-Erstellung
aus markierten Artikeln. Downloads werden an SABnzbd delegiert.

## Architektur-Modell (Hybrid)

| Aufgabe                              | Wo                    | Wie                                            |
| ------------------------------------ | --------------------- | ---------------------------------------------- |
| Text-Gruppen lesen                   | direkt via NNTP       | `pynntp` zum Newshosting-Server                |
| Binär-Gruppen-Header lesen           | direkt via NNTP       | nur abonnierte Gruppen, inkrementell           |
| Binär-Suche über *alle* Gruppen      | Newznab-Indexer-API   | externer Service (Phase 7, später)             |
| NZB aus markierten Artikeln bauen    | lokal                 | XML aus Header-Cache via `lxml`                |
| Download + Postprocessing            | SABnzbd               | JSON-API, NZB einreichen + Queue pollen        |

## Tech-Stack

- **Python 3.13** (Kali-System-Python; `nntplib` ist in 3.13 entfernt → `pynntp`)
- **GUI:** PySide6 (LGPL, native `QTableView` für große Header-Listen)
- **Async:** `asyncio` + `qasync` für die Brücke zur Qt-Eventloop
- **Storage:** SQLite mit FTS5 für Header-Suche
- **HTTP-Client:** `httpx` (async) für SABnzbd-API und später Newznab
- **NZB-XML:** `lxml`

## Modul-Layout

```
usenet/
├── core/
│   ├── nntp_client.py        # pynntp + Connection-Pool zum Newshosting
│   ├── header_cache.py       # SQLite (groups, articles, FTS5)
│   └── nzb_builder.py        # markierte Artikel → NZB-XML
├── indexer/
│   └── newznab.py            # Newznab-API-Client (Phase 7)
├── backend/
│   └── sabnzbd.py            # JSON-API: NZB einreichen, Status pollen
├── gui/
│   ├── main_window.py
│   ├── group_panel.py        # linke Sidebar: abonnierte Gruppen
│   ├── header_view.py        # Header-Tabelle + Suchleiste
│   ├── search_view.py        # Indexer-Suche (Phase 7)
│   └── queue_panel.py        # SABnzbd-Download-Queue live
├── config.py                 # ~/.config/usenet-app/config.toml
└── main.py
```

## Datenflüsse

**Text-Gruppen:**
`NNTP GROUP/OVER` → `header_cache (SQLite+FTS5)` → `header_view`
→ Artikel-Body via `NNTP ARTICLE` → Anzeige.

**Binär (zwei Wege):**

1. *Suche per Indexer (später):*
   `search_view` → Newznab-API → NZB → `sabnzbd.py` → SAB-Queue.
2. *Eigene Markierung:*
   Header in `header_view` markieren → `nzb_builder.py` baut XML
   → an SABnzbd schicken.

## Provider-Setup

- **NNTP:** Newshosting, 30 parallele Connections im Tarif.
  Reader-Default: 8 (Header-Sync braucht keinen großen Pool;
  die 30 sind primär für SABnzbds Downloader).
- **SABnzbd:** lokal installiert (`sabnzbdplus` aus Kali-Repo, v4.5.4).
  Web-UI auf `http://127.0.0.1:8080`, API-Key kommt aus dem Setup-Wizard.
- **Newznab-Indexer:** noch nicht eingerichtet – Phase 7 / optional.

---

## Phasen

### Phase 1 – Fundament  *(1–2 Tage)*

Projektgerüst steht, Config & Logging laufen, GUI öffnet leeres Fenster.

- [x] Verzeichnisstruktur (`core/`, `indexer/`, `backend/`, `gui/`)
- [x] `requirements.txt` (PySide6, qasync, pynntp, httpx, lxml)
- [ ] Deps in `.venv` installieren
- [ ] `config.py` mit TOML-Loader (`~/.config/usenet-app/config.toml`)
- [ ] Logging (RotatingFileHandler + Console, Default INFO)
- [ ] Leeres `MainWindow` (PySide6): Menubar, Statusbar, 3 Splitter-Panels
- [ ] `qasync`-Setup: asyncio + Qt-Eventloop verheiratet
- **Akzeptanz:** App startet, zeigt leere Panels, Config lädt.

### Phase 2 – NNTP-Anbindung & Header-Cache  *(2–3 Tage)*

Verbinde mit Newshosting, lade Gruppen-Header in lokale DB.

- `nntp_client.py`: `pynntp`-Wrapper mit Connection-Pool (8 TLS-Verbindungen, Port 563)
- Authentifizierung gegen Newshosting + Capability-Check
- `header_cache.py`: SQLite-Schema
  ```
  groups(name, last_article_seen, subscribed)
  articles(group, number, message_id, subject, from, date, bytes, lines)
  + FTS5-Virtual-Table auf subject+from
  ```
- Gruppen-Liste fetchen (`LIST ACTIVE`), persistieren
- Inkrementeller Header-Sync via `OVER first-last` in Batches à 10k
- CLI-Smoketest: `python -m core.nntp_client --sync de.alt.test`
- **Akzeptanz:** Eine abonnierte Gruppe komplett in SQLite indexiert;
  FTS5-Query liefert Treffer.

### Phase 3 – Reader-GUI  *(2–3 Tage)*

Header lesen, durchsuchen, Artikel-Bodies anzeigen.

- `group_panel.py` (links): Baum aller verfügbaren Gruppen + Subscribe-Toggle
- `header_view.py` (mitte): `QTableView` mit `QAbstractTableModel`,
  lazy aus SQLite paginiert. Sortierung + Filter.
- Suchleiste oben → FTS5-Query, Live-Resultate
- Doppelklick auf Artikel → Body via `ARTICLE message-id` async laden
  → Detail-Pane (Text lesbar, Binär = yEnc-Hinweis)
- "Sync now"-Button pro Gruppe (inkrementell ab `last_article_seen`)
- **Akzeptanz:** Gruppe anklicken → Header sehen → Volltextsuche → Posting lesen.

### Phase 4 – NZB-Builder  *(1 Tag)*

Markierte Binär-Artikel → gültige NZB-Datei.

- Mehrfachauswahl mit Checkbox-Spalte im `header_view`
- Heuristik: Artikel mit gleichem Subject-Stamm
  (z.B. `"Foo.rar" yEnc (1/42)` … `(42/42)`) zu einem File-Set gruppieren
- `nzb_builder.py`: NZB-XML via `lxml`
- Validierung gegen NZB-Schema
- "Save NZB…"-Dialog + Direkt-Button "→ SABnzbd"
- **Akzeptanz:** 5 zusammengehörige Segmente markieren → erzeugte `.nzb` ist gültig.

### Phase 5 – SABnzbd-Integration  *(1–2 Tage)*

NZBs übergeben, Queue live anzeigen.

- `backend/sabnzbd.py`: async `httpx`-Client gegen SAB-API
  - `addurl` / `addfile` zum NZB-Einreichen
  - `queue` / `history` zum Pollen
- `queue_panel.py` (unten): Tabelle mit aktiver Queue,
  Polling alle 2 s, Pause/Resume/Delete
- API-Key & SAB-URL aus `config.toml`
- Verbindungs-Health-Indicator in Statusbar (grün/rot)
- **Akzeptanz:** Klick "→ SABnzbd" → Job in Queue-Panel, läuft, Datei landet im SAB-Folder.

### Phase 6 – Politur  *(1–2 Tage)*  ✅

- Settings-Dialog (NNTP-Server, SAB-URL+Key, Connection-Anzahl)
- Persistierung von Spaltenbreiten/Splitter-Positionen
- Keyboard-Shortcuts (J/K next/prev, Space markieren)
- Desktop-Datei für Anwendungsmenü
- README mit Setup-Anleitung

### Phase 6+ – Großgruppen-Handling  *(Nachträge nach realem Betrieb)*  ✅

Reale Nutzung gegen Mio-Gruppen hat eine Reihe von Kanten freigelegt –
hier zusammengefasst, weil zusammenhängend.

- **Async-Dialoge** (a740eb8): warn_later/info_later/critical_later
  via QTimer.singleShot, confirm_async non-modal via Future. Vermeidet
  „Cannot enter into task X while task Y is being executed", wenn ein
  modaler QMessageBox aus einem qasync-Task aufgerufen wird.
- **Sync-Lock** synchron schon im _on_sync_requested, sonst gewinnt ein
  Doppelklick das Rennen vor dem Task-Schedule.
- **`clear_group`** ohne Trigger pro Zeile – AFTER-DELETE auf articles
  würde bei 1.7M Zeilen 8+ Min im FTS-Index suchen; Bulk-Delete mit
  temporär gedropptem Trigger ist in 35 s durch.
- **SyncPlan** mit vier Modi (inkrementell / letzte N / seit Datum /
  Vollsync) und Pro-Lauf-Cap `max_articles`. Sync-Dialog mit Statistik
  und Schätzung beim Auswählen.
- **`find_article_at_date`** – Binary Search über `OVER`-Roundtrips
  mit 100er-Fenster (überbrückt Lücken in Article-Nummern). Erlaubt
  „Seit YYYY-MM-DD" als Sync-Startpunkt.
- **Abbrechbarer Sync** über `asyncio.Event`. Cancel stoppt nur das
  Ziehen neuer Chunks; laufende Fetches werden zu Ende persistiert.
  Pro-Chunk-Persistenz + monotones Präfix-Tracking garantieren, dass
  `last_seen` lückenfrei bleibt und Resume nahtlos weitermacht.
- **Parallel-Fetch** via `SyncPlan.parallel`. Eine asyncio.Queue
  verteilt Chunk-Indizes an N Worker, die parallel XOVER fahren;
  Inserts in SQLite sind hinter `state_lock` serialisiert. Live
  gemessen: 1,7× Speedup bei parallel=4 vs 1.
- **`articles.date_unix`** + Index + Backfill-Migration. NNTP-Date
  wird beim Insert in Unix-Sekunden geparst (`-1` = versucht-aber-
  ungültig, `0` = leerer Header). Header-Tabelle sortiert die
  Datums-Spalte jetzt chronologisch statt lexikografisch.
- **FTS-Suche per JOIN** – `search_articles()` liefert komplette
  ArticleRows in einem Query, statt N+1 Lookups nach jedem `SearchHit`.
- **pynntp-Workaround**: `LIST ACTIVE` wird von pynntp als
  `(name, low, high, status)` geparst, obwohl RFC 3977 §7.6.2.2
  `name high low status` liefert. Wir tauschen low/high in
  `NNTPPool.list_active` zurück, damit überall `low <= high` gilt.

### Phase 7 – Newznab-Indexer  *(optional, später)*

Sobald ein Indexer-Account existiert. Additiv – greift nicht in Phase 1–6 ein.

- `indexer/newznab.py`: Suche, Kategorien, NZB-Download
- `search_view.py`: separater Tab/Panel für Indexer-Suche
- Direkt-Übergabe an SABnzbd

### Phase 8 – Group-Browser  ✅

Beim Klick auf "Abonnieren…" musste man bisher den Gruppennamen exakt
kennen. Realisiert (98b0068):

- LIST ACTIVE per "Aktualisieren"-Button ziehbar (Newshosting liefert
  ~111k Gruppen in ~1,3 s, ~0,3 s Bulk-Insert) und in der
  `groups`-Tabelle persistiert. `upsert_groups_bulk` schont `subscribed`
  und `last_article_seen` – ein Refresh überschreibt also keinen Abo-
  Stand.
- `gui/group_browser.py`: flacher QTableView statt TreeView – bei 100k
  Gruppen ist Filter auf Name praktischer als Hierarchie-Navigation,
  und alphabetische Sortierung clustert `alt.binaries.*` / `de.alt.*`
  ohnehin zusammen.
- Spalten: Name, ≈ Artikel (high−low+1), Status, Abonniert. Eigene
  Sort-Role liefert numerische Werte für die Artikel-Spalte.
- Multi-Select (ExtendedSelection) + Buttons "Markierte abonnieren" /
  "Abbestellen". `subscribed_changed`-Signal triggert Refresh des
  linken GroupPanel.
- `QSortFilterProxyModel` mit case-insensitive Substring-Filter auf
  der Name-Spalte; "Nur Abonnierte zeigen"-Checkbox reduziert den
  Source-Bestand.

### Phase 9 – NZB-Import  ✅

Bisher entstanden NZBs nur aus markierten Headern (Phase 4). Jetzt
lassen sich auch vorhandene `.nzb`-Dateien von der Platte importieren
und direkt herunterladen (22e6ae4):

- `Datei → "NZB importieren…"` (Ctrl+O) öffnet einen Multi-Select-
  `QFileDialog` (`*.nzb`/`*.nzb.gz`).
- Dateien werden off-thread (`asyncio.to_thread`) gelesen und über das
  bestehende `SABnzbdClient.add_nzb_bytes` (`addfile`) an SAB übergeben –
  kein Backend-Code nötig, reine GUI-Erweiterung in `main_window.py`.
- Fehler werden pro Datei übersprungen (via `critical_later`, nicht
  modal), am Ende Status-Zusammenfassung + `queue_panel`-Refresh.

---

## Geschätzter Gesamtumfang

**~10–14 Arbeitstage** für eine solide v1 (Phase 1–6).

## Reihenfolge-Logik

Phase 1–3 = **Reader-Kern**, nutzbar auf sich allein gestellt
(Text-Newsgroups lesen funktioniert nach Phase 3).
Phase 4–5 macht daraus einen **Binär-Client**.
Nach jeder Phase ein lauffähiges Inkrement.
