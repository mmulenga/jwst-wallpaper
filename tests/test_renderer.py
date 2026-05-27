"""Tests for the FITS renderer."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from jwst_wallpaper import config as cfg_module
from jwst_wallpaper.renderer import fits_to_array, render_single, _fit_to_canvas


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def simple_fits(tmp_path: Path) -> Path:
    """Write a minimal valid FITS file with a SCI extension."""
    rng = np.random.default_rng(42)
    data = rng.exponential(scale=100, size=(256, 256)).astype(np.float32)

    hdul = fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(data=data, name="SCI"),
    ])
    path = tmp_path / "test_sci.fits"
    hdul.writeto(str(path))
    return path


@pytest.fixture()
def default_cfg() -> cfg_module.Config:
    return cfg_module.Config(width=1920, height=1080)


# ---------------------------------------------------------------------------
# fits_to_array
# ---------------------------------------------------------------------------

class TestFitsToArray:
    def test_reads_sci_extension(self, simple_fits: Path) -> None:
        data, header = fits_to_array(simple_fits)
        assert data.ndim == 2
        assert data.shape == (256, 256)

    def test_returns_float64(self, simple_fits: Path) -> None:
        data, _ = fits_to_array(simple_fits)
        assert data.dtype == np.float64

    def test_raises_on_missing_file(self) -> None:
        with pytest.raises(Exception):
            fits_to_array(Path("/nonexistent/file.fits"))


# ---------------------------------------------------------------------------
# render_single
# ---------------------------------------------------------------------------

class TestRenderSingle:
    def test_produces_png(self, simple_fits: Path, default_cfg: cfg_module.Config,
                          tmp_path: Path) -> None:
        out = tmp_path / "wallpaper.png"
        result = render_single(simple_fits, default_cfg, output_path=out)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 0

    def test_output_dimensions(self, simple_fits: Path, default_cfg: cfg_module.Config,
                               tmp_path: Path) -> None:
        from PIL import Image
        out = tmp_path / "wallpaper.png"
        render_single(simple_fits, default_cfg, output_path=out)
        img = Image.open(out)
        assert img.size == (default_cfg.width, default_cfg.height)

    @pytest.mark.parametrize("stretch", ["asinh", "log", "sqrt", "linear"])
    def test_all_stretches(self, simple_fits: Path, default_cfg: cfg_module.Config,
                           tmp_path: Path, stretch: str) -> None:
        default_cfg.stretch = stretch  # type: ignore[assignment]
        out = tmp_path / f"wallpaper_{stretch}.png"
        render_single(simple_fits, default_cfg, output_path=out)
        assert out.exists()


# ---------------------------------------------------------------------------
# _fit_to_canvas
# ---------------------------------------------------------------------------

class TestFitToCanvas:
    def test_wide_image_fits_width(self) -> None:
        from PIL import Image
        img = Image.new("RGB", (4000, 1000))
        result = _fit_to_canvas(img, 1920, 1080)
        assert result.size == (1920, 1080)

    def test_tall_image_fits_height(self) -> None:
        from PIL import Image
        img = Image.new("RGB", (500, 3000))
        result = _fit_to_canvas(img, 1920, 1080)
        assert result.size == (1920, 1080)

    def test_exact_fit(self) -> None:
        from PIL import Image
        img = Image.new("RGB", (1920, 1080))
        result = _fit_to_canvas(img, 1920, 1080)
        assert result.size == (1920, 1080)
