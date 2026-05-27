"""Local cache management — inventory of downloaded FITS and rendered wallpapers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import cache_dir, fits_dir, wallpaper_dir


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """Metadata for a single downloaded + (optionally) rendered image."""

    obs_id: str
    target: str
    instrument: str
    fits_path: str          # relative to fits_dir()
    wallpaper_path: str     # relative to wallpaper_dir(); empty if not rendered
    fetched_at: str         # ISO-8601 timestamp
    rendered_at: str        # ISO-8601 timestamp; empty if not rendered
    filter_name: str = ""
    exposure_s: float = 0.0

    @property
    def fits_file(self) -> Path:
        return fits_dir() / self.fits_path

    @property
    def wallpaper_file(self) -> Optional[Path]:
        if not self.wallpaper_path:
            return None
        return wallpaper_dir() / self.wallpaper_path

    def is_rendered(self) -> bool:
        return bool(self.wallpaper_path) and (wallpaper_dir() / self.wallpaper_path).exists()


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------

_INDEX_FILE = "index.json"


def _index_path() -> Path:
    return cache_dir() / _INDEX_FILE


def load_index() -> list[CacheEntry]:
    """Load the cache index from disk."""
    path = _index_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        return [CacheEntry(**item) for item in raw]
    except Exception:
        return []


def save_index(entries: list[CacheEntry]) -> None:
    """Persist the cache index to disk."""
    _index_path().write_text(json.dumps([asdict(e) for e in entries], indent=2))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_entry(entry: CacheEntry) -> None:
    """Add or update a cache entry (keyed on obs_id + filter_name)."""
    entries = load_index()
    key = (entry.obs_id, entry.filter_name)
    entries = [e for e in entries if (e.obs_id, e.filter_name) != key]
    entries.append(entry)
    save_index(entries)


def mark_rendered(obs_id: str, filter_name: str, wallpaper_path: str) -> None:
    """Record that a FITS entry has been rendered to a wallpaper."""
    entries = load_index()
    for e in entries:
        if e.obs_id == obs_id and e.filter_name == filter_name:
            e.wallpaper_path = wallpaper_path
            e.rendered_at = datetime.now(timezone.utc).isoformat()
            break
    save_index(entries)


def get_rendered() -> list[CacheEntry]:
    """Return all entries that have been rendered to wallpapers."""
    return [e for e in load_index() if e.is_rendered()]


def get_all() -> list[CacheEntry]:
    """Return all cache entries."""
    return load_index()


def purge_oldest_wallpapers(max_wallpapers: int) -> int:
    """Delete the oldest rendered wallpapers when count exceeds *max_wallpapers*.

    Returns the number of files deleted.
    """
    rendered = sorted(
        [e for e in load_index() if e.is_rendered()],
        key=lambda e: e.rendered_at,
    )
    deleted = 0
    while len(rendered) > max_wallpapers:
        oldest = rendered.pop(0)
        wp = oldest.wallpaper_file
        if wp and wp.exists():
            wp.unlink()
            deleted += 1
        oldest.wallpaper_path = ""
        oldest.rendered_at = ""

    if deleted:
        save_index(load_index())  # Re-save updated entries
    return deleted


def entry_from_fits(
    fits_path: Path,
    obs_id: str = "",
    target: str = "",
    instrument: str = "",
    filter_name: str = "",
    exposure_s: float = 0.0,
) -> CacheEntry:
    """Convenience constructor from a FITS path."""
    return CacheEntry(
        obs_id=obs_id or fits_path.stem,
        target=target,
        instrument=instrument,
        fits_path=fits_path.name,
        wallpaper_path="",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        rendered_at="",
        filter_name=filter_name,
        exposure_s=exposure_s,
    )
