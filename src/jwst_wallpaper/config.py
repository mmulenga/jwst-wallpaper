"""User configuration — cache paths, rendering defaults, and preferences."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_dir, user_config_dir

APP_NAME = "jwst-wallpaper"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def cache_dir() -> Path:
    """Return the platform-appropriate cache directory, creating it if needed."""
    path = Path(user_cache_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def fits_dir() -> Path:
    """Sub-directory for raw FITS downloads."""
    path = cache_dir() / "fits"
    path.mkdir(parents=True, exist_ok=True)
    return path


def wallpaper_dir() -> Path:
    """Sub-directory for rendered wallpaper PNGs."""
    path = cache_dir() / "wallpapers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    """Path to the user config JSON file."""
    cfg = Path(user_config_dir(APP_NAME))
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg / "config.json"


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

Colormap = Literal[
    "inferno", "magma", "plasma", "viridis",
    "afmhot", "hot", "gist_heat",
    "Greys_r", "bone", "gray",
]

Stretch = Literal["asinh", "log", "sqrt", "linear", "histeq"]


@dataclass
class Config:
    """Persisted user preferences."""

    # Rendering
    colormap: Colormap = "inferno"
    stretch: Stretch = "asinh"
    width: int = 3840          # Output pixel width (downscaled to fit screen automatically)
    height: int = 2160         # Output pixel height
    percentile_lo: float = 1.0  # Lower clip percentile for stretch
    percentile_hi: float = 99.5  # Upper clip percentile for stretch

    # MAST query defaults
    target: str = ""           # Default target name (e.g. "Carina Nebula")
    instrument: str = "NIRCam" # NIRCam | MIRI | NIRSpec | NIRISS
    max_results: int = 10      # Max observations to fetch per query

    # RGB compositing (only used when instrument == "NIRCam" + multi-filter mode)
    rgb_mode: bool = False      # If True, attempt 3-filter RGB composite
    filter_red: str = "F444W"
    filter_green: str = "F277W"
    filter_blue: str = "F090W"

    # Cache
    keep_fits: bool = True      # Keep raw FITS files after rendering
    max_wallpapers: int = 50    # Delete oldest when limit exceeded


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load() -> Config:
    """Load config from disk, falling back to defaults."""
    path = config_file()
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text())
        return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    except Exception:
        return Config()


def save(cfg: Config) -> None:
    """Persist config to disk."""
    config_file().write_text(json.dumps(asdict(cfg), indent=2))
