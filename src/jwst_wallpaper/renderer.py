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

def _subtract_sky(data: np.ndarray, percentile_lo: float) -> np.ndarray:
    """Subtract the sky background from *data* and clip to ≥ 0.

    Steps
    -----
    1. Replace NaN / Inf with 0.
    2. Subtract the sky background (estimated as the ``percentile_lo``
       percentile of finite pixels) so faint features sit on a true zero.
    3. Clip to ≥ 0 — Lupton stretch requires non-negative input.

    Unlike the old ``_prep_channel``, this function does **not** apply any
    per-channel normalisation.  Normalisation is done once, jointly, in
    :func:`render_lupton_rgb` so that relative flux ratios between channels
    are preserved — the key to getting traditional astrophotography colours.
    """
    data = np.where(np.isfinite(data), data, 0.0)
    finite = data[data != 0]
    if finite.size:
        sky = np.percentile(finite, max(percentile_lo, 1.0))
        data = data - sky
    np.clip(data, 0.0, None, out=data)
    return data


def _apply_scurve(arr: np.ndarray, strength: float = 0.4) -> np.ndarray:
    """Apply a photographic S-curve (sigmoid-based) to a [0, 1] float array.

    The S-curve darkens shadows, keeps mid-tones roughly anchored, and lifts
    highlights — adding the kind of contrast punch you see in processed
    astrophotos without clipping the full dynamic range.

    Parameters
    ----------
    arr:
        Input array with values in [0, 1].
    strength:
        How steep the S is.  0 = identity; 1 = very aggressive.  Default 0.4.
    """
    # Logistic-based S-curve centred on 0.5:
    #   f(x) = 1 / (1 + exp(-k * (x - 0.5)))  renormalised so f(0)→0, f(1)→1
    k = 4.0 + strength * 8.0          # range [4, 12]
    sig = 1.0 / (1.0 + np.exp(-k * (arr - 0.5)))
    lo = 1.0 / (1.0 + np.exp(-k * (0.0 - 0.5)))
    hi = 1.0 / (1.0 + np.exp(-k * (1.0 - 0.5)))
    return np.clip((sig - lo) / (hi - lo), 0.0, 1.0)


def _boost_saturation(img: "Image.Image", factor: float) -> "Image.Image":
    """Increase colour saturation of *img* by *factor* (1.0 = unchanged)."""
    from PIL import ImageEnhance
    return ImageEnhance.Color(img).enhance(factor)


def _color_matrix(img: "Image.Image", matrix: np.ndarray) -> "Image.Image":
    """Apply a 3×3 linear colour-mixing matrix to *img*.

    The matrix maps input (R, G, B) → output (R', G', B') via a dot product.
    Each row of *matrix* gives the mix coefficients for one output channel.
    Row sums should be ≈ 1.0 to preserve overall luminosity.

    Example — shift red→orange, green→teal, keep blue:
        np.array([
            [0.80, 0.20, 0.00],   # R' mostly from R, some G
            [0.45, 0.45, 0.10],   # G' half from R, half from G → amber mid-tones
            [0.00, 0.15, 0.85],   # B' mostly blue, slight cyan tint
        ])
    """
    arr = np.asarray(img, dtype=np.float32) / 255.0
    out = np.tensordot(arr, matrix.T, axes=([2], [0]))   # shape H×W×3
    out = np.clip(out, 0.0, 1.0)
    return Image.fromarray((out * 255).astype(np.uint8), mode="RGB")


def _desaturate_bright(img: "Image.Image",
                       threshold: float = 0.80,
                       strength: float = 0.65) -> "Image.Image":
    """Gradually desaturate very bright pixels (star cores) toward white.

    This prevents bright-star diffraction spikes from looking neon when a
    saturation boost is later applied.  Pixels below *threshold* luminosity
    are unchanged; pixels above it are progressively blended toward grey.

    Parameters
    ----------
    threshold:
        Luminosity above which desaturation starts (0–1).  Default 0.80.
    strength:
        Maximum fraction of desaturation at luminosity=1.  Default 0.65.
    """
    arr = np.asarray(img, dtype=np.float32) / 255.0
    # Perceived luminosity (Rec.709)
    lum = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    # Weight: 0 below threshold, rises linearly to `strength` at lum=1
    w = np.clip((lum - threshold) / (1.0 - threshold + 1e-8), 0.0, 1.0) * strength
    w = w[:, :, np.newaxis]                 # broadcast over channels
    grey = lum[:, :, np.newaxis]
    out = arr * (1.0 - w) + grey * w
    return Image.fromarray((np.clip(out, 0.0, 1.0) * 255).astype(np.uint8), mode="RGB")


# Keep old name as alias so callers that used _prep_channel still work
def _prep_channel(data: np.ndarray, percentile_lo: float, percentile_hi: float) -> np.ndarray:
    """Legacy alias — calls :func:`_subtract_sky` with independent p99 scaling.

    Retained for backwards compatibility with unit tests.  New code should
    use :func:`_subtract_sky` followed by linked normalisation in the caller.
    """
    data = _subtract_sky(data, percentile_lo)
    p99 = float(np.percentile(data[data > 0], 99.0)) if data.max() > 0 else 1.0
    if p99 > 0:
        data = data / p99
    return data


