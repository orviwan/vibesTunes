import os
import threading
from typing import Optional, List, Set, Dict
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPushButton, QMessageBox, QStatusBar, QLabel
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer

from vibestunes.core.config import AppConfig
from vibestunes.core.device import (
    iPodDevice, detect_ipod, refresh_storage_quick, compute_detailed_storage,
    is_mount_readonly, remount_rw
)
from vibestunes.core.ipod_scanner import (
    iPodArtist, scan_ipod_music, find_and_delete_ipod_album,
    find_and_delete_ipod_artist, clean_trash
)
from vibestunes.core.plex_client import PlexManager, normalize_music_key
from vibestunes.core.sync_engine import SyncWorker, SyncTask, SyncPlaylistTask, DeleteTask
from vibestunes.ui.theme import DARK_STYLESHEET
from vibestunes.ui.widgets.device_header import DeviceHeaderWidget
from vibestunes.ui.widgets.storage_bar import StorageBarWidget
from vibestunes.ui.widgets.plex_browser import PlexBrowserWidget
from vibestunes.ui.widgets.playlist_browser import PlaylistBrowserWidget
from vibestunes.ui.widgets.sync_drawer import SyncDrawerWidget
from vibestunes.ui.widgets.queue_dialog import SyncQueueDialog
from vibestunes.ui.widgets.storage_analyzer_dialog import StorageAnalyzerDialog
from vibestunes.ui.widgets.settings_dialog import SettingsDialog

class MainWorkerSignals(QObject):
    detailed_storage_ready = Signal(object)  # iPodDevice
    ipod_scan_finished = Signal(list)        # List[iPodArtist]

