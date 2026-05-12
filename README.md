# Usenet-App

Desktop-Newsreader für Kali Linux: Text- und Binär-Gruppen lesen, Header-Cache
mit FTS5-Suche, NZB-Builder aus markierten Artikeln, Übergabe an SABnzbd.

## Voraussetzungen

- Python **3.13** (Kali-System-Python)
- SABnzbd 4.x (`sudo apt install sabnzbdplus`) – für Phase 5
- NNTP-Account mit TLS-Zugang (Konfiguration ist auf Newshosting eingestellt,
  funktioniert aber mit jedem RFC-3977-konformen Server)

## Setup

```sh
# 1) Repo klonen
git clone <repo> usenet
cd usenet

# 2) virtuelles Env + Dependencies
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) lokale Config anlegen
mkdir -p ~/.config/usenet-app
cp config.example.toml ~/.config/usenet-app/config.toml
chmod 600 ~/.config/usenet-app/config.toml
$EDITOR ~/.config/usenet-app/config.toml  # NNTP-Login eintragen
```

## SABnzbd einrichten

```sh
# Erststart erzeugt ~/.sabnzbd/sabnzbd.ini inkl. API-Key
sabnzbdplus --daemon -b 0 -s 127.0.0.1:8080

# API-Key auslesen und in config.toml übernehmen
grep '^api_key' ~/.sabnzbd/sabnzbd.ini
$EDITOR ~/.config/usenet-app/config.toml
```

