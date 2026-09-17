"""Scanner and file manager for music on the iPod."""
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Any

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".aac", ".alac", ".ogg", ".opus", ".wav", ".wma", ".aiff"}

from vibestunes.core.naming import clean_fat32_name

@dataclass
class iPodTrack:
    filename: str
    path: Path
    size_bytes: int
    title: str = ""
    track_number: int = 0

@dataclass
class iPodPlaylist:
    name: str
    filename: str
    path: Path
    track_count: int = 0

@dataclass
class iPodAlbum:
    title: str
    artist_name: str
    path: Path
    year: Optional[int] = None
    tracks: List[iPodTrack] = field(default_factory=list)
    total_size_bytes: int = 0
    cover_art: Optional[Path] = None
    sample_track_path: Optional[Path] = None

    @property
    def track_count(self) -> int:
        return len(self.tracks)

@dataclass
class iPodArtist:
    name: str
    path: Path
    albums: List[iPodAlbum] = field(default_factory=list)
    total_size_bytes: int = 0

    @property
    def album_count(self) -> int:
        return len(self.albums)

    @property
    def track_count(self) -> int:
        return sum(a.track_count for a in self.albums)

def is_plex_track_on_ipod(track: Any, ipod_tracks: List[iPodTrack]) -> bool:
    """
    Checks whether a Plex track matches any track in the list of iPod tracks.
    Matches using normalized title, filename stem, and track number.
    """
    if not ipod_tracks:
        return False

    raw_title = getattr(track, "title", str(track))
    norm_plex_title = re.sub(r"[^\w]", "", raw_title).casefold()
    plex_track_num = getattr(track, "track_number", 0) or 0

    for it in ipod_tracks:
        norm_it_title = re.sub(r"[^\w]", "", it.title).casefold()
        norm_it_stem = re.sub(r"[^\w]", "", Path(it.filename).stem).casefold()
        stem_without_num = re.sub(r"^\d+", "", norm_it_stem)

        # 1. Exact normalized title match
        if norm_plex_title and (norm_plex_title == norm_it_title or norm_plex_title == stem_without_num or norm_plex_title == norm_it_stem):
            return True

        # 2. Number match + partial title match
        if plex_track_num > 0 and it.track_number == plex_track_num:
            if len(norm_plex_title) >= 3 and (norm_plex_title in norm_it_stem or norm_it_title in norm_plex_title):
                return True
            if not norm_plex_title or not norm_it_title:
                return True

        # 3. Substring match for non-trivial title length (handles Live, Remastered, etc. in either direction)
        if len(norm_plex_title) >= 4 and (norm_plex_title in stem_without_num or norm_plex_title in norm_it_title):
            return True
        if len(stem_without_num) >= 4 and stem_without_num in norm_plex_title:
            return True
        if len(norm_it_title) >= 4 and norm_it_title in norm_plex_title:
            return True

    return False

