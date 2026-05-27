"""Tests for the cache module."""

from __future__ import annotations

from pathlib import Path

import pytest

from jwst_wallpaper import cache
from jwst_wallpaper.cache import CacheEntry, entry_from_fits


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect all cache paths to a temporary directory."""
    monkeypatch.setattr("jwst_wallpaper.cache.cache_dir", lambda: tmp_path)
    monkeypatch.setattr("jwst_wallpaper.cache.fits_dir", lambda: tmp_path / "fits")
    monkeypatch.setattr("jwst_wallpaper.cache.wallpaper_dir", lambda: tmp_path / "wallpapers")
    monkeypatch.setattr("jwst_wallpaper.cache._index_path", lambda: tmp_path / "index.json")
    (tmp_path / "fits").mkdir()
    (tmp_path / "wallpapers").mkdir()


class TestCacheRoundtrip:
    def test_add_and_load(self, tmp_path: Path) -> None:
        fits_file = tmp_path / "fits" / "test.fits"
        fits_file.write_bytes(b"")
        entry = entry_from_fits(fits_file, target="Carina Nebula", instrument="NIRCam")
        cache.add_entry(entry)
        loaded = cache.get_all()
        assert len(loaded) == 1
        assert loaded[0].target == "Carina Nebula"

    def test_update_existing(self, tmp_path: Path) -> None:
        fits_file = tmp_path / "fits" / "test.fits"
        fits_file.write_bytes(b"")
        entry = entry_from_fits(fits_file, obs_id="OBS001", target="Alpha")
        cache.add_entry(entry)
        entry2 = entry_from_fits(fits_file, obs_id="OBS001", target="Beta")
        cache.add_entry(entry2)
        all_entries = cache.get_all()
        assert len(all_entries) == 1
        assert all_entries[0].target == "Beta"

    def test_mark_rendered(self, tmp_path: Path) -> None:
        fits_file = tmp_path / "fits" / "test.fits"
        fits_file.write_bytes(b"")
        wp = tmp_path / "wallpapers" / "test.png"
        wp.write_bytes(b"")
        entry = entry_from_fits(fits_file, obs_id="OBS002")
        cache.add_entry(entry)
        cache.mark_rendered("OBS002", "", "test.png")
        rendered = cache.get_rendered()
        assert len(rendered) == 1
        assert rendered[0].wallpaper_path == "test.png"
