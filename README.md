# jwst-wallpaper

> **Note:** This project is a personal exploration of [Claude Code](https://claude.ai/code) — an AI-assisted coding workflow. The tool itself is real and functional, but the primary purpose was to see how far Claude Code could take a project from scratch: querying a live science archive, processing genuine astronomy data, and iterating on image rendering to match NASA press-release aesthetics. Every commit in this repo was written collaboratively with Claude.

Download raw FITS science images from NASA's [MAST archive](https://mast.stsci.edu) and render them as desktop wallpapers.

```bash
jwst-wallpaper run "Carina Nebula"
```

## Example output

### v34 — 5-filter composite (current)

Carina Nebula "Cosmic Cliffs" (NGC 3324) — 5-filter Lupton RGB composite from raw JWST NIRCam FITS data, rendered at 3840×2160.

![Carina Nebula 5-filter RGB composite v34](images/carina_nebula_5filter_v34.png)

**Channel mapping:**

| Output | Filters | Physical signal |
|--------|---------|-----------------|
| Red | F444W + 0.5×F335M | Warm dust thermal emission + PAH shoulder |
| Green | F335M + 0.5×F187N | PAH polycyclic aromatic hydrocarbons + ionised H |
| Blue | F090W + 0.5×F187N + F200W\* | Scattered blue starlight + ionised gas + stellar continuum |

\*F200W normalised separately (gain=0.20) and added to blue to give stars a blue tint without contaminating the green channel.

**Processing pipeline:** sky subtraction (10th-pct) → p99 normalisation with per-channel gains (R×1.60, G×0.80, B×0.60+0.20) → Lupton asinh stretch (Q=8, stretch=0.30) → S-curve (strength=0.15) → gamma 0.82 → star whitening (threshold=0.68) → luminance-based targeted desaturation → orange→red dust filter (targeted G-reduction for G/B>1.5 pixels) → dark background hue lock → vertical flip (FITS→screen orientation).

**Colour statistics vs NASA press-release reference:**

| Region | R/G/B% | Hue | Sat | Notes |
|--------|--------|-----|-----|-------|
| Stars (val>0.80) | 37/32/31 | 12° | 0.375 | Target: 39/33/29, 17°, 0.361 |
| Nebula (val 0.15–0.80) | 38/28/34 | 325° | 0.597 | Target: 37/29/33, 336°, 0.553 |
| Background (val<0.15) | 34/29/38 | 286° | 0.201 | Target: 34/29/37, 271° |
| **Overall R/G** | **1.260** | — | — | **Target: 1.260 ✓** |
| **Overall B/G** | **1.102** | — | — | **Target: 1.105 ✓** |

*Raw data: [JWST program 2731](https://www.stsci.edu/jwst/phase2-public/2731.pdf), retrieved from MAST. Colour tuned to match the [NASA/STScI Carina Nebula press-release image](https://science.nasa.gov/missions/webb/nasas-webb-captures-dying-star-s-final-performance-in-fine-detail/) across star hue, nebula saturation, background hue, and overall R/G and B/G ratios.*

---

### v1 — 3-filter composite (original)

Carina Nebula "Cosmic Cliffs" (NGC 3324) — 3-filter Lupton RGB composite.  
F444W → red · F200W → green · F090W → blue. Rendered at 3840×2160.

![Carina Nebula 3-filter RGB composite](images/carina_nebula_rgb.png)

*Raw data: [JWST program 2731](https://www.stsci.edu/jwst/phase2-public/2731.pdf), retrieved from MAST.*

---

## Features

- **Queries MAST** via `astroquery` for JWST Level-3 (`_i2d.fits`) drizzled mosaics, falling back to Level-2 calibrated frames
- **Astronomy-grade rendering** — per-percentile clipping + asinh/log/sqrt/histogram-equalization stretch via `astropy.visualization`
- **False-colour** — any matplotlib colormap (`inferno`, `magma`, `viridis`, …)
- **Lupton RGB compositing** — combine three single-filter FITS images into a true-colour composite using the same algorithm as HST/JWST press-release images
- **NASA palette** — post-composite colour matrix + star desaturation to approximate the look of STScI image releases
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
  --green jw01234_F200W_i2d.fits \
  --blue  jw01234_F090W_i2d.fits

# Tune the colour rendering
jwst-wallpaper render --rgb \
  --red   F444W.fits --green F200W.fits --blue F090W.fits \
  --q 8 --lupton-stretch 0.35 --saturation 0.8 \
  --r-gain 1.0 --g-gain 0.85 --b-gain 0.9

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

## RGB rendering parameters

The Lupton RGB pipeline exposes several knobs for tuning the colour look:

| Flag | Default | Description |
|------|---------|-------------|
| `--q` | `9` | Lupton softening — lower = richer colour saturation |
| `--lupton-stretch` | `0.35` | Stretch scale — lower = more faint nebular detail |
| `--r-gain` | `1.0` | Red channel gain after per-channel normalisation |
| `--g-gain` | `0.85` | Green channel gain |
| `--b-gain` | `0.90` | Blue channel gain |
| `--saturation` | `0.75` | PIL saturation multiplier (< 1 = desaturate toward grey) |

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