def parse_album_folder_name(folder_name: str, artist_name: str) -> tuple[str, Optional[int]]:
    """
    Parses 'Artist-Year-Album' or 'Artist-Album' or 'Album (Year)' or plain 'Album'.
    Returns (clean_album_title, year).
    """
    name = folder_name.strip()
    year: Optional[int] = None

    # 1. Strip leading artist prefix if present
    clean_art = clean_fat32_name(artist_name)
    art_prefix_pattern = rf"^(?:{re.escape(artist_name)}|{re.escape(clean_art)})\s*[-_]\s*"
    name_stripped = re.sub(art_prefix_pattern, "", name, flags=re.IGNORECASE)
    if name_stripped != name and name_stripped.strip():
        name = name_stripped.strip()

    # 2. Check if name starts with YYYY-
    m_year_prefix = re.match(r"^(\d{4})\s*[-_]\s*(.+)$", name)
    if m_year_prefix:
        try:
            y = int(m_year_prefix.group(1))
            if 1900 <= y <= 2099:
                year = y
                name = m_year_prefix.group(2).strip()
        except ValueError:
            pass

    # 3. Check if name ends with (YYYY) or [YYYY]
    m_year_paren = re.search(r"[\(\[](\d{4})[\)\]]", name)
    if m_year_paren:
        try:
            y = int(m_year_paren.group(1))
            if 1900 <= y <= 2099:
                year = y
                name = re.sub(r"\s*[\(\[]\d{4}[\)\]]", "", name).strip()
        except ValueError:
            pass

    # 4. Fallback for Artist-YYYY-Album if artist wasn't at start
    if year is None:
        m_fallback = re.match(r"^.*?[-_](\d{4})[-_](.+)$", name)
        if m_fallback:
            try:
                y = int(m_fallback.group(1))
                if 1900 <= y <= 2099:
                    year = y
                    name = m_fallback.group(2).strip()
            except ValueError:
                pass

    # 5. Strip disc/CD suffixes e.g. " (Disc 1)", " [CD 2]", " - Disc 01", " CD 1"
    name = re.sub(r"\s*(?:[\(\[-]\s*)?(?:cd|disc)\s*\d+[\)\]]?\s*$", "", name, flags=re.IGNORECASE).strip()

    # 6. Strip trailing (Artist) or [Artist] suffix if present (from Plex folder conventions)
    art_suffix_pattern = rf"\s*[\(\[](?:{re.escape(artist_name)}|{re.escape(clean_art)})[\)\]]\s*$"
    name_stripped_suffix = re.sub(art_suffix_pattern, "", name, flags=re.IGNORECASE)
    if name_stripped_suffix.strip():
        name = name_stripped_suffix.strip()

    return name if name else folder_name, year

def scan_album_directory(album_dir: Path, artist_name: str) -> iPodAlbum:
    album_title, year = parse_album_folder_name(album_dir.name, artist_name)
    album = iPodAlbum(
        title=album_title,
        artist_name=artist_name,
        path=album_dir,
        year=year,
    )

    total_size = 0
    tracks = []
    cover_art = None
    sample_track_path = None

    # Recurse inside album folder (in case of CD 01, CD 02 subfolders)
    for root, dirs, files in os.walk(album_dir):
        root_path = Path(root)
        for f in files:
            f_lower = f.lower()
            f_path = root_path / f
            try:
                size = f_path.stat().st_size
            except OSError:
                continue

            total_size += size
            ext = f_path.suffix.lower()

            if ext in AUDIO_EXTENSIONS:
                if sample_track_path is None:
                    sample_track_path = f_path
                # Parse track number if leading: e.g. "01 - Airbag.flac"
                t_match = re.match(r"^(\d+)\s*[-._]?\s*(.+?)(?:\.[^.]*)?$", f)
                t_num = int(t_match.group(1)) if t_match else 0
                t_title = t_match.group(2).strip() if t_match else f_path.stem
                tracks.append(iPodTrack(
                    filename=f,
                    path=f_path,
                    size_bytes=size,
                    title=t_title,
                    track_number=t_num,
                ))
            elif f_lower in ("cover.jpg", "cover.png", "folder.jpg", "front.jpg"):
                if cover_art is None:
                    cover_art = f_path

    tracks.sort(key=lambda t: (t.track_number, t.filename))
    album.tracks = tracks
    album.total_size_bytes = total_size
    album.cover_art = cover_art
    album.sample_track_path = sample_track_path
    return album

