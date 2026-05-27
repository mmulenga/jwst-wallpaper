"""Tests for the cross-platform wallpaper setter."""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jwst_wallpaper.wallpaper import WallpaperError, _detect_linux_de, set_wallpaper


class TestDetectLinuxDe:
    def test_detects_gnome_via_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        assert _detect_linux_de() == "gnome"

    def test_detects_kde_via_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert _detect_linux_de() == "kde"

    def test_detects_sway_via_swaysock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "")
        monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway-ipc.sock")
        assert _detect_linux_de() == "sway"


class TestSetWallpaper:
    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            set_wallpaper(tmp_path / "ghost.png")

    @pytest.mark.skipif(platform.system() != "Linux", reason="Linux only")
    def test_gnome_calls_gsettings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "bg.png"
        img.write_bytes(b"fake")
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
        with patch("jwst_wallpaper.wallpaper._run") as mock_run:
            set_wallpaper(img)
            assert any("gsettings" in str(call) for call in mock_run.call_args_list)