class MainWindow(QMainWindow):
    def __init__(self, demo_mode: bool = False):
        super().__init__()
        self.is_demo_mode = demo_mode
        self.setWindowTitle("vibesTunes — iPod Rockbox & Plex Manager")
        self.resize(1100, 750)
        self.setStyleSheet(DARK_STYLESHEET)

        # Core State
        self.config = AppConfig.load()
        self.device: Optional[iPodDevice] = None
        self.plex = PlexManager(self.config.plex_url, self.config.plex_token)
        self.ipod_artists: List[iPodArtist] = []
        self.worker_signals = MainWorkerSignals()
        self.worker_signals.detailed_storage_ready.connect(self._on_detailed_storage_ready, Qt.QueuedConnection)
        self.worker_signals.ipod_scan_finished.connect(self._on_ipod_scan_finished, Qt.QueuedConnection)
        self._storage_scan_thread: Optional[threading.Thread] = None
        self._ipod_scan_thread: Optional[threading.Thread] = None
        self.sync_worker: Optional[SyncWorker] = None
        self.sync_thread: Optional[threading.Thread] = None
        self.queue_dialog: Optional[SyncQueueDialog] = None
        self.analyzer_dialog: Optional[StorageAnalyzerDialog] = None
        self._ignored_ejected_nodes: Set[str] = set()

        # Main Layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        # Top Bar: Settings & Window Controls
        top_bar = QHBoxLayout()
        app_title = QLabel("vibesTunes")
        app_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #89b4fa;")
        top_bar.addWidget(app_title)
        top_bar.addStretch()

        self.settings_btn = QPushButton("⚙ Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        top_bar.addWidget(self.settings_btn)

        main_layout.addLayout(top_bar)

        # Device Header & Eject Card
        self.header_widget = DeviceHeaderWidget()
        self.header_widget.refresh_requested.connect(self.scan_for_device)
        self.header_widget.eject_completed.connect(self._on_device_ejected)
        main_layout.addWidget(self.header_widget)

        # Storage Gauge
        self.storage_bar = StorageBarWidget()
        self.storage_bar.analyzer_requested.connect(self._show_storage_analyzer)
        main_layout.addWidget(self.storage_bar)

        # Tab Widget (Single Unified Music Library + Playlists)
        self.tabs = QTabWidget()

        # Music Library Tab (Unified Plex master + iPod management)
        self.plex_browser = PlexBrowserWidget(self.plex)
        self.plex_browser.sync_album_requested.connect(self._on_sync_album_requested)
        self.plex_browser.sync_artist_requested.connect(self._on_sync_artist_requested)
        self.plex_browser.remove_album_requested.connect(self._on_remove_album_requested)
        self.plex_browser.remove_artist_requested.connect(self._on_remove_artist_requested)
        self.plex_browser.rescan_ipod_requested.connect(self.refresh_all)
        self.plex_browser.refresh_requested.connect(self.refresh_all)
        self.tabs.addTab(self.plex_browser, "Music Library")

        # Playlists Tab (Plex & iPod)
        self.playlist_browser = PlaylistBrowserWidget(self.plex)
        self.playlist_browser.sync_playlist_requested.connect(self._on_sync_playlist_requested)
        self.playlist_browser.refresh_requested.connect(self.refresh_all)
        self.tabs.addTab(self.playlist_browser, "Playlists")

        main_layout.addWidget(self.tabs, stretch=1)

        # Sync Drawer
        self.sync_drawer = SyncDrawerWidget()
        self.sync_drawer.cancel_requested.connect(self._on_sync_cancelled)
        self.sync_drawer.view_queue_requested.connect(self._show_queue_dialog)
        main_layout.addWidget(self.sync_drawer)

        # Status bar
        self.statusBar().showMessage("Ready")

        # Periodic Device Polling Timer (detects plugged-in iPods and unplugged iPods)
        self._device_poll_timer = QTimer(self)
        self._device_poll_timer.setInterval(2500)
        self._device_poll_timer.timeout.connect(self._on_device_poll_tick)
        self._device_poll_timer.start()

        # Initial Scan & Connect (or activate Demo Mode)
        if self.is_demo_mode:
            self._activate_demo_mode()
        else:
            self.scan_for_device(force_mount=True)
            self._init_plex_connection()

    def _on_device_poll_tick(self):
        if self.is_demo_mode:
            return

        # Do not scan while an active transfer is in progress
        if self.sync_thread and self.sync_thread.is_alive() and self.sync_worker and not self.sync_worker.is_finished:
            return

        if self.device is None:
            dev = detect_ipod(
                self.config.custom_ipod_path,
                allow_auto_mount=True,
                ignored_nodes=self._ignored_ejected_nodes,
            )
            if dev:
                self.scan_for_device(force_mount=True)
        else:
            if not os.path.exists(self.device.mount_point):
                self._on_device_ejected()

    def _activate_demo_mode(self):
        self.is_demo_mode = True
        self.setWindowTitle("vibesTunes — [Demo Mode] iPod Rockbox & Plex Manager")
        from vibestunes.core.demo_data import DemoPlexManager, get_demo_device, get_demo_ipod_state
        self.plex = DemoPlexManager()
        self.device = get_demo_device()
        self.header_widget.set_device(self.device)
        self._update_storage_ui()
        on_ipod_albums, ipod_album_data, ipod_artist_album_counts, ipod_artists = get_demo_ipod_state()
        self.ipod_artists = ipod_artists
        self.plex_browser.plex = self.plex
        self._sync_ipod_album_badges()
        self.plex_browser.set_library("Lossless Music (Demo)")
        self.playlist_browser.plex = self.plex
        self.playlist_browser.set_mount_point(self.device.mount_point)
        self.plex_browser.set_ipod_mount(self.device.mount_point)
        self.playlist_browser.reload_all()
        if self.analyzer_dialog:
            self.analyzer_dialog.update_data(self.ipod_artists)
        self.statusBar().showMessage("🎭 Demo Mode active — Fictional music catalog loaded.")

    def scan_for_device(self, force_mount: bool = True):
        if self.is_demo_mode:
            return
        if force_mount:
            self._ignored_ejected_nodes.clear()
        self.statusBar().showMessage("Scanning for connected iPod...")
        self.device = detect_ipod(
            self.config.custom_ipod_path,
            allow_auto_mount=force_mount,
            ignored_nodes=self._ignored_ejected_nodes if not force_mount else None,
        )
        self.header_widget.set_device(self.device)

        if self.device:
            # Immediate quick storage update
            refresh_storage_quick(self.device)
            self._update_storage_ui()

            # Set playlist browser mount
            self.playlist_browser.set_mount_point(self.device.mount_point)
            self.plex_browser.set_ipod_mount(self.device.mount_point)

            # Start detailed storage computation in background
            self._refresh_storage()

            # Start scanning iPod music library in background
            self._start_ipod_scan()
            self.statusBar().showMessage(f"Connected: {self.device.model_name} at {self.device.mount_point}")
        else:
            self.ipod_artists = []
            self.storage_bar.set_storage(0, 0, 0, 0, 0)
            self.playlist_browser.set_mount_point("")
            self.plex_browser.set_ipod_mount("")
            self.plex_browser.update_ipod_known_albums(set(), {})
            self.statusBar().showMessage("No Rockbox iPod detected. Plug in your iPod via USB.")

    def _update_storage_ui(self):
        if not self.device:
            return
        st = self.device.storage
        self.storage_bar.set_storage(
            total=st.total,
            free=st.free,
            music=st.music,
            rockbox=st.rockbox,
            other=st.other,
        )

    def _on_detailed_storage_ready(self, device: iPodDevice):
        self._update_storage_ui()

    def _refresh_storage(self):
        if not self.device:
            return
        refresh_storage_quick(self.device)
        self._update_storage_ui()

        if self._storage_scan_thread and self._storage_scan_thread.is_alive():
            return

        dev = self.device
        def worker():
            compute_detailed_storage(dev)
            try:
                self.worker_signals.detailed_storage_ready.emit(dev)
            except (RuntimeError, AttributeError):
                pass

        self._storage_scan_thread = threading.Thread(target=worker, daemon=True)
        self._storage_scan_thread.start()

    def refresh_all(self):
        self.statusBar().showMessage("Refreshing Plex catalog and iPod content...")
        if not self.is_demo_mode:
            self.plex_browser.reload_library()
        self.playlist_browser.reload_all()
        self._start_ipod_scan()
        self._refresh_storage()

    def _start_ipod_scan(self):
        if not self.device or not self.device.mount_point:
            return
        if self._ipod_scan_thread and self._ipod_scan_thread.is_alive():
            return
        self.statusBar().showMessage("Scanning iPod music library...")
        mount = self.device.mount_point
        def worker():
            if not self.is_demo_mode:
                try:
                    from vibestunes.core.naming_sync import inspect_ipod_naming_alignment, apply_naming_alignment, repair_ipod_playlists
                    repair_ipod_playlists(Path(mount))
                    if self.plex.is_connected() and self.config.naming_pattern == "plex_exact":
                        proposals = inspect_ipod_naming_alignment(Path(mount), self.plex, self.config.plex_library)
                        if proposals:
                            apply_naming_alignment(proposals, ipod_mount=Path(mount))
                except Exception as e:
                    print(f"Auto-alignment background notice: {e}")

            artists = scan_ipod_music(mount)
            try:
                self.worker_signals.ipod_scan_finished.emit(artists)
            except (RuntimeError, AttributeError):
                pass

        self._ipod_scan_thread = threading.Thread(target=worker, daemon=True)
        self._ipod_scan_thread.start()

    def _on_ipod_scan_finished(self, artists: List[iPodArtist]):
        self.ipod_artists = artists
        self._sync_ipod_album_badges()
        self.playlist_browser.reload_ipod_playlists()
        if self.analyzer_dialog:
            self.analyzer_dialog.update_data(self.ipod_artists)
        self.statusBar().showMessage(f"iPod library: {len(artists)} artist(s) on device.")

    def _sync_ipod_album_badges(self):
        """Passes currently synced iPod albums, tracks, and counts to Plex browser and Playlist browser for visual checkmarks."""
        known: Set[str] = set()
        artist_counts: Dict[str, int] = {}
        ipod_album_data: Dict[str, Dict[str, Any]] = {}
        ipod_artist_tracks: Dict[str, List[Any]] = {}

        for artist in self.ipod_artists:
            norm_art = normalize_music_key(artist.name)
            if norm_art not in ipod_artist_tracks:
                ipod_artist_tracks[norm_art] = []

            for album in artist.albums:
                ipod_artist_tracks[norm_art].extend(album.tracks)
                key = normalize_music_key(artist.name, album.title)
                known.add(key)
                if key not in ipod_album_data:
                    ipod_album_data[key] = {
                        "track_count": len(album.tracks),
                        "tracks": list(album.tracks),
                    }
                else:
                    ipod_album_data[key]["track_count"] += len(album.tracks)
                    ipod_album_data[key]["tracks"].extend(album.tracks)

            artist_counts[norm_art] = len({k for k in known if k.startswith(f"{norm_art}::")})

        self.plex_browser.update_ipod_known_albums(known, artist_counts, ipod_album_data, ipod_artist_tracks)
        self.playlist_browser.update_ipod_tracks(ipod_artist_tracks)

    def _init_plex_connection(self):
        if self.is_demo_mode:
            return
        if self.config.plex_url and self.config.plex_token:
            success, msg = self.plex.connect()
            if success:
                self.plex_browser.set_library(self.config.plex_library)
                self.playlist_browser.reload_all()
                self.statusBar().showMessage(f"Plex: {msg}")
            else:
                self.statusBar().showMessage(f"Plex connection: {msg}")

    def _open_settings(self):
        dialog = SettingsDialog(self.config, self.plex, self)
        if dialog.exec():
            # Reload Plex if config changed
            self.plex = PlexManager(self.config.plex_url, self.config.plex_token)
            self.plex_browser.plex = self.plex
            self.playlist_browser.plex = self.plex
            self._init_plex_connection()
            # Rescan device with custom path if set
            self.scan_for_device()

    def _on_device_ejected(self):
        if self.device and self.device.device_node:
            self._ignored_ejected_nodes.add(self.device.device_node)
        self.device = None
        self.ipod_artists = []
        if self.analyzer_dialog:
            self.analyzer_dialog.update_data([])
        self.header_widget.set_device(None)
        self.storage_bar.set_storage(0, 0, 0, 0, 0)
        self.playlist_browser.set_mount_point("")
        self.plex_browser.set_ipod_mount("")
        self.plex_browser.update_ipod_known_albums(set(), {})
        self.statusBar().showMessage("iPod safely ejected. Safe to unplug!")

    def _show_storage_analyzer(self):
        if not self.device or not self.device.mount_point:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first to inspect storage.")
            return

        if self.analyzer_dialog is None:
            self.analyzer_dialog = StorageAnalyzerDialog(
                self.ipod_artists,
                self.device.mount_point,
                self
            )
            self.analyzer_dialog.delete_album_requested.connect(self._on_remove_album_requested)
            self.analyzer_dialog.delete_artist_requested.connect(self._on_remove_artist_requested)
            self.analyzer_dialog.show_album_in_library.connect(self._on_jump_to_album)
            self.analyzer_dialog.show_artist_in_library.connect(self._on_jump_to_artist)
        else:
            self.analyzer_dialog.update_data(self.ipod_artists)

        self.analyzer_dialog.show()
        self.analyzer_dialog.raise_()
        self.analyzer_dialog.activateWindow()

    def _on_jump_to_album(self, artist_name: str, album_title: str):
        self.tabs.setCurrentIndex(0)
        self.plex_browser.select_artist_and_album(artist_name, album_title)

    def _on_jump_to_artist(self, artist_name: str):
        self.tabs.setCurrentIndex(0)
        self.plex_browser.select_artist_and_album(artist_name)

    # --- Sync & Deletion Orchestration ---
    def _on_sync_album_requested(self, task: SyncTask):
        if not self.device:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return
        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return
        self._start_sync([task])

    def _on_sync_artist_requested(self, tasks: List[SyncTask]):
        if not self.device:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return
        if not tasks:
            return
        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return
        self._start_sync(tasks)

    def _on_sync_playlist_requested(self, task: SyncPlaylistTask):
        if not self.device:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return
        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return
        self._start_sync([task])

    def _on_remove_album_requested(self, artist_name: str, album_title: str):
        if not self.device or not self.device.mount_point:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return

        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return

        task = DeleteTask(artist_name=artist_name, album_title=album_title)
        self._start_sync([task])

    def _on_remove_artist_requested(self, artist_name: str):
        if not self.device or not self.device.mount_point:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return

        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return

        task = DeleteTask(artist_name=artist_name)
        self._start_sync([task])

    def _on_clean_trash_requested(self):
        if not self.device or not self.device.mount_point:
            QMessageBox.warning(self, "No iPod", "Please connect and mount your iPod first.")
            return
        if not self.is_demo_mode and is_mount_readonly(self.device.mount_point):
            self._prompt_readonly_recovery()
            return

        task = DeleteTask(clean_trash_only=True)
        self._start_sync([task])

    def _on_delete_completed(self, task: DeleteTask, success: bool, freed: int, msg: str):
        if success:
            self.statusBar().showMessage(msg)
            if task.clean_trash_only:
                if self.is_demo_mode and self.device:
                    self.device.storage.free = min(self.device.storage.total, self.device.storage.free + freed)
                    self.device.storage.used = max(0, self.device.storage.used - freed)
                    self._update_storage_ui()
            elif task.album_title and task.artist_name:
                key = normalize_music_key(task.artist_name, task.album_title)
                self.plex_browser.on_ipod_albums.discard(key)
                self.plex_browser.ipod_album_data.pop(key, None)
                norm_art = normalize_music_key(task.artist_name)
                self.plex_browser.ipod_artist_album_counts[norm_art] = len(
                    {k for k in self.plex_browser.on_ipod_albums if k.startswith(f"{norm_art}::")}
                )
                self.plex_browser._update_filter_button_counts()
                self.plex_browser._filter_artists()
                if self.plex_browser.selected_artist:
                    self.plex_browser._refresh_album_list_badges()
            elif task.artist_name:
                norm_art = normalize_music_key(task.artist_name)
                self.plex_browser.ipod_artist_album_counts[norm_art] = 0
                self.plex_browser.on_ipod_albums = {k for k in self.plex_browser.on_ipod_albums if not k.startswith(f"{norm_art}::")}
                self.plex_browser.ipod_album_data = {k: v for k, v in self.plex_browser.ipod_album_data.items() if not k.startswith(f"{norm_art}::")}
                self.plex_browser._update_filter_button_counts()
                self.plex_browser._filter_artists()
                if self.plex_browser.selected_artist:
                    self.plex_browser._refresh_album_list_badges()

            if self.is_demo_mode:
                if self.device and freed > 0 and not task.clean_trash_only:
                    self.device.storage.music = max(0, self.device.storage.music - freed)
                    self.device.storage.free = min(self.device.storage.total, self.device.storage.free + freed)
                    self.device.storage.used = max(0, self.device.storage.used - freed)
                    self._update_storage_ui()
            else:
                self._refresh_storage()
                self._start_ipod_scan()
        else:
            if "Read-only file system" in msg or "[Errno 30]" in msg:
                self._prompt_readonly_recovery()
            else:
                self.statusBar().showMessage(f"Delete failed: {msg}")

    def _prompt_readonly_recovery(self):
        reply = QMessageBox.question(
            self,
            "Filesystem Read-Only",
            "The iPod filesystem is currently mounted read-only by the operating system.\n\n"
            "Would you like vibesTunes to automatically remount it read-write now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        if reply == QMessageBox.Yes and self.device:
            success, remount_msg = remount_rw(self.device.device_node, self.device.mount_point)
            if success:
                self.statusBar().showMessage("iPod remounted read-write successfully.", 6000)
                self.scan_for_device()
            else:
                repair_hint = (
                    f"You can repair filesystem errors in a terminal with:\nsudo fsck.vfat -a {self.device.device_node}"
                    if self.device.device_node
                    else "Please reconnect your iPod or verify disk integrity using your system's disk utility."
                )
                QMessageBox.warning(
                    self,
                    "Remount Failed",
                    f"Could not remount read-write: {remount_msg}\n\n{repair_hint}"
                )

    def _start_sync(self, tasks: list):
        if self.sync_thread and self.sync_thread.is_alive() and self.sync_worker and not self.sync_worker.is_finished:
            for t in tasks:
                self.sync_worker.add_task(t)
            return

        if self.is_demo_mode:
            from vibestunes.core.demo_data import DemoSyncWorker
            self.sync_worker = DemoSyncWorker(tasks)
        else:
            self.sync_worker = SyncWorker(self.plex, self.device.mount_point, self.config)
            for t in tasks:
                self.sync_worker.add_task(t)

        self.sync_drawer.set_sync_active(True)

        # Wire signals with Qt.QueuedConnection to guarantee thread safety
        self.sync_worker.album_started.connect(self.sync_drawer.set_album_started, Qt.QueuedConnection)
        self.sync_worker.playlist_started.connect(self.sync_drawer.set_playlist_started, Qt.QueuedConnection)
        self.sync_worker.delete_started.connect(self.sync_drawer.set_delete_started, Qt.QueuedConnection)
        self.sync_worker.track_started.connect(self.sync_drawer.set_track_started, Qt.QueuedConnection)
        self.sync_worker.track_progress.connect(self.sync_drawer.set_track_progress, Qt.QueuedConnection)
        self.sync_worker.track_completed.connect(self._on_track_completed, Qt.QueuedConnection)
        self.sync_worker.queue_updated.connect(self.sync_drawer.set_queue_status, Qt.QueuedConnection)
        self.sync_worker.queue_changed.connect(self._on_queue_changed, Qt.QueuedConnection)
        self.sync_worker.task_enqueued.connect(self._on_task_enqueued, Qt.QueuedConnection)
        self.sync_worker.album_completed.connect(self._on_album_sync_completed, Qt.QueuedConnection)
        self.sync_worker.playlist_completed.connect(self._on_playlist_sync_completed, Qt.QueuedConnection)
        self.sync_worker.delete_completed.connect(self._on_delete_completed, Qt.QueuedConnection)
        self.sync_worker.sync_finished.connect(self._on_sync_finished, Qt.QueuedConnection)

        if self.queue_dialog:
            self._connect_worker_to_queue_dialog(self.sync_worker)

        self.sync_thread = threading.Thread(target=self.sync_worker.run, daemon=True)
        self.sync_thread.start()
        self.statusBar().showMessage(f"Starting sync of {len(tasks)} item(s)...")

    def _show_queue_dialog(self):
        if self.queue_dialog is None:
            self.queue_dialog = SyncQueueDialog(self)
            self.queue_dialog.remove_task_requested.connect(self._on_remove_queued_task)
            self.queue_dialog.clear_queue_requested.connect(self._on_clear_sync_queue)
            self.queue_dialog.cancel_all_requested.connect(self._on_sync_cancelled)

        if self.sync_worker:
            self._connect_worker_to_queue_dialog(self.sync_worker)
            curr, queued = self.sync_worker.get_queue_snapshot()
            self.queue_dialog.update_queue(queued)
            self.queue_dialog.set_active_state(self.sync_worker.get_active_status())
        elif self.queue_dialog:
            self.queue_dialog.set_sync_finished()

        self.queue_dialog.show()
        self.queue_dialog.raise_()
        self.queue_dialog.activateWindow()

    def _connect_worker_to_queue_dialog(self, worker: SyncWorker):
        if not self.queue_dialog:
            return
        if getattr(worker, "_queue_dialog_connected", False):
            return
        worker._queue_dialog_connected = True
        worker.album_started.connect(self.queue_dialog.set_active_album, Qt.QueuedConnection)
        worker.playlist_started.connect(self.queue_dialog.set_active_playlist, Qt.QueuedConnection)
        worker.delete_started.connect(self.queue_dialog.set_active_delete, Qt.QueuedConnection)
        worker.track_started.connect(self.queue_dialog.set_track_started, Qt.QueuedConnection)
        worker.track_progress.connect(self.queue_dialog.set_track_progress, Qt.QueuedConnection)
        worker.queue_changed.connect(self.queue_dialog.update_queue, Qt.QueuedConnection)

    def _on_task_enqueued(self, task: Any, pos: int):
        task_name = getattr(task, "display_title", "item")
        self.sync_drawer.show_enqueue_notice(task_name, pos)
        self.statusBar().showMessage(f"Added '{task_name}' to sync queue (Position #{pos})")

    def _on_queue_changed(self, queued_tasks: list):
        if self.queue_dialog:
            self.queue_dialog.update_queue(queued_tasks)

        queued_album_keys = {t.album_key for t in queued_tasks if isinstance(t, SyncTask)}
        queued_pl_keys = {t.playlist_key for t in queued_tasks if isinstance(t, SyncPlaylistTask)}

        active_alb_key = None
        active_pl_key = None
        if self.sync_worker and self.sync_worker.current_task:
            ct = self.sync_worker.current_task
            if isinstance(ct, SyncTask):
                active_alb_key = ct.album_key
            elif isinstance(ct, SyncPlaylistTask):
                active_pl_key = ct.playlist_key

        self.plex_browser.update_sync_queue_keys(queued_album_keys, active_alb_key)
        self.playlist_browser.update_sync_queue_keys(queued_pl_keys, active_pl_key)

    def _on_remove_queued_task(self, index: int):
        if self.sync_worker:
            removed = self.sync_worker.remove_task(index)
            if removed:
                name = getattr(removed, "display_title", "item")
                self.statusBar().showMessage(f"Removed '{name}' from sync queue.")

    def _on_clear_sync_queue(self):
        if self.sync_worker:
            cleared = self.sync_worker.clear_queue()
            self.statusBar().showMessage(f"Cleared {cleared} pending items from sync queue.")

    def _on_album_sync_completed(self, album_title: str, tracks: int, bytes_transferred: int):
        if self.is_demo_mode:
            if self.device and bytes_transferred > 0:
                self.device.storage.music += bytes_transferred
                self.device.storage.free = max(0, self.device.storage.free - bytes_transferred)
                self.device.storage.used += bytes_transferred
                self._update_storage_ui()
            if self.plex_browser.selected_artist:
                art_name = self.plex_browser.selected_artist.name
                key = normalize_music_key(art_name, album_title)
                self.plex_browser.on_ipod_albums.add(key)
                self.plex_browser.ipod_album_data[key] = {
                    "track_count": tracks,
                    "tracks": []
                }
                norm_art = normalize_music_key(art_name)
                self.plex_browser.ipod_artist_album_counts[norm_art] = len(
                    {k for k in self.plex_browser.on_ipod_albums if k.startswith(f"{norm_art}::")}
                )
                self.plex_browser._update_filter_button_counts()
                self.plex_browser._filter_artists()
                self.plex_browser._refresh_album_list_badges()
                if self.plex_browser.selected_album and self.plex_browser.selected_album.title == album_title:
                    self.plex_browser._update_album_actions()
                    if self.plex_browser.current_tracks:
                        self.plex_browser._render_tracks_table(self.plex_browser.current_tracks)
        else:
            self._refresh_storage()
            self._start_ipod_scan()

    def _on_playlist_sync_completed(self, playlist_title: str, tracks: int, bytes_transferred: int):
        if not self.is_demo_mode:
            self.playlist_browser.reload_ipod_playlists()

    def _on_track_completed(self, title: str, success: bool, msg: str):
        if not success:
            if "404" in msg or "Missing on Plex" in msg:
                self.statusBar().showMessage(f"Notice: '{title}' missing on Plex server (HTTP 404)", 6000)
            else:
                self.statusBar().showMessage(f"Notice: '{title}' - {msg}", 5000)

    def _on_sync_finished(self, total_tracks: int, total_bytes: int, errors: list):
        self.sync_drawer.set_sync_active(False)
        if self.queue_dialog:
            self.queue_dialog.set_sync_finished()
        if not self.is_demo_mode:
            self._refresh_storage()
            self._start_ipod_scan()
            self.playlist_browser.reload_ipod_playlists()
        self.plex_browser.update_sync_queue_keys(set(), None)
        self.playlist_browser.update_sync_queue_keys(set(), None)

        # Refresh currently viewed track tables to reflect updated sync and warning states
        if self.playlist_browser.selected_playlist and self.playlist_browser.current_tracks:
            self.playlist_browser._render_tracks_table(self.playlist_browser.current_tracks)
        if self.plex_browser.selected_album and self.plex_browser.current_tracks:
            self.plex_browser._refresh_album_list_badges()
            self.plex_browser._render_tracks_table(self.plex_browser.current_tracks)

        if errors:
            plex_404_count = sum(1 for e in errors if "404" in e or "Plex Server" in e)
            other_errors = len(errors) - plex_404_count
            if total_tracks == 0 and plex_404_count > 0:
                self.statusBar().showMessage(
                    f"Sync failed: 0 track(s) synced • ⚠ {plex_404_count} track(s) missing on Plex server (HTTP 404 - files missing on Plex host)",
                    15000
                )
            elif plex_404_count > 0 and other_errors == 0:
                self.statusBar().showMessage(
                    f"Sync complete: {total_tracks} track(s) synced • ⚠ {plex_404_count} track(s) missing on Plex server (HTTP 404)",
                    12000
                )
            elif plex_404_count > 0:
                self.statusBar().showMessage(
                    f"Sync complete: {total_tracks} track(s) synced • ⚠ {plex_404_count} missing on Plex, {other_errors} notice(s)",
                    12000
                )
            else:
                self.statusBar().showMessage(
                    f"Sync complete with {len(errors)} notice(s): {errors[0]}",
                    10000
                )
        else:
            mb = total_bytes / (1024 * 1024)
            self.statusBar().showMessage(
                f"Sync complete: {total_tracks} track(s) synced ({mb:.1f} MB) to iPod.",
                8000
            )

    def _on_sync_cancelled(self):
        if self.sync_worker:
            self.sync_worker.cancel()
            self.sync_drawer.set_sync_active(False)
            if self.queue_dialog:
                self.queue_dialog.set_sync_finished()
            self.plex_browser.update_sync_queue_keys(set(), None)
            self.playlist_browser.update_sync_queue_keys(set(), None)
            self.statusBar().showMessage("Sync cancelled.")

    def closeEvent(self, event):
        if self.sync_worker:
            self.sync_worker.cancel()
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=2.0)
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            event.accept()
            return
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)

