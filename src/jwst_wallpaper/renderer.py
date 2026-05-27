"""Render JWST FITS science images into desktop-sized wallpaper PNGs.

Pipeline overview
-----------------
1. Open the FITS file and locate the science (SCI) extension.
2. Apply a data stretch (asinh, log, sqrt …) after sigma/percentile clipping.
3. Map to a matplotlib colormap → 8-bit RGB.
4. Resize to the target wallpaper resolution, preserving aspect ratio and
   padding with black.
5. Optionally composite up to three single-filter images into an RGB image
   (red/green/blue channels assigned per filter name).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from astropy.io import fits
from astropy.visualization import (
    AsinhStretch,
    HistEqStretch,
    LinearStretch,
    LogStretch,
    SqrtStretch,
    ZScaleInterval,
    ManualInterval,
    PercentileInterval,
)
from PIL import Image

from .config import Config

# ---------------------------------------------------------------------------
# Stretch factory
# ---------------------------------------------------------------------------

_STRETCHES = {
    "asinh": AsinhStretch(),
    "log": LogStretch(),
    "sqrt": SqrtStretch(),
    "linear": LinearStretch(),
    "histeq": HistEqStretch,   # needs data, instantiated lazily
}


def _make_normalizer(data: np.ndarray, cfg: Config):
    """Return an astropy ImageNormalize-compatible callable for *data*."""
    from astropy.visualization import ImageNormalize

    interval = PercentileInterval(cfg.percentile_hi) if cfg.percentile_lo == 0 else \
        ManualInterval(*np.nanpercentile(data[np.isfinite(data)],
                                         [cfg.percentile_lo, cfg.percentile_hi]))

    stretch_key = cfg.stretch
    if stretch_key == "histeq":
        finite = data[np.isfinite(data)]
        stretch = HistEqStretch(finite)
    else:
        stretch = _STRETCHES[stretch_key]

    return ImageNormalize(data, interval=interval, stretch=stretch, clip=True)


# ---------------------------------------------------------------------------
# Single-band render
# ---------------------------------------------------------------------------

def fits_to_array(path: Path) -> tuple[np.ndarray, fits.Header]:
    """Open a FITS file and return the (2-D) science array and its header.

    Handles both simple FITS (single array) and MEF (multi-extension FITS)
    with a 'SCI' extension.
    """
    with fits.open(path, memmap=True) as hdul:
        # Try the named SCI extension first (JWST pipeline standard)
        if "SCI" in hdul:
            data = hdul["SCI"].data
            header = hdul["SCI"].header
        elif len(hdul) > 1 and hdul[1].data is not None:
            data = hdul[1].data
            header = hdul[1].header
        else:
            data = hdul[0].data
            header = hdul[0].header

    if data is None:
        raise ValueError(f"No image data found in {path}")

    # Collapse 3-D cubes (e.g. IFU slices) to 2-D by taking the median slice
    if data.ndim == 3:
        data = np.nanmedian(data, axis=0)
    elif data.ndim != 2:
        raise ValueError(f"Unexpected data shape {data.shape} in {path}")

    return data.astype(np.float64), header


def render_single(
    fits_path: Path,
    cfg: Config,
    output_path: Optional[Path] = None,
) -> Path:
    """Render a single FITS file to a wallpaper PNG.

    Parameters
    ----------
    fits_path:
        Path to a calibrated FITS image.
    cfg:
        Rendering configuration.
    output_path:
        Destination PNG path.  If *None*, derived from *fits_path* stem.

    Returns
    -------
    Path
        Path to the written PNG file.
    """
    import matplotlib.cm as cm

    if output_path is None:
        from .config import wallpaper_dir
        output_path = wallpaper_dir() / (fits_path.stem + ".png")

    data, _ = fits_to_array(fits_path)

    # Replace NaN/Inf with 0
    data = np.where(np.isfinite(data), data, 0.0)

    # Normalise to [0, 1]
    norm = _make_normalizer(data, cfg)
    normed: np.ndarray = norm(data)  # type: ignore[assignment]
    normed = np.clip(normed, 0.0, 1.0)

    # Apply colormap → RGBA uint8
    cmap = cm.get_cmap(cfg.colormap)
    rgba = (cmap(normed) * 255).astype(np.uint8)
    img = Image.fromarray(rgba, mode="RGBA").convert("RGB")

    img = _fit_to_canvas(img, cfg.width, cfg.height)
    img.save(str(output_path), format="PNG", optimize=False)
    return output_path


# ---------------------------------------------------------------------------
# RGB composite
# ---------------------------------------------------------------------------

def render_rgb(
    red_path: Path,
    green_path: Path,
    blue_path: Path,
    cfg: Config,
    output_path: Optional[Path] = None,
) -> Path:
    """Composite three single-filter FITS images into a false-colour RGB wallpaper.

    Each channel is independently stretched before compositing.

    Parameters
    ----------
    red_path, green_path, blue_path:
        FITS files assigned to R, G, B channels respectively.
    cfg:
        Rendering configuration (stretch/percentile applied per channel).
    output_path:
        Destination PNG path.

    Returns
    -------
    Path
        Path to the written PNG file.
    """
    if output_path is None:
        from .config import wallpaper_dir
        stem = red_path.stem.replace(cfg.filter_red, "RGB")
        output_path = wallpaper_dir() / (stem + ".png")

    channels: list[np.ndarray] = []
    for path in (red_path, green_path, blue_path):
        data, _ = fits_to_array(path)
        data = np.where(np.isfinite(data), data, 0.0)
        norm = _make_normalizer(data, cfg)
        normed = np.clip(norm(data), 0.0, 1.0)
        channels.append((normed * 255).astype(np.uint8))

    # Match spatial dimensions (crop to smallest)
    min_h = min(c.shape[0] for c in channels)
    min_w = min(c.shape[1] for c in channels)
    channels = [c[:min_h, :min_w] for c in channels]

    rgb = np.stack(channels, axis=-1)
    img = Image.fromarray(rgb, mode="RGB")
    img = _fit_to_canvas(img, cfg.width, cfg.height)
    img.save(str(output_path), format="PNG", optimize=False)
    return output_path


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def _fit_to_canvas(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale *img* to fit within (*width*, *height*), padding with black."""
    img_ratio = img.width / img.height
    target_ratio = width / height

    if img_ratio > target_ratio:
        # Wider than canvas — fit width
        new_w = width
        new_h = round(width / img_ratio)
    else:
        # Taller — fit height
        new_h = height
        new_w = round(height * img_ratio)

    resized = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    x_off = (width - new_w) // 2
    y_off = (height - new_h) // 2
    canvas.paste(resized, (x_off, y_off))
    return canvas