def scan_ipod_music(mount_point: str, progress_callback: Optional[Callable[[int, str], None]] = None) -> List[iPodArtist]:
    mp = Path(mount_point)
    if not mp.is_dir():
        return []

    # Filter out system and special directories
    ignored_names = {".rockbox", ".trash-1000", "playlists", "lost+found", "system volume information", "ipod_control"}
    artists: List[iPodArtist] = []

    try:
        entries = [
            e for e in mp.iterdir()
            if e.is_dir() and e.name.lower() not in ignored_names and not e.name.startswith(".")
        ]
        entries.sort(key=lambda x: x.name.lower())
        total = len(entries)

        for i, artist_dir in enumerate(entries):
            if progress_callback:
                progress_callback(int((i + 1) / max(1, total) * 100), artist_dir.name)

            artist = iPodArtist(name=artist_dir.name, path=artist_dir)
            album_dirs = [d for d in artist_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
            album_dirs.sort(key=lambda x: x.name.lower())

            # Helper to add or merge an album under artist
            def add_or_merge_album(alb: iPodAlbum):
                if alb.track_count <= 0:
                    return
                from vibestunes.core.plex_client import normalize_music_key
                alb_k = normalize_music_key(artist.name, alb.title)
                existing = next(
                    (a for a in artist.albums if normalize_music_key(artist.name, a.title) == alb_k),
                    None
                )
                if existing:
                    existing.tracks.extend(alb.tracks)
                    existing.tracks.sort(key=lambda t: (t.track_number, t.filename))
                    existing.total_size_bytes += alb.total_size_bytes
                    if not existing.cover_art and alb.cover_art:
                        existing.cover_art = alb.cover_art
                else:
                    artist.albums.append(alb)

            # Check if tracks are placed directly in artist dir or in album subdirs
            has_subdirs = len(album_dirs) > 0
            if has_subdirs:
                for a_dir in album_dirs:
                    album = scan_album_directory(a_dir, artist.name)
                    add_or_merge_album(album)
            else:
                # Direct album / single folder
                album = scan_album_directory(artist_dir, artist.name)
                add_or_merge_album(album)

            artist.total_size_bytes = sum(a.total_size_bytes for a in artist.albums)
            if artist.track_count > 0:
                artists.append(artist)

    except Exception:
        pass

    return artists

def delete_album(album: iPodAlbum) -> tuple[bool, int, str]:
    """Permanently deletes an album directory and cleans up empty parent if applicable."""
    if not album.path.exists():
        return False, 0, "Album folder does not exist"
    try:
        size_freed = album.total_size_bytes
        shutil.rmtree(album.path)
        # Check if parent artist folder is now empty
        parent = album.path.parent
        if parent.is_dir():
            remaining = [f for f in parent.iterdir() if not f.name.startswith(".")]
            if not remaining:
                try:
                    parent.rmdir()
                except OSError:
                    pass
        return True, size_freed, f"Deleted '{album.title}' ({size_freed / (1024*1024):.1f} MB freed)"
    except Exception as e:
        return False, 0, f"Failed to delete album: {e}"

def delete_artist(artist: iPodArtist) -> tuple[bool, int, str]:
    """Permanently deletes an entire artist directory."""
    if not artist.path.exists():
        return False, 0, "Artist folder does not exist"
    try:
        size_freed = artist.total_size_bytes
        shutil.rmtree(artist.path)
        return True, size_freed, f"Deleted artist '{artist.name}' ({size_freed / (1024*1024):.1f} MB freed)"
    except Exception as e:
        return False, 0, f"Failed to delete artist: {e}"

def find_and_delete_ipod_album(mount_point: str, artist_name: str, album_title: str) -> tuple[bool, int, str]:
    """
    Finds and deletes an album folder matching artist_name and album_title on the iPod.
    Returns (success, freed_bytes, message).
    """
    from vibestunes.core.plex_client import normalize_music_key, is_album_match

    mp = Path(mount_point)
    if not mp.is_dir():
        return False, 0, f"iPod mount point {mount_point} not accessible"

    target_artist_norm = normalize_music_key(artist_name)

    # Search for matching artist directory first
    for art_dir in mp.iterdir():
        if not art_dir.is_dir() or art_dir.name.startswith("."):
            continue
        if normalize_music_key(art_dir.name) != target_artist_norm:
            continue

        # Found artist directory; search for matching album directory
        for alb_dir in art_dir.iterdir():
            if not alb_dir.is_dir():
                continue
            alb_clean_title, _ = parse_album_folder_name(alb_dir.name, art_dir.name)
            is_match = (
                is_album_match(alb_clean_title, album_title) or
                is_album_match(alb_dir.name, album_title)
            )
            if is_match:

                try:
                    freed = sum(f.stat().st_size for f in alb_dir.rglob("*") if f.is_file())
                    shutil.rmtree(alb_dir)
                    # Clean up parent if empty
                    remaining = [f for f in art_dir.iterdir() if not f.name.startswith(".")]
                    if not remaining:
                        try:
                            art_dir.rmdir()
                        except OSError:
                            pass
                    return True, freed, f"Removed '{album_title}' from iPod ({freed / (1024*1024):.1f} MB freed)"
                except Exception as e:
                    return False, 0, f"Failed to delete album: {e}"

    return False, 0, f"Album '{album_title}' not found on iPod"

def find_and_delete_ipod_artist(mount_point: str, artist_name: str) -> tuple[bool, int, str]:
    """
    Finds and permanently deletes all music for an artist on the iPod.
    Returns (success, freed_bytes, message).
    """
    from vibestunes.core.plex_client import normalize_music_key

    mp = Path(mount_point)
    if not mp.is_dir():
        return False, 0, f"iPod mount point {mount_point} not accessible"

    target_artist_norm = normalize_music_key(artist_name)
    for art_dir in mp.iterdir():
        if not art_dir.is_dir() or art_dir.name.startswith("."):
            continue
        if normalize_music_key(art_dir.name) == target_artist_norm:
            try:
                freed = sum(f.stat().st_size for f in art_dir.rglob("*") if f.is_file())
                shutil.rmtree(art_dir)
                return True, freed, f"Removed artist '{artist_name}' from iPod ({freed / (1024*1024):.1f} MB freed)"
            except Exception as e:
                return False, 0, f"Failed to delete artist: {e}"

    return False, 0, f"Artist '{artist_name}' not found on iPod"

def clean_trash(mount_point: str) -> tuple[bool, int, str]:
    """Empties .Trash-1000 directory on the iPod."""
    trash_dir = Path(mount_point) / ".Trash-1000"
    if not trash_dir.exists():
        return True, 0, "Trash is already empty"
    try:
        total_freed = sum(f.stat().st_size for f in trash_dir.rglob("*") if f.is_file())
        shutil.rmtree(trash_dir)
        return True, total_freed, f"Cleaned trash ({total_freed / (1024*1024):.1f} MB freed)"
    except Exception as e:
        return False, 0, f"Failed to clean trash: {e}"

def open_folder(path: Path) -> None:
    """Opens folder in default system file manager (macOS Finder, Windows Explorer, Linux file manager)."""
    try:
        target = str(path)
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
        elif sys.platform == "win32":
            if hasattr(os, "startfile"):
                os.startfile(target)
            else:
                subprocess.Popen(["explorer", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception:
        pass

def scan_ipod_playlists(mount_point: str) -> List[iPodPlaylist]:
    mp = Path(mount_point)
    pl_dir = mp / "Playlists"
    if not pl_dir.is_dir():
        pl_dir = mp / "playlists"
    if not pl_dir.is_dir():
        return []

    playlists = []
    try:
        for f in pl_dir.iterdir():
            if f.is_file() and f.suffix.lower() in (".m3u", ".m3u8"):
                count = 0
                try:
                    with open(f, "r", encoding="utf-8-sig", errors="ignore") as pf:
                        for line in pf:
                            line_s = line.strip()
                            if line_s and not line_s.startswith("#"):
                                count += 1
                except Exception:
                    pass
                playlists.append(iPodPlaylist(
                    name=f.stem,
                    filename=f.name,
                    path=f,
                    track_count=count,
                ))
        playlists.sort(key=lambda x: x.name.lower())
    except Exception:
        pass
    return playlists

def delete_playlist(playlist: iPodPlaylist) -> tuple[bool, str]:
    if not playlist.path.exists():
        return False, "Playlist file does not exist"
    try:
        playlist.path.unlink()
        return True, f"Deleted playlist '{playlist.name}'"
    except Exception as e:
        return False, f"Failed to delete playlist: {e}"

def build_ipod_track_index(artists: List[iPodArtist]) -> dict:
    """
    Builds a fast lookup index mapping (normalized_artist::normalized_title) -> Path.
    """
    index = {}
    for artist in artists:
        a_norm = re.sub(r"[^a-zA-Z0-9]", "", artist.name).lower()
        for album in artist.albums:
            for track in album.tracks:
                t_norm = re.sub(r"[^a-zA-Z0-9]", "", track.title).lower()
                key = f"{a_norm}::{t_norm}"
                index[key] = track.path
    return index

