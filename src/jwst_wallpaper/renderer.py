"""Render JWST FITS science images into desktop-sized wallpaper PNGs.

Pipeline overview
-----------------
Single-band
  1. Open the FITS file and locate the science (SCI) extension.
  2. Apply a data stretch (asinh, log, sqrt …) after percentile clipping.
  3. Map to a matplotlib colormap → 8-bit RGB.
  4. Resize to the target wallpaper resolution, preserving aspect ratio and
     padding with black.

Full-colour RGB (Lupton)
  1. Load three single-filter FITS files (red, green, blue channel).
  2. Per-channel: subtract background, clip negatives.
  3. Feed all three to ``astropy.visualization.make_lupton_rgb``, which applies
     a shared asinh stretch that preserves colour relationships across the full
     dynamic range — the same algorithm used for JWST/HST press-release images.
  4. Resize and save.
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
    ManualInterval,
    PercentileInterval,
    SqrtStretch,
    make_lupton_rgb,
)
from PIL import Image

from .config import Config

# ---------------------------------------------------------------------------
# Stretch factory (used for single-band renders)
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
# FITS I/O
# ---------------------------------------------------------------------------

def fits_to_array(path: Path) -> tuple[np.ndarray, fits.Header]:
    """Open a FITS file and return the (2-D) science array and its header.

    Handles both simple FITS (single array) and MEF (multi-extension FITS)
    with a ``SCI`` extension.
    """
    with fits.open(path, memmap=True) as hdul:
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


# ---------------------------------------------------------------------------
# Single-band render
# ---------------------------------------------------------------------------

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
    data = np.where(np.isfinite(data), data, 0.0)

    norm = _make_normalizer(data, cfg)
    normed: np.ndarray = norm(data)  # type: ignore[assignment]
    normed = np.clip(normed, 0.0, 1.0)

    cmap = cm.get_cmap(cfg.colormap)
    rgba = (cmap(normed) * 255).astype(np.uint8)
    img = Image.fromarray(rgba, mode="RGBA").convert("RGB")

    img = _fit_to_canvas(img, cfg.width, cfg.height)
    img.save(str(output_path), format="PNG", optimize=False)
    return output_path


# ---------------------------------------------------------------------------
# Full-colour RGB composite (Lupton algorithm)
# ---------------------------------------------------------------------------

def _prep_channel(data: np.ndarray, percentile_lo: float, percentile_hi: float) -> np.ndarray:
    """Prepare a single channel array for Lupton compositing.

    Steps
    -----
    1. Replace NaN / Inf with 0.
    2. Subtract the sky background (estimated as the ``percentile_lo``
       percentile of finite pixels) so faint features sit on a true zero.
    3. Clip to ≥ 0 — Lupton stretch requires non-negative input.
    4. Scale to a common reference so channels with very different flux
       calibrations (e.g. MIRI vs NIRCam) don't dominate the composite.
    """
    data = np.where(np.isfinite(data), data, 0.0)

    finite = data[data != 0]
    if finite.size:
        sky = np.percentile(finite, max(percentile_lo, 1.0))
        data = data - sky
    np.clip(data, 0.0, None, out=data)

    # Scale each channel so the 99th percentile maps to 1.0 — this prevents
    # a single very bright channel from washing out the others.
    p99 = np.percentile(data[data > 0], 99.0) if data.max() > 0 else 1.0
    if p99 > 0:
        data /= p99

    return data


def render_lupton_rgb(
    red_path: Path,
    green_path: Path,
    blue_path: Path,
    cfg: Config,
    output_path: Optional[Path] = None,
    Q: float = 10.0,
    stretch: float = 0.5,
) -> Path:
    """Composite three FITS files into a true-colour wallpaper using Lupton RGB.

    The Lupton et al. (2004) algorithm applies a shared asinh stretch across
    all three channels.  Because the stretch is luminance-based rather than
    per-channel, colours are preserved across the full dynamic range — bright
    stars stay white, faint nebulae show their hue, and nothing gets clipped
    to a single channel.

    Parameters
    ----------
    red_path, green_path, blue_path:
        FITS files assigned to the R, G, B channels.  Typically long- to
        short-wavelength filters (e.g. F444W → red, F277W → green,
        F090W → blue for NIRCam).
    cfg:
        Rendering configuration.  ``percentile_lo`` / ``percentile_hi`` are
        used for background estimation and channel scaling.
    output_path:
        Destination PNG path.  Defaults to ``<red_stem>_RGB.png`` in the
        wallpaper cache directory.
    Q:
        Lupton softening parameter.  Higher → more aggressive compression of
        bright peaks; lower → more linear.  Default 10 works well for JWST.
    stretch:
        Lupton stretch scale.  Smaller values bring out faint detail at the
        cost of saturating bright regions.  Default 0.5.

    Returns
    -------
    Path
        Path to the written PNG file.
    """
    if output_path is None:
        from .config import wallpaper_dir
        stem = red_path.stem.split("_")[0] + "_RGB"
        output_path = wallpaper_dir() / (stem + ".png")

    # Load all three channels
    r_raw, _ = fits_to_array(red_path)
    g_raw, _ = fits_to_array(green_path)
    b_raw, _ = fits_to_array(blue_path)

    # Align spatial dimensions — crop to the smallest common footprint
    min_h = min(r_raw.shape[0], g_raw.shape[0], b_raw.shape[0])
    min_w = min(r_raw.shape[1], g_raw.shape[1], b_raw.shape[1])
    r_raw = r_raw[:min_h, :min_w]
    g_raw = g_raw[:min_h, :min_w]
    b_raw = b_raw[:min_h, :min_w]

    # Per-channel preparation
    r = _prep_channel(r_raw, cfg.percentile_lo, cfg.percentile_hi)
    g = _prep_channel(g_raw, cfg.percentile_lo, cfg.percentile_hi)
    b = _prep_channel(b_raw, cfg.percentile_lo, cfg.percentile_hi)

    # Lupton composite → uint8 RGB array (H × W × 3).
    # Suppress the expected divide-by-zero warning from zero-intensity edge pixels.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rgb_array = make_lupton_rgb(r, g, b, Q=Q, stretch=stretch)

    img = Image.fromarray(rgb_array, mode="RGB")
    img = _fit_to_canvas(img, cfg.width, cfg.height)
    img.save(str(output_path), format="PNG", optimize=False)
    return output_path


def render_rgb(
    red_path: Path,
    green_path: Path,
    blue_path: Path,
    cfg: Config,
    output_path: Optional[Path] = None,
) -> Path:
    """Public alias — delegates to :func:`render_lupton_rgb`.

    Kept for backwards compatibility with callers that use the old name.
    """
    return render_lupton_rgb(red_path, green_path, blue_path, cfg, output_path)


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def _fit_to_canvas(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale *img* to fit within (*width*, *height*), padding with black."""
    img_ratio = img.width / img.height
    target_ratio = width / height

    if img_ratio > target_ratio:
        new_w = width
        new_h = round(width / img_ratio)
    else:
        new_h = height
        new_w = round(height * img_ratio)

    resized = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    x_off = (width - new_w) // 2
    y_off = (height - new_h) // 2
    canvas.paste(resized, (x_off, y_off))
    return canvas
