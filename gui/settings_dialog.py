from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import Config, NNTPConfig, SABnzbdConfig

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Editor für NNTP- und SABnzbd-Sektionen der config.toml.

    Andere Sektionen (storage, logging) bleiben vom Dialog unberührt
    und werden beim Save aus der Datei wieder übernommen.
    """

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("Einstellungen")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_nntp_box())
        layout.addWidget(self._build_sab_box())
        info = QLabel(
            "Änderungen werden in ~/.config/usenet-app/config.toml "
            "geschrieben und greifen beim nächsten App-Start.",
            self,
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888;")
        layout.addWidget(info)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_nntp_box(self) -> QGroupBox:
        box = QGroupBox("NNTP-Server", self)
        form = QFormLayout(box)

        n = self._cfg.nntp
        self._nntp_host = QLineEdit(n.host, box)
        self._nntp_port = QSpinBox(box)
        self._nntp_port.setRange(1, 65535)
        self._nntp_port.setValue(n.port)
        self._nntp_tls = QCheckBox("TLS (Port 563)", box)
        self._nntp_tls.setChecked(n.use_tls)
        self._nntp_user = QLineEdit(n.username, box)
        self._nntp_pass = QLineEdit(n.password, box)
        self._nntp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._nntp_conns = QSpinBox(box)
        self._nntp_conns.setRange(1, 64)
        self._nntp_conns.setValue(n.connections)
        self._nntp_conns.setSuffix(" parallel")

        form.addRow("Host", self._nntp_host)
        form.addRow("Port", self._nntp_port)
        form.addRow("", self._nntp_tls)
        form.addRow("Benutzer", self._nntp_user)
        form.addRow("Passwort", self._nntp_pass)
        form.addRow("Connections", self._nntp_conns)
        return box

    def _build_sab_box(self) -> QGroupBox:
        box = QGroupBox("SABnzbd", self)
        form = QFormLayout(box)
        s = self._cfg.sabnzbd
        self._sab_url = QLineEdit(s.url, box)
        self._sab_url.setPlaceholderText("http://127.0.0.1:8080")
        self._sab_key = QLineEdit(s.api_key, box)
        self._sab_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._sab_key.setPlaceholderText("API-Key aus SABnzbd → Config → General")

        form.addRow("URL", self._sab_url)
        form.addRow("API-Key", self._sab_key)
        return box

    # ---- Public ---------------------------------------------------------

    def collected_nntp(self) -> NNTPConfig:
        return NNTPConfig(
            host=self._nntp_host.text().strip(),
            port=int(self._nntp_port.value()),
            use_tls=self._nntp_tls.isChecked(),
            username=self._nntp_user.text(),
            password=self._nntp_pass.text(),
            connections=int(self._nntp_conns.value()),
        )

    def collected_sab(self) -> SABnzbdConfig:
        return SABnzbdConfig(
            url=self._sab_url.text().strip().rstrip("/"),
            api_key=self._sab_key.text().strip(),
        )