def render_lupton_rgb(
    red_path: Path,
    green_path: Path,
    blue_path: Path,
    cfg: Config,
    output_path: Optional[Path] = None,
    Q: float = 9.0,
    stretch: float = 0.35,
    r_gain: float = 1.0,
    g_gain: float = 0.85,
    b_gain: float = 0.90,
    saturation: float = 0.75,
    scurve_strength: float = 0.22,
) -> Path:
    """Composite three FITS files into a true-colour wallpaper using Lupton RGB.

    The Lupton et al. (2004) algorithm applies a shared asinh stretch across
    all three channels.  Because the stretch is luminance-based rather than
    per-channel, colours are preserved across the full dynamic range — bright
    stars stay white, faint nebulae show their hue, and nothing gets clipped
    to a single channel.

    NASA press-release colour treatment
    ------------------------------------
    This function matches the STScI/NASA approach used for images like the
    Pillars of Creation and Carina Nebula:

    1. **Independent per-channel normalisation** — each filter is stretched
       to its own 99th-percentile so no single filter dominates.  This is
       different from preserving physical flux ratios; it ensures all three
       channels contribute meaningfully to the composite.

    2. **Channel gain tuning** — after normalisation, per-channel multipliers
       (``r_gain``, ``g_gain``, ``b_gain``) push the palette toward the
       characteristic NASA look: golden/amber dust pillars on a deep blue
       stellar background.  The defaults (R×0.85, G×1.0, B×1.3) cool down
       the red channel slightly and boost the blue so background stars read
       as blue rather than grey.

    3. **S-curve contrast + saturation boost** — gives photographic punch and
       compensates for the desaturation inherent in Lupton's asinh stretch.

    Parameters
    ----------
    red_path, green_path, blue_path:
        FITS files assigned to the R, G, B channels.  Typically long- to
        short-wavelength filters (e.g. F444W → red, F200W → green,
        F090W → blue for NIRCam).
    cfg:
        Rendering configuration.  ``percentile_lo`` is used for sky estimation.
    output_path:
        Destination PNG path.  Defaults to ``<red_stem>_RGB.png`` in the
        wallpaper cache directory.
    Q:
        Lupton softening parameter.  Lower → richer, more saturated colours
        (more aggressive asinh compression of bright peaks).  Default 8.
    stretch:
        Lupton stretch scale.  Smaller values pull up more faint nebular
        detail.  Default 0.4.
    r_gain, g_gain, b_gain:
        Per-channel gain applied *after* independent p99 normalisation.
        Defaults (0.85 / 1.0 / 1.3) push warm dust toward orange-amber and
        boost the blue stellar background — matching the NASA press-release
        palette.  Set all to 1.0 for equal-weight channels.
    saturation:
        PIL colour-saturation multiplier applied after Lupton compositing.
        1.0 = unchanged; 1.8 = moderate boost (default).
    scurve_strength:
        Strength of the photographic S-curve contrast, in [0, 1].
        0 = identity; 0.35 = moderate (default).

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

    # ── Step 1: Sky subtraction (independent per channel) ─────────────────────
    r = _subtract_sky(r_raw, cfg.percentile_lo)
    g = _subtract_sky(g_raw, cfg.percentile_lo)
    b = _subtract_sky(b_raw, cfg.percentile_lo)

    # ── Step 2: Independent per-channel normalisation + gain ──────────────────
    # Each channel is scaled to its own 99th percentile, so no single filter
    # overwhelms the others regardless of their relative flux calibrations.
    # A per-channel gain is then applied to shape the palette:
    #   • r_gain < 1  → cools warm dust from pure-red toward orange-amber
    #   • b_gain > 1  → boosts short-wavelength stars so backgrounds read blue
    def _norm(arr: np.ndarray, gain: float) -> np.ndarray:
        pos = arr[arr > 0]
        p99 = float(np.percentile(pos, 99.0)) if pos.size else 1.0
        return (arr / p99 * gain) if p99 > 0 else arr * gain

    r = _norm(r, r_gain)
    g = _norm(g, g_gain)
    b = _norm(b, b_gain)

    # ── Step 3: Lupton composite ──────────────────────────────────────────────
    # → uint8 RGB array (H × W × 3).
    # Suppress the expected divide-by-zero warning from zero-intensity edge pixels.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rgb_array = make_lupton_rgb(r, g, b, Q=Q, stretch=stretch)

    # ── Step 4: Post-processing for photographic colour ───────────────────────
    img = Image.fromarray(rgb_array, mode="RGB")

    # 4a. S-curve contrast — darkens shadows, preserves mid-tones, lifts highlights
    if scurve_strength > 0:
        arr = np.asarray(img, dtype=np.float32) / 255.0
        arr = _apply_scurve(arr, strength=scurve_strength)
        img = Image.fromarray((arr * 255).clip(0, 255).astype(np.uint8), mode="RGB")

    # 4b. Colour matrix — shift the raw Lupton palette toward the NASA press-release
    #     look: red/pink nebula → orange-amber; green hazes → teal; blue stays blue.
    #
    #     Row sums = [1.0, 1.0, 1.0] — luminosity-preserving.
    #
    #         R' = 0.80·R + 0.20·G            (warm orange-brown for dusty pillars)
    #         G' = 0.25·R + 0.65·G + 0.10·B  (golden mid-tones)
    #         B' = 0.40·G + 0.60·B           (F200W feeds into blue → cyan wisps at
    #                                          ionised gas / dust interfaces, like F187N)
    _pillars_matrix = np.array([
        [0.80, 0.20, 0.00],
        [0.25, 0.65, 0.10],
        [0.00, 0.40, 0.60],
    ], dtype=np.float32)
    img = _color_matrix(img, _pillars_matrix)

    # 4c. Desaturate very bright pixels (star cores / diffraction spikes) so they
    #     read as white/near-white rather than neon-coloured after the saturation step.
    #     Lower threshold (0.55) catches more of the diffraction spike arms.
    img = _desaturate_bright(img, threshold=0.55, strength=0.85)

    # 4d. Saturation boost — Lupton asinh inherently desaturates; compensate
    if saturation != 1.0:
        img = _boost_saturation(img, saturation)

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
