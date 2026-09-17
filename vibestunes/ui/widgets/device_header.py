import threading
from typing import Optional
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMessageBox, QFrame
)
from PySide6.QtCore import Qt, Signal, QObject

from vibestunes.core.device import iPodDevice, eject_ipod, is_mount_readonly, remount_rw

class EjectWorkerSignals(QObject):
    step = Signal(str)
    finished = Signal(bool, str)

class DeviceHeaderWidget(QFrame):
    refresh_requested = Signal()
    eject_completed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DeviceHeader")
        self.current_device: Optional[iPodDevice] = None
        self._eject_thread: Optional[threading.Thread] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)

        # iPod Icon / Badge
        self.icon_label = QLabel("")
        self.icon_label.setStyleSheet("font-size: 32px; color: #89b4fa; padding-right: 4px;")
        layout.addWidget(self.icon_label)

        # Device info texts
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)

        title_row = QHBoxLayout()
        self.name_label = QLabel("No iPod Detected")
        self.name_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff;")
        self.status_badge = QLabel("Disconnected")
        self.status_badge.setStyleSheet("""
            background-color: #313244;
            color: #f38ba8;
            font-size: 11px;
            font-weight: bold;
            padding: 2px 8px;
            border-radius: 10px;
        """)
        title_row.addWidget(self.name_label)
        title_row.addWidget(self.status_badge)
        title_row.addStretch()
        info_layout.addLayout(title_row)

        self.details_label = QLabel("Connect your Rockbox iPod via USB to begin.")
        self.details_label.setStyleSheet("font-size: 12px; color: #a6adc8;")
        info_layout.addWidget(self.details_label)

        layout.addLayout(info_layout, stretch=1)

        # Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.remount_btn = QPushButton("⚠ Fix Read-Only")
        self.remount_btn.setToolTip("iPod is currently mounted read-only. Click to remount read-write.")
        self.remount_btn.setStyleSheet("""
            QPushButton {
                background-color: #f9e2af;
                color: #11111b;
                font-weight: bold;
                border-radius: 6px;
                padding: 7px 12px;
            }
            QPushButton:hover {
                background-color: #f5c2e7;
            }
        """)
        self.remount_btn.setVisible(False)
        self.remount_btn.clicked.connect(self._on_remount_clicked)
        btn_layout.addWidget(self.remount_btn)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Rescan for connected iPod")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        btn_layout.addWidget(self.refresh_btn)

        self.eject_btn = QPushButton("⏏ Eject iPod")
        self.eject_btn.setProperty("class", "danger")
        self.eject_btn.setStyleSheet("""
            QPushButton {
                background-color: #f38ba8;
                color: #11111b;
                font-weight: bold;
                border-radius: 6px;
                padding: 7px 16px;
            }
            QPushButton:hover {
                background-color: #eba0ac;
            }
            QPushButton:disabled {
                background-color: #313244;
                color: #585b70;
            }
        """)
        self.eject_btn.setEnabled(False)
        self.eject_btn.clicked.connect(self._on_eject_clicked)
        btn_layout.addWidget(self.eject_btn)

        layout.addLayout(btn_layout)

        self.setStyleSheet("""
            #DeviceHeader {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 10px;
            }
        """)

        self.eject_signals = EjectWorkerSignals()
        self.eject_signals.step.connect(self.status_badge.setText, Qt.QueuedConnection)
        self.eject_signals.finished.connect(self._on_eject_finished, Qt.QueuedConnection)

    def set_device(self, device: Optional[iPodDevice]):
        self.current_device = device
        if device:
            self.name_label.setText(device.model_name)
            is_ro = is_mount_readonly(device.mount_point)
            if is_ro:
                self.status_badge.setText("Read-Only (Locked)")
                self.status_badge.setStyleSheet("""
                    background-color: #452430;
                    color: #f9e2af;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 2px 8px;
                    border-radius: 10px;
                """)
                self.remount_btn.setVisible(True)
            else:
                self.status_badge.setText("Connected")
                self.status_badge.setStyleSheet("""
                    background-color: #1e3a2f;
                    color: #a6e3a1;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 2px 8px;
                    border-radius: 10px;
                """)
                self.remount_btn.setVisible(False)

            ver = device.version if device.version != "Unknown" else "Rockbox"
            ram = f" • {device.memory_mb}MB RAM" if device.memory_mb > 0 else ""
            self.details_label.setText(
                f"Rockbox {ver}{ram} • {device.filesystem.upper()} on {device.device_node or device.mount_point}"
            )
            self.eject_btn.setEnabled(True)
            self.eject_btn.setText("⏏ Eject iPod")
        else:
            self.name_label.setText("No iPod Detected")
            self.status_badge.setText("Disconnected")
            self.status_badge.setStyleSheet("""
                background-color: #313244;
                color: #f38ba8;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 10px;
            """)
            self.details_label.setText("Connect your Rockbox iPod via USB and click Refresh.")
            self.remount_btn.setVisible(False)
            self.eject_btn.setEnabled(False)
            self.eject_btn.setText("⏏ Eject iPod")

    def _on_remount_clicked(self):
        if not self.current_device:
            return
        node = self.current_device.device_node
        success, msg = remount_rw(node, self.current_device.mount_point)
        if success:
            self.status_badge.setText("Ready (RW)")
            self.details_label.setText("Filesystem remounted read-write successfully.")
            self.refresh_requested.emit()
        else:
            repair_hint = (
                f"To repair filesystem errors, run in terminal:\nsudo fsck.vfat -a {node}"
                if node
                else "Please reconnect your iPod or check disk permissions with your system disk utility."
            )
            QMessageBox.warning(
                self,
                "Remount Notice",
                f"Could not automatically remount read-write: {msg}\n\n{repair_hint}"
            )

    def _on_eject_clicked(self):
        if not self.current_device:
            return

        self.eject_btn.setEnabled(False)
        self.eject_btn.setText("Ejecting...")
        self.status_badge.setText("Flushing Cache...")
        self.status_badge.setStyleSheet("""
            background-color: #3e3223;
            color: #fab387;
            font-size: 11px;
            font-weight: bold;
            padding: 2px 8px;
            border-radius: 10px;
        """)

        dev = self.current_device
        def worker():
            success, msg = eject_ipod(dev, step_callback=lambda s: self.eject_signals.step.emit(s))
            self.eject_signals.finished.emit(success, msg)

        self._eject_thread = threading.Thread(target=worker, daemon=True)
        self._eject_thread.start()

    def _on_eject_finished(self, success: bool, message: str):
        if success:
            self.status_badge.setText("Safe to Disconnect")
            self.status_badge.setStyleSheet("""
                background-color: #1e3a2f;
                color: #a6e3a1;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 10px;
            """)
            self.details_label.setText("Device unmounted safely. You can unplug the USB cable now.")
            self.eject_btn.setEnabled(False)
            self.eject_btn.setText("Ejected")
            self.eject_completed.emit()
        else:
            self.status_badge.setText("Eject Failed")
            self.status_badge.setStyleSheet("""
                background-color: #3a1e23;
                color: #f38ba8;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 10px;
            """)
            self.eject_btn.setEnabled(True)
            self.eject_btn.setText("⏏ Eject iPod")
            QMessageBox.warning(self, "Eject Failed", f"Could not eject device:\n{message}")
