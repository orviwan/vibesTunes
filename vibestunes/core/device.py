"""iPod device detection, storage metrics, and safe ejection."""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Any, Set, List

@dataclass
class StorageBreakdown:
    total: int = 0
    used: int = 0
    free: int = 0
    music: int = 0
    rockbox: int = 0
    trash: int = 0
    other: int = 0

@dataclass
class iPodDevice:
    mount_point: str
    target: str = "Unknown"
    version: str = "Unknown"
    memory_mb: int = 0
    model_name: str = "iPod (Rockbox)"
    label: str = "IPOD"
    device_node: str = ""
    disk_node: str = ""
    filesystem: str = "vfat"
    storage: StorageBreakdown = None

    def __post_init__(self):
        if self.storage is None:
            self.storage = StorageBreakdown()

def get_target_model_name(target: str) -> str:
    target_lower = target.lower()
    mapping = {
        "ipod6g": "iPod Classic (6th/7th Gen)",
        "ipodvideo": "iPod Video (5th/5.5 Gen)",
        "ipodcolor": "iPod Photo / Color (4th Gen)",
        "ipod4g": "iPod 4th Gen (Click Wheel)",
        "ipod3g": "iPod 3rd Gen",
        "ipod1g2g": "iPod 1st/2nd Gen",
        "ipodmini1g": "iPod Mini (1st Gen)",
        "ipodmini2g": "iPod Mini (2nd Gen)",
        "ipodnano1g": "iPod Nano (1st Gen)",
        "ipodnano2g": "iPod Nano (2nd Gen)",
    }
    return mapping.get(target_lower, f"iPod ({target})")