Im SABnzbd-Web-UI (http://127.0.0.1:8080) muss zusätzlich der NNTP-Server
konfiguriert werden, sonst kann SAB nichts herunterladen. Der Login dort
ist derselbe wie in der `config.toml` unter `[nntp]`.

## Starten

```sh
# Direkt
.venv/bin/python main.py

# Oder als CLI-Smoketest gegen den Cache
.venv/bin/python -m core.nntp_client list-active
.venv/bin/python -m core.nntp_client sync de.alt.test
.venv/bin/python -m core.nntp_client sync de.alt.test --last-n 50000
.venv/bin/python -m core.nntp_client sync de.alt.test --since 2026-01-01 --parallel 4
.venv/bin/python -m core.nntp_client sync de.alt.test --full --max-articles 200000
.venv/bin/python -m core.nntp_client search "term" --group de.alt.test
```

`sync` kennt vier Modi (`--full` / `--last-n N` / `--since YYYY-MM-DD` /
ohne Flag = inkrementell ab last_seen), einen Pro-Lauf-Cap
(`--max-articles N`) und einen Parallel-Schalter (`--parallel N`, 1 =
sequenziell).

Die Desktop-Datei `usenet-app.desktop` lässt sich nach `~/.local/share/applications/`
kopieren, damit der Eintrag im Anwendungsmenü erscheint.

## Konfiguration

`~/.config/usenet-app/config.toml`:

```toml
[nntp]
host = "news.newshosting.com"
port = 563
use_tls = true
username = "..."
password = "..."
connections = 8

[sabnzbd]
url = "http://127.0.0.1:8080"
api_key = "..."

[storage]
header_cache_path = ""   # leer → ~/.local/share/usenet-app/header_cache.sqlite3

[logging]
level = "INFO"
```

Über `Datei → Einstellungen…` (Ctrl+,) lassen sich NNTP- und SAB-Werte
auch im GUI editieren – Änderungen werden nach `config.toml` geschrieben
und greifen beim nächsten Start.

## Bedienung

| Eingabe              | Wirkung                                                                 |
| -------------------- | ----------------------------------------------------------------------- |
| Klick auf Gruppe     | Lädt Header in die Tabelle                                              |
| Doppelklick          | Lädt Artikel-Body (oder yEnc-Hinweis)                                   |
| Klick auf Spalte     | Sortiert (Datum chronologisch via `date_unix`, Nr./Bytes numerisch)     |
| `J` / `K`            | nächster / vorheriger Artikel                                           |
| `Space`              | aktuellen Artikel (de-)markieren                                        |
| `Abonnieren…`-Button | Group-Browser: 100k+ Gruppen, Filter, Mehrfach-Subscribe                |
| `Sync…`-Button       | Sync-Dialog: Modus + Pro-Lauf-Cap + Parallel-Connections                |
| `Stop` (im Sync)     | Sync abbrechen – fertige Chunks bleiben persistent, Resume macht weiter |
| `NZB speichern…`     | markierte Segmente → `.nzb` auf Platte                                  |
| `→ SABnzbd`          | NZB direkt in SAB-Queue schicken                                        |
| `Ctrl+,`             | Einstellungs-Dialog                                                     |

### Großgruppen-Handling

Beim Sync-Klick öffnet sich ein Dialog mit Statistik (low/high/last_seen
und Gap) und vier Modi:

- **Inkrementell** – ab `last_seen+1`. Default, sobald die Gruppe schon
  einmal gesynct wurde.
- **Letzte N Artikel** – `max(low, high-N+1, last_seen+1)..high`. Default
  beim Erst-Sync (50 000) – verhindert, dass eine Mio-Gruppe stundenlang
  Backlog lädt.
- **Seit Datum** – Binary Search per `OVER` über die Article-Numbers, dann
  Sync ab der gefundenen Nummer. ~2–4 s Overhead, ~17 Roundtrips.
- **Vollsync** – Cache der Gruppe leeren, ab `low` syncen.

Quer dazu: **Pro Lauf höchstens N Artikel** als Cap (Rest holt der nächste
Sync), und **Parallele Connections** (1..pool_max) verteilt den
XOVER-Fetch auf mehrere NNTP-Sockets. Cancel-Button bricht laufende
Syncs sauber ab; bereits gefetchte Chunks werden noch zu Ende
persistiert, `last_seen` wandert lückenfrei nur über das fertige Präfix.

## Layout

```
usenet/
├── core/
│   ├── nntp_client.py   # pynntp-Wrapper + Pool, SyncPlan, parallele
│   │                    # XOVER-Worker, find_article_at_date
│   ├── header_cache.py  # SQLite + FTS5 + date_unix-Migration
│   ├── nzb_builder.py   # markierte Artikel → NZB-XML
│   ├── dates.py         # NNTP-Date-Parsing (RFC 2822 → unix)
│   └── logging_setup.py
├── backend/
│   └── sabnzbd.py       # async httpx-Client gegen SAB JSON-API
├── gui/
│   ├── main_window.py
│   ├── group_panel.py
│   ├── group_browser.py # LIST-ACTIVE-Cache + Filter + Multi-Subscribe
│   ├── header_view.py   # QTableView + lazy Model + Datums-Sortierung
│   ├── article_view.py
│   ├── queue_panel.py   # SABnzbd-Queue mit 2s-Polling
│   ├── health_indicator.py
│   ├── settings_dialog.py
│   ├── sync_dialog.py   # Modus/Cap/Parallel-Auswahl beim Sync
│   └── dialogs.py       # async-safe QMessageBox-Wrapper
├── config.py
└── main.py
```

## Daten- und Log-Pfade

- Konfiguration: `~/.config/usenet-app/config.toml`
- Header-Cache (SQLite, WAL-Mode): `~/.local/share/usenet-app/header_cache.sqlite3`
- Logfile (rotiert, 2 MB × 5): `~/.local/share/usenet-app/logs/usenet-app.log`
- Window-Layout: `QSettings` (Plattform-Default, auf Linux meist
  `~/.config/local/Usenet-App.conf`)

## Phasen-Stand

- [x] Phase 1 – Fundament (Config, Logging, MainWindow, qasync)
- [x] Phase 2 – NNTP + Header-Cache (FTS5)
- [x] Phase 3 – Reader-GUI (Group/Header/Article)
- [x] Phase 4 – NZB-Builder
- [x] Phase 5 – SABnzbd-Integration
- [x] Phase 6 – Politur (Settings, Layout-Persistenz, Shortcuts, Desktop-Datei)
- [x] Phase 6+ – Großgruppen-Handling: SyncPlan, Datums-Bisection,
      abbrechbarer Sync, Parallel-Fetch, `date_unix`-Spalte
- [x] Phase 8 – Group-Browser (LIST ACTIVE → filterbare Tabelle,
      Mehrfach-Subscribe)
- [ ] Phase 7 – Newznab-Indexer (optional)
