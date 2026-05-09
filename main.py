from __future__ import annotations

import asyncio
import logging
import sys

import qasync
from PySide6.QtWidgets import QApplication

from backend.sabnzbd import SABnzbdClient
from config import load as load_config
from core import header_cache, nntp_client
from core.logging_setup import configure as configure_logging
from gui.main_window import MainWindow


def main() -> None:
    cfg = load_config()
    log_file = configure_logging(cfg.logging.level, cfg.logging.log_dir)

    log = logging.getLogger("usenet-app")
    log.info("Starte Usenet-App – Logfile: %s", log_file)
    log.info("Config-Quelle: %s", cfg.source_path)

    app = QApplication(sys.argv)
    app.setApplicationName("Usenet-App")
    app.setOrganizationName("local")

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    cache = header_cache.HeaderCache(cfg.storage.header_cache_path)
    cache.init_schema()
    pool = nntp_client.NNTPPool(cfg.nntp)
    sab = SABnzbdClient(cfg.sabnzbd)

    window = MainWindow(cfg, cache, pool, sab=sab)
    window.show()

    app_close_event = asyncio.Event()
    app.aboutToQuit.connect(app_close_event.set)

    async def _wait_and_cleanup() -> None:
        await app_close_event.wait()
        window.shutdown()

    with loop:
        loop.run_until_complete(_wait_and_cleanup())

    log.info("Shutdown – schließe Pool und Cache")
    pool.close()
    cache.close()
    # sab.aclose() würde hier die anyio-Cancel-Scope brauchen, aber
    # qasync hat den Loop schon abgebaut – Prozess-Exit räumt die
    # offenen Sockets sauber weg (gleiches Muster wie bei NNTPPool).


if __name__ == "__main__":
    main()
