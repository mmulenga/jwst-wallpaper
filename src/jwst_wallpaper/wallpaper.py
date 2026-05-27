"""Cross-platform desktop wallpaper setter.

Supports:
  - Linux  : GNOME (gsettings), KDE Plasma (plasma-apply-wallpaperimage),
             Sway/wlroots (swaybg), i3/X11 (feh / xwallpaper)
  - macOS  : AppleScript via osascript
  - Windows: ctypes SystemParametersInfo
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


class WallpaperError(RuntimeError):
    """Raised when the wallpaper could not be set."""


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

def set_wallpaper(path: Path) -> None:
    """Set the desktop wallpaper to *path* on the current platform.

    Parameters
    ----------
    path:
        Absolute path to a PNG or JPEG file.

    Raises
    ------
    WallpaperError
        If no supported setter is found or the command fails.
    """
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Wallpaper file not found: {path}")

    system = platform.system()
    if system == "Linux":
        _set_linux(path)
    elif system == "Darwin":
        _set_macos(path)
    elif system == "Windows":
        _set_windows(path)
    else:
        raise WallpaperError(f"Unsupported platform: {system}")


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _set_linux(path: Path) -> None:
    """Detect the desktop environment and use the appropriate setter."""
    de = _detect_linux_de()

    if de == "gnome":
        _set_gnome(path)
    elif de == "kde":
        _set_kde(path)
    elif de == "sway":
        _set_sway(path)
    else:
        # Generic X11 fallback
        _set_x11_fallback(path)


def _detect_linux_de() -> str:
    """Return a lower-case DE identifier: 'gnome', 'kde', 'sway', or 'unknown'."""
    xdg = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gnome" in xdg or "unity" in xdg or "budgie" in xdg:
        return "gnome"
    if "kde" in xdg or "plasma" in xdg:
        return "kde"

    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    swaysock = os.environ.get("SWAYSOCK", "")
    if swaysock or "sway" in wayland_display:
        return "sway"

    # Check running processes as a last resort
    try:
        procs = subprocess.check_output(["ps", "-e", "-o", "comm="],
                                         text=True, timeout=3)
        if "gnome-shell" in procs:
            return "gnome"
        if "plasmashell" in procs:
            return "kde"
        if "sway" in procs:
            return "sway"
    except Exception:
        pass

    return "unknown"


def _set_gnome(path: Path) -> None:
    uri = f"file://{path}"
    _run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri])
    # Also set the dark variant (GNOME 42+)
    try:
        _run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri])
    except WallpaperError:
        pass


def _set_kde(path: Path) -> None:
    if shutil.which("plasma-apply-wallpaperimage"):
        _run(["plasma-apply-wallpaperimage", str(path)])
        return
    # Fallback: D-Bus via qdbus
    script = (
        "var allDesktops = desktops();"
        "for (var i = 0; i < allDesktops.length; i++) {"
        "  d = allDesktops[i];"
        "  d.wallpaperPlugin = 'org.kde.image';"
        f"  d.currentConfigGroup = ['Wallpaper','org.kde.image','General'];"
        f"  d.writeConfig('Image','file://{path}');"
        "}"
    )
    _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
          "org.kde.PlasmaShell.evaluateScript", script])


def _set_sway(path: Path) -> None:
    if shutil.which("swaybg"):
        # swaybg runs as a daemon; kill old instance first
        subprocess.run(["pkill", "-x", "swaybg"], capture_output=True)
        subprocess.Popen(["swaybg", "-i", str(path), "-m", "fill"],
                         start_new_session=True)
        return
    raise WallpaperError(
        "swaybg not found. Install it with your package manager (e.g. apt install swaybg)."
    )


def _set_x11_fallback(path: Path) -> None:
    """Try feh, then xwallpaper, then xsetroot."""
    if shutil.which("feh"):
        _run(["feh", "--bg-fill", str(path)])
        return
    if shutil.which("xwallpaper"):
        _run(["xwallpaper", "--zoom", str(path)])
        return
    if shutil.which("xsetroot"):
        # xsetroot only supports simple colours/bitmaps, but try anyway
        _run(["xsetroot", "-bitmap", str(path)])
        return
    raise WallpaperError(
        "No supported wallpaper setter found on Linux X11.\n"
        "Install one of: feh, xwallpaper, or swaybg."
    )


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _set_macos(path: Path) -> None:
    script = (
        f'tell application "Finder" to set desktop picture '
        f'to POSIX file "{path}"'
    )
    _run(["osascript", "-e", script])


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _set_windows(path: Path) -> None:
    import ctypes

    SPI_SETDESKWALLPAPER = 0x0014
    SPIF_UPDATEINIFILE = 0x01
    SPIF_SENDCHANGE = 0x02

    ret = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
        SPI_SETDESKWALLPAPER,
        0,
        str(path),
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
    )
    if not ret:
        raise WallpaperError("SystemParametersInfoW failed — could not set wallpaper.")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise WallpaperError(f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise WallpaperError(f"Command timed out: {' '.join(cmd)}")
    if result.returncode != 0:
        raise WallpaperError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