def parse_rockbox_info(rockbox_dir: Path) -> Dict[str, Any]:
    info = {"target": "Unknown", "version": "Unknown", "memory": 0}
    info_file = rockbox_dir / "rockbox-info.txt"
    if not info_file.exists():
        return info
    try:
        with open(info_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" in line:
                    key, val = line.split(":", 1)
                    k = key.strip().lower()
                    v = val.strip()
                    if k == "target":
                        info["target"] = v
                    elif k == "version":
                        info["version"] = v
                    elif k == "memory":
                        try:
                            info["memory"] = int(v)
                        except ValueError:
                            pass
    except Exception:
        pass
    return info

def get_block_device_for_mount(mount_point: str) -> tuple[str, str, str]:
    """Returns (device_node, disk_node, filesystem)."""
    dev_node = ""
    disk_node = ""
    fstype = ""
    if not sys.platform.startswith("linux"):
        return dev_node, disk_node, fstype

    try:
        out = subprocess.check_output(["findmnt", "-n", "-o", "SOURCE,FSTYPE", mount_point], text=True).strip()
        parts = out.split()
        if len(parts) >= 1:
            dev_node = parts[0]
        if len(parts) >= 2:
            fstype = parts[1]
    except Exception:
        pass

    if dev_node and dev_node.startswith("/dev/"):
        try:
            parent = subprocess.check_output(["lsblk", "-n", "-o", "PKNAME", dev_node], text=True).strip()
            if parent:
                disk_node = f"/dev/{parent}"
            else:
                disk_node = dev_node
        except Exception:
            disk_node = dev_node
    return dev_node, disk_node, fstype

VOLUMES_DIR = Path("/Volumes")
MEDIA_DIR = Path("/media")

def find_candidate_mounts() -> list[Path]:
    candidates = []

    # Check macOS /Volumes/*
    if VOLUMES_DIR.is_dir():
        try:
            candidates.extend([d for d in VOLUMES_DIR.iterdir() if d.is_dir()])
        except Exception:
            pass

    # Check Windows drive letters (D: through Z:)
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            if drive.is_dir():
                candidates.append(drive)

    # Check /run/media/$USER/* (Linux)
    user = os.environ.get("USER", "")
    if user:
        p = Path(f"/run/media/{user}")
        if p.is_dir():
            try:
                candidates.extend([d for d in p.iterdir() if d.is_dir()])
            except Exception:
                pass

    # Check /media/* (Linux)
    if MEDIA_DIR.is_dir():
        try:
            candidates.extend([d for d in MEDIA_DIR.iterdir() if d.is_dir()])
        except Exception:
            pass
        if user and (MEDIA_DIR / user).is_dir():
            try:
                candidates.extend([d for d in (MEDIA_DIR / user).iterdir() if d.is_dir()])
            except Exception:
                pass

    # Check current mounts from /proc/mounts (Linux)
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 2:
                    mp = Path(fields[1])
                    if mp not in candidates and mp.is_dir():
                        if (mp / ".rockbox").is_dir() or (mp / "iPod_Control").is_dir():
                            candidates.append(mp)
    except Exception:
        pass
    return candidates

def auto_mount_unmounted_ipod(ignored_nodes: Optional[Set[str]] = None) -> list[Path]:
    """
    Scans block devices via lsblk for unmounted partitions matching an iPod
    (e.g., label 'IPOD' or device model containing 'ipod' or 'iflash'),
    and attempts to mount them via udisksctl. Returns list of newly mounted Paths.
    """
    if not sys.platform.startswith("linux"):
        return []

    if ignored_nodes is None:
        ignored_nodes = set()

    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,LABEL,FSTYPE,MOUNTPOINTS,MODEL,TRAN"],
            text=True,
            timeout=3,
        )
        data = json.loads(out)
    except Exception:
        return []

    mounted: list[Path] = []
    for dev in data.get("blockdevices", []):
        parent_model = (dev.get("model") or "").lower()
        tran = (dev.get("tran") or "").lower()
        is_ipod_disk = ("ipod" in parent_model or "iflash" in parent_model or tran == "usb")

        for child in dev.get("children", [dev]):
            name = child.get("name")
            if not name:
                continue
            dev_node = f"/dev/{name}"
            if dev_node in ignored_nodes or name in ignored_nodes:
                continue

            label = (child.get("label") or "").upper()
            fstype = (child.get("fstype") or "").lower()
            mps = [m for m in child.get("mountpoints", []) if m]

            # If unmounted and matches iPod characteristics (label IPOD or USB disk with FAT)
            if not mps and (label == "IPOD" or (is_ipod_disk and fstype in ("vfat", "fat32", "fat"))):
                try:
                    res = subprocess.run(
                        ["udisksctl", "mount", "-b", dev_node, "--no-user-interaction"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if res.returncode == 0:
                        mp_out = subprocess.check_output(
                            ["findmnt", "-n", "-o", "TARGET", dev_node],
                            text=True,
                            timeout=3,
                        ).strip()
                        if mp_out:
                            p = Path(mp_out)
                            if p.is_dir():
                                mounted.append(p)
                except Exception:
                    pass

    return mounted

def detect_ipod(
    custom_path: Optional[str] = None,
    allow_auto_mount: bool = True,
    ignored_nodes: Optional[Set[str]] = None,
) -> Optional[iPodDevice]:
    mounts = []
    if custom_path:
        p = Path(custom_path)
        if p.is_dir():
            mounts.append(p)
    mounts.extend(find_candidate_mounts())

    def _try_device_from_mount(m: Path) -> Optional[iPodDevice]:
        rockbox_dir = m / ".rockbox"
        if rockbox_dir.is_dir():
            info = parse_rockbox_info(rockbox_dir)
            target = info.get("target", "Unknown")
            version = info.get("version", "Unknown")
            memory = info.get("memory", 0)
            model_name = get_target_model_name(target)
            dev_node, disk_node, fstype = get_block_device_for_mount(str(m))

            label_name = m.name or m.drive or "IPOD"
            device = iPodDevice(
                mount_point=str(m),
                target=target,
                version=version,
                memory_mb=memory,
                model_name=model_name,
                label=label_name,
                device_node=dev_node,
                disk_node=disk_node,
                filesystem=fstype or "vfat",
            )
            refresh_storage_quick(device)
            return device
        return None

    seen = set()
    for m in mounts:
        m_resolved = str(m.resolve())
        if m_resolved in seen:
            continue
        seen.add(m_resolved)
        dev = _try_device_from_mount(m)
        if dev:
            return dev

    if allow_auto_mount:
        new_mounts = auto_mount_unmounted_ipod(ignored_nodes)
        for m in new_mounts:
            m_resolved = str(m.resolve())
            if m_resolved in seen:
                continue
            seen.add(m_resolved)
            dev = _try_device_from_mount(m)
            if dev:
                return dev

    return None

def refresh_storage_quick(device: iPodDevice) -> None:
    """Instantly gets total, used, free from statvfs."""
    try:
        usage = shutil.disk_usage(device.mount_point)
        device.storage.total = usage.total
        device.storage.free = usage.free
        device.storage.used = usage.used
        # Approximate breakdown until full scan completes
        device.storage.music = max(0, usage.used - 200 * 1024 * 1024)
        device.storage.rockbox = min(usage.used, 150 * 1024 * 1024)
        device.storage.other = max(0, usage.used - device.storage.music - device.storage.rockbox)
    except Exception:
        pass

def compute_detailed_storage(device: iPodDevice, progress_callback: Optional[Callable[[int], None]] = None) -> None:
    """Computes exact byte breakdown of Music, Rockbox, Trash, Other."""
    mp = Path(device.mount_point)
    if not mp.is_dir():
        return
    music_size = 0
    rockbox_size = 0
    trash_size = 0
    other_size = 0

    try:
        entries = list(mp.iterdir())
        total_entries = len(entries)
        for i, entry in enumerate(entries):
            if entry.name == ".rockbox":
                rockbox_size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            elif entry.name.startswith(".Trash"):
                trash_size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            elif entry.name.startswith("."):
                continue
            elif entry.is_dir():
                for f in entry.rglob("*"):
                    if f.is_file():
                        music_size += f.stat().st_size
            elif entry.is_file():
                other_size += entry.stat().st_size

            if progress_callback and total_entries > 0:
                progress_callback(int((i + 1) / total_entries * 100))

        device.storage.music = music_size
        device.storage.rockbox = rockbox_size
        device.storage.trash = trash_size
        device.storage.other = other_size
    except Exception:
        pass

def eject_ipod(device: iPodDevice, step_callback: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """
    Flushes cache, unmounts partition, and powers off the drive cleanly.
    Returns (success, message).
    """
    if step_callback:
        step_callback("Flushing unwritten data to iPod (syncing)...")

    # Platform-aware sync/flush
    try:
        if sys.platform == "darwin":
            subprocess.run(["sync"], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["sync", "-f", device.mount_point], check=False)
            subprocess.run(["sync"], check=False)
        elif sys.platform == "win32":
            pass
    except Exception:
        pass

    # Platform-aware unmount
    if step_callback:
        step_callback("Unmounting iPod filesystem...")

    if sys.platform == "darwin":
        try:
            res = subprocess.run(["diskutil", "eject", device.mount_point], capture_output=True, text=True)
            if res.returncode != 0:
                res2 = subprocess.run(["diskutil", "unmount", device.mount_point], capture_output=True, text=True)
                if res2.returncode != 0:
                    return False, f"Could not eject {device.mount_point}: {res.stderr or res2.stderr}"
        except Exception as e:
            return False, f"Ejection failed: {e}"

    elif sys.platform == "win32":
        try:
            drive_clean = device.mount_point.rstrip("\\/").rstrip(":")
            if len(drive_clean) == 1 and drive_clean.isalpha():
                ps_cmd = f"(New-Object -comObject Shell.Application).Namespace(17).ParseName('{drive_clean.upper()}:').InvokeVerb('Eject')"
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=6)
        except Exception:
            pass

    else:
        # Linux
        if device.device_node:
            res = subprocess.run(["udisksctl", "unmount", "-b", device.device_node, "--no-user-interaction"], capture_output=True, text=True)
            if res.returncode != 0:
                # Fallback to standard umount
                res2 = subprocess.run(["umount", device.mount_point], capture_output=True, text=True)
                if res2.returncode != 0:
                    return False, f"Could not unmount {device.device_node}: {res.stderr or res2.stderr}"
        else:
            res = subprocess.run(["umount", device.mount_point], capture_output=True, text=True)
            if res.returncode != 0:
                return False, f"Could not unmount {device.mount_point}: {res.stderr}"

        # Power off block device if available
        if device.disk_node:
            if step_callback:
                step_callback("Powering off USB device safely...")
            subprocess.run(["udisksctl", "power-off", "-b", device.disk_node, "--no-user-interaction"], capture_output=True, text=True)

    if step_callback:
        step_callback("Safe to disconnect your iPod!")
    return True, "iPod successfully unmounted and powered down. Safe to unplug!"

def is_mount_readonly(mount_point: str) -> bool:
    """Checks if the mount point is mounted read-only (either by mount options or write probe)."""
    if not mount_point:
        return False
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(["findmnt", "-n", "-o", "OPTIONS", mount_point], text=True).strip()
            opts = [o.strip() for o in out.split(",")]
            if "ro" in opts:
                return True
        except Exception:
            pass

    # Active write test probe
    test_file = Path(mount_point) / ".vibestunes_rw_probe"
    try:
        with open(test_file, "w") as f:
            f.write("rw")
        test_file.unlink(missing_ok=True)
        return False
    except OSError as e:
        if e.errno in (30, 13):  # EROFS Read-only file system or EACCES Permission denied
            return True
    except Exception:
        pass
    return False

def remount_rw(device_node: str, mount_point: Optional[str] = None) -> tuple[bool, str]:
    """
    Attempts to remount a partition read-write via udisksctl (Linux).
    Returns (success, message).
    """
    if not device_node:
        return False, "No device node available for remount."
    if not sys.platform.startswith("linux"):
        return False, "Automatic remount is only supported on Linux. Please check disk permissions or reconnect the device."
    try:
        subprocess.run(["udisksctl", "unmount", "-b", device_node, "--no-user-interaction"], capture_output=True, text=True, timeout=5)
        res = subprocess.run(["udisksctl", "mount", "-b", device_node, "--no-user-interaction"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return True, "iPod remounted read-write successfully."
        return False, res.stderr.strip() or "Failed to remount device."
    except Exception as e:
        return False, str(e)
