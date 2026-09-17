import tempfile
from pathlib import Path
from vibestunes.core.device import parse_rockbox_info, get_target_model_name, detect_ipod

def test_get_target_model_name():
    assert get_target_model_name("ipod6g") == "iPod Classic (6th/7th Gen)"
    assert get_target_model_name("ipodvideo") == "iPod Video (5th/5.5 Gen)"
    assert get_target_model_name("ipodmini1g") == "iPod Mini (1st Gen)"
    assert get_target_model_name("custom") == "iPod (custom)"

def test_parse_rockbox_info():
    with tempfile.TemporaryDirectory() as tmpdir:
        rockbox_dir = Path(tmpdir) / ".rockbox"
        rockbox_dir.mkdir()
        info_file = rockbox_dir / "rockbox-info.txt"
        info_file.write_text(
            "Target: ipod6g\n"
            "Memory: 64\n"
            "Version: 12345abcdef\n",
            encoding="utf-8"
        )

        parsed = parse_rockbox_info(rockbox_dir)
        assert parsed["target"] == "ipod6g"
        assert parsed["memory"] == 64
        assert parsed["version"] == "12345abcdef"

def test_detect_ipod_custom_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        rockbox_dir = Path(tmpdir) / ".rockbox"
        rockbox_dir.mkdir()
        info_file = rockbox_dir / "rockbox-info.txt"
        info_file.write_text("Target: ipod6g\nMemory: 64\nVersion: test_ver\n")

        dev = detect_ipod(custom_path=tmpdir)
        assert dev is not None
        assert dev.target == "ipod6g"
        assert dev.memory_mb == 64
        assert dev.model_name == "iPod Classic (6th/7th Gen)"

def test_is_mount_readonly_rw():
    from vibestunes.core.device import is_mount_readonly
    with tempfile.TemporaryDirectory() as tmpdir:
        # Normal writable directory should return False
        assert is_mount_readonly(tmpdir) is False

def test_remount_rw_no_node():
    from vibestunes.core.device import remount_rw
    success, msg = remount_rw("")
    assert success is False
    assert "No device node" in msg

def test_auto_mount_unmounted_ipod(monkeypatch):
    import json
    from vibestunes.core.device import auto_mount_unmounted_ipod

    mock_lsblk = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "model": "latform iPod Ada",
                "tran": "usb",
                "children": [
                    {
                        "name": "sda1",
                        "label": "IPOD",
                        "fstype": "vfat",
                        "mountpoints": []
                    }
                ]
            }
        ]
    })

    commands_executed = []

    def mock_check_output(cmd, text=True, timeout=None):
        if "lsblk" in cmd:
            return mock_lsblk
        if "findmnt" in cmd:
            return "/media/mock/IPOD"
        return ""

    def mock_run(cmd, capture_output=True, text=True, timeout=None):
        commands_executed.append(cmd)
        class MockRes:
            returncode = 0
            stdout = "Mounted /dev/sda1 at /media/mock/IPOD"
            stderr = ""
        return MockRes()

    monkeypatch.setattr("subprocess.check_output", mock_check_output)
    monkeypatch.setattr("subprocess.run", mock_run)

    with tempfile.TemporaryDirectory() as fake_mp:
        # Patch findmnt to return fake_mp so Path.is_dir() succeeds
        def mock_check_output_with_dir(cmd, text=True, timeout=None):
            if "lsblk" in cmd:
                return mock_lsblk
            if "findmnt" in cmd:
                return fake_mp
            return ""
        monkeypatch.setattr("subprocess.check_output", mock_check_output_with_dir)

        mounted = auto_mount_unmounted_ipod()
        assert len(mounted) == 1
        assert str(mounted[0]) == fake_mp
        assert any("udisksctl" in c and "/dev/sda1" in c for c in commands_executed)

def test_find_candidate_mounts_cross_platform(monkeypatch):
    import sys
    import vibestunes.core.device as dev_mod
    from vibestunes.core.device import find_candidate_mounts

    # Test macOS /Volumes
    with tempfile.TemporaryDirectory() as fake_volumes:
        vol_ipod = Path(fake_volumes) / "IPOD"
        vol_ipod.mkdir()
        monkeypatch.setattr(dev_mod, "VOLUMES_DIR", Path(fake_volumes))

        candidates = find_candidate_mounts()
        assert vol_ipod in candidates

    # Test Windows drive letters
    monkeypatch.setattr(sys, "platform", "win32")
    with tempfile.TemporaryDirectory() as fake_drive:
        fake_drive_path = Path(fake_drive)
        # Mock Path so that 'E:/' returns fake_drive_path
        orig_path = dev_mod.Path
        def mock_path(p):
            if str(p).startswith("E:"):
                return fake_drive_path
            return orig_path(p)

        monkeypatch.setattr(dev_mod, "Path", mock_path)
        candidates = find_candidate_mounts()
        assert fake_drive_path in candidates

def test_eject_ipod_cross_platform(monkeypatch):
    import sys
    from vibestunes.core.device import iPodDevice, eject_ipod

    executed = []
    def mock_run(cmd, capture_output=True, text=True, check=False, timeout=None):
        executed.append(cmd)
        class MockRes:
            returncode = 0
            stdout = ""
            stderr = ""
        return MockRes()

    monkeypatch.setattr("subprocess.run", mock_run)

    # macOS eject
    monkeypatch.setattr(sys, "platform", "darwin")
    dev_mac = iPodDevice(mount_point="/Volumes/IPOD")
    ok, msg = eject_ipod(dev_mac)
    assert ok is True
    assert any("diskutil" in cmd and "eject" in cmd and "/Volumes/IPOD" in cmd for cmd in executed)

    # Windows eject
    executed.clear()
    monkeypatch.setattr(sys, "platform", "win32")
    dev_win = iPodDevice(mount_point="E:\\")
    ok, msg = eject_ipod(dev_win)
    assert ok is True
    assert any("powershell" in cmd and "Eject" in cmd[3] for cmd in executed)

def test_open_folder_cross_platform(monkeypatch):
    import sys
    from vibestunes.core.ipod_scanner import open_folder

    opened_cmds = []
    def mock_popen(cmd):
        opened_cmds.append(cmd)

    monkeypatch.setattr("subprocess.Popen", mock_popen)

    # Test macOS
    monkeypatch.setattr(sys, "platform", "darwin")
    open_folder(Path("/Volumes/IPOD/Music"))
    assert opened_cmds[-1] == ["open", "/Volumes/IPOD/Music"]

    # Test Linux
    monkeypatch.setattr(sys, "platform", "linux")
    open_folder(Path("/media/IPOD/Music"))
    assert opened_cmds[-1] == ["xdg-open", "/media/IPOD/Music"]

    # Test Windows with startfile
    monkeypatch.setattr(sys, "platform", "win32")
    startfile_targets = []
    monkeypatch.setattr("os.startfile", lambda p: startfile_targets.append(p), raising=False)
    open_folder(Path("E:/Music"))
    assert startfile_targets[-1] == "E:/Music"
