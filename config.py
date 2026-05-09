from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import tomli_w

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "usenet-app"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "usenet-app"


@dataclass(frozen=True)
class NNTPConfig:
    host: str = "news.newshosting.com"
    port: int = 563
    use_tls: bool = True
    username: str = ""
    password: str = ""
    connections: int = 8


@dataclass(frozen=True)
class SABnzbdConfig:
    url: str = "http://127.0.0.1:8080"
    api_key: str = ""


@dataclass(frozen=True)
class StorageConfig:
    header_cache_path: Path = field(default_factory=lambda: DATA_DIR / "header_cache.sqlite3")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    log_dir: Path = field(default_factory=lambda: DATA_DIR / "logs")


@dataclass(frozen=True)
class Config:
    nntp: NNTPConfig = field(default_factory=NNTPConfig)
    sabnzbd: SABnzbdConfig = field(default_factory=SABnzbdConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: Path | None = None


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    nntp_raw = raw.get("nntp", {})
    sab_raw = raw.get("sabnzbd", {})
    storage_raw = raw.get("storage", {})
    log_raw = raw.get("logging", {})

    cache_override = storage_raw.get("header_cache_path", "").strip()
    storage = StorageConfig(
        header_cache_path=Path(cache_override).expanduser()
        if cache_override
        else DATA_DIR / "header_cache.sqlite3"
    )

    return Config(
        nntp=NNTPConfig(
            host=nntp_raw.get("host", NNTPConfig.host),
            port=int(nntp_raw.get("port", NNTPConfig.port)),
            use_tls=bool(nntp_raw.get("use_tls", NNTPConfig.use_tls)),
            username=nntp_raw.get("username", ""),
            password=nntp_raw.get("password", ""),
            connections=int(nntp_raw.get("connections", NNTPConfig.connections)),
        ),
        sabnzbd=SABnzbdConfig(
            url=sab_raw.get("url", SABnzbdConfig.url),
            api_key=sab_raw.get("api_key", ""),
        ),
        storage=storage,
        logging=LoggingConfig(level=log_raw.get("level", "INFO")),
        source_path=path,
    )


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Schreibt die konfigurierbaren Sektionen zurück nach TOML.

    Wir berühren nur Felder, die der Settings-Dialog anbieten soll.
    Andere Sektionen (storage, logging) werden aus der bestehenden
    Datei übernommen, falls vorhanden – sonst Defaults.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            existing = tomllib.load(fh)

    existing["nntp"] = {
        "host": cfg.nntp.host,
        "port": cfg.nntp.port,
        "use_tls": cfg.nntp.use_tls,
        "username": cfg.nntp.username,
        "password": cfg.nntp.password,
        "connections": cfg.nntp.connections,
    }
    existing["sabnzbd"] = {
        "url": cfg.sabnzbd.url,
        "api_key": cfg.sabnzbd.api_key,
    }
    # storage / logging unverändert lassen, falls schon da
    existing.setdefault("storage", {"header_cache_path": ""})
    existing.setdefault("logging", {"level": cfg.logging.level})

    with path.open("wb") as fh:
        tomli_w.dump(existing, fh)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def with_updates(
    cfg: Config,
    *,
    nntp: NNTPConfig | None = None,
    sabnzbd: SABnzbdConfig | None = None,
) -> Config:
    """Liefert eine Kopie von cfg mit ausgetauschten Sektionen."""
    new_nntp = nntp if nntp is not None else cfg.nntp
    new_sab = sabnzbd if sabnzbd is not None else cfg.sabnzbd
    return replace(cfg, nntp=new_nntp, sabnzbd=new_sab)
