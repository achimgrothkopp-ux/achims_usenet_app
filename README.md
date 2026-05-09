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
.venv/bin/python -m core.nntp_client search "term" --group de.alt.test
```

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

| Eingabe          | Wirkung                                      |
| ---------------- | -------------------------------------------- |
| Klick auf Gruppe | Lädt Header in die Tabelle                   |
| Doppelklick      | Lädt Artikel-Body (oder yEnc-Hinweis)        |
| `J` / `K`        | nächster / vorheriger Artikel                |
| `Space`          | aktuellen Artikel (de-)markieren             |
| `Sync`-Button    | inkrementeller Header-Sync ab `last_seen`    |
| `NZB speichern…` | markierte Segmente → `.nzb` auf Platte       |
| `→ SABnzbd`      | NZB direkt in SAB-Queue schicken             |
| `Ctrl+,`         | Einstellungs-Dialog                          |

## Layout

```
usenet/
├── core/
│   ├── nntp_client.py   # pynntp-Wrapper + Connection-Pool
│   ├── header_cache.py  # SQLite + FTS5
│   ├── nzb_builder.py   # markierte Artikel → NZB-XML
│   └── logging_setup.py
├── backend/
│   └── sabnzbd.py       # async httpx-Client gegen SAB JSON-API
├── gui/
│   ├── main_window.py
│   ├── group_panel.py
│   ├── header_view.py   # QTableView + lazy Model + Checkboxen
│   ├── article_view.py
│   ├── queue_panel.py   # SABnzbd-Queue mit 2s-Polling
│   ├── health_indicator.py
│   └── settings_dialog.py
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
- [ ] Phase 7 – Newznab-Indexer (optional)
