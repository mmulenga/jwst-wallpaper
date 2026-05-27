# jwst-wallpaper

Download raw FITS science images from NASA's [MAST archive](https://mast.stsci.edu) and render them as desktop wallpapers.

```
jwst-wallpaper run "Pillars of Creation"
```

## Features

- **Queries MAST** via `astroquery` for JWST Level-3 (`_i2d.fits`) drizzled mosaics, falling back to Level-2 calibrated frames
- **Astronomy-grade rendering** — per-percentile clipping + asinh/log/sqrt/histogram-equalization stretch via `astropy.visualization`
- **False-colour** — any matplotlib colormap (`inferno`, `magma`, `viridis`, …)
- **RGB compositing** — combine three single-filter FITS images into a Hubble-palette-style composite
- **Cross-platform wallpaper setting** — GNOME, KDE Plasma, Sway, X11 (feh/xwallpaper), macOS, Windows
- **Local cache** — FITS files and rendered PNGs are tracked in a JSON index; oldest wallpapers are auto-pruned

## Installation

```bash
# From source (development)
pip install -e ".[dev]"

# From PyPI (once published)
pip install jwst-wallpaper
```

Python ≥ 3.10 required.

## Quick start

```bash
# One-shot: fetch + render + set wallpaper
jwst-wallpaper run "Carina Nebula"

# With options
jwst-wallpaper run "Stephan's Quintet" --colormap magma --stretch log

# Separate steps
jwst-wallpaper fetch "NGC 3324" --instrument NIRCam --max-obs 3
jwst-wallpaper render                          # renders most recent FITS
jwst-wallpaper set                             # sets most recent wallpaper

# RGB composite (three filters → red/green/blue channels)
jwst-wallpaper render --rgb \
  --red   jw01234_F444W_i2d.fits \
  --green jw01234_F277W_i2d.fits \
  --blue  jw01234_F090W_i2d.fits

# List cache
jwst-wallpaper list
jwst-wallpaper list --rendered

# Configuration
jwst-wallpaper config show
jwst-wallpaper config set colormap plasma
jwst-wallpaper config set stretch asinh
jwst-wallpaper config set width 2560
jwst-wallpaper config set height 1440
```

## Available colormaps

Any matplotlib colormap name works. Recommendations for JWST data:

| Colormap | Character |
|----------|-----------|
| `inferno` | Gold/red — warm nebulae (default) |
| `magma` | Deep purple/white — galaxy fields |
| `plasma` | Purple-to-yellow — high contrast |
| `viridis` | Blue-green-yellow — balanced |
| `afmhot` | Hot metal — emission regions |
| `Greys_r` | White-on-black monochrome |

## Stretch algorithms

| Stretch | Best for |
|---------|----------|
| `asinh` | Broad dynamic range, preserves faint detail (default) |
| `log` | Compact bright cores + faint halos |
| `sqrt` | Gentle compression, less aggressive than log |
| `linear` | Raw linear mapping (often too dark) |
| `histeq` | Maximises contrast, normalises histogram |

## Cache locations

| Platform | Path |
|----------|------|
| Linux | `~/.cache/jwst-wallpaper/` |
| macOS | `~/Library/Caches/jwst-wallpaper/` |
| Windows | `%LOCALAPPDATA%\jwst-wallpaper\jwst-wallpaper\Cache\` |

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src/
mypy src/
```

## Data acknowledgement

This program uses data from the Mikulski Archive for Space Telescopes (MAST) at the Space Telescope Science Institute. JWST data is funded by NASA.

## License

MIT
