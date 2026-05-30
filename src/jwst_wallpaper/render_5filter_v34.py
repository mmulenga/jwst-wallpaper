"""5-filter Lupton RGB — v34.

Problem with v32/v33 (blanket G-reduction):
  G-reduction affects ALL warm pixels including borderline warm (hue 15-20°, G barely > B).
  Those pixels flip: G_new < B → hue 350-360° (purple) → pulls mean hue DOWN to 310-315°.

v34 fix: TARGETED G-reduction — only where G > 1.5×B (deeply orange, G >> B).
  At G/B=1.5, pixel is at hue≈25°. Reducing G by 25% → G/B approaches 1.0 → hue→5-15° (red).
  Pixels where G/B < 1.5 (hue < 25°, near warm/red boundary): NOT touched → no flip.

Effect:
  Deeply orange (hue 25-45°): shifted toward orange-red (10-20°) ✓
  Near-warm (hue 15-25°): unchanged → stays in warm% ✓
  Near-red (hue < 15°): unchanged ✓
  Cool (B>R or G>R): excluded by R_dominant mask ✓

Also: retain a gentle warm_dust_filter(0.10) to nudge hue toward 336° (needs slight B reduction
for the warm pixels that stay, which helps push mean hue slightly toward 340°).

Channel mapping: identical to v29.
"""

import warnings
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

FITS_DIR = Path.home() / ".cache" / "jwst-wallpaper" / "fits"
OUT_DIR  = Path.home() / ".cache" / "jwst-wallpaper" / "wallpapers"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT   = OUT_DIR / "jw02731-o001_5filter_RGB_v34.png"

F444W = FITS_DIR / "jw02731-o001_t017_nircam_clear-f444w_i2d.fits"
F335M = FITS_DIR / "jw02731-o001_t017_nircam_clear-f335m_i2d.fits"
F200W = FITS_DIR / "jw02731-o001_t017_nircam_clear-f200w_i2d.fits"
F187N = FITS_DIR / "jw02731-o001_t017_nircam_clear-f187n_i2d.fits"
F090W = FITS_DIR / "jw02731-o001_t017_nircam_clear-f090w_i2d.fits"

WIDTH, HEIGHT = 3840, 2160
MAX_CLIP = 3.0


def load_fits(path):
    print(f"  {path.name}", flush=True)
    with fits.open(path, memmap=True) as hdul:
        if "SCI" in hdul:
            data = hdul["SCI"].data.astype(np.float64)
        elif len(hdul) > 1 and hdul[1].data is not None:
            data = hdul[1].data.astype(np.float64)
        else:
            data = hdul[0].data.astype(np.float64)
    if data.ndim == 3:
        data = np.nanmedian(data, axis=0)
    return data


def sky_subtract(data, pct_lo=10.0):
    data = np.where(np.isfinite(data), data, 0.0)
    finite = data[data > 0]
    if finite.size:
        data -= np.percentile(finite, pct_lo)
    return np.clip(data, 0.0, None)


def p99_norm_clipped(data, gain=1.0, max_clip=MAX_CLIP):
    pos = data[data > 0]
    p99 = float(np.percentile(pos, 99.0)) if pos.size else 1.0
    return np.clip(data / p99 * gain, 0.0, max_clip)


def apply_scurve(arr, strength=0.15):
    k   = 4.0 + strength * 8.0
    sig = 1.0 / (1.0 + np.exp(-k * (arr - 0.5)))
    lo  = 1.0 / (1.0 + np.exp(-k * (0.0 - 0.5)))
    hi  = 1.0 / (1.0 + np.exp(-k * (1.0 - 0.5)))
    return np.clip((sig - lo) / (hi - lo), 0.0, 1.0)


def gamma_correct(img, gamma=0.82):
    arr = np.asarray(img, dtype=np.float32) / 255.0
    out = np.power(np.clip(arr, 0.0, 1.0), gamma)
    return Image.fromarray((out * 255).astype(np.uint8), mode="RGB")


def whiten_stars(img, threshold=0.68, strength=0.90):
    arr = np.asarray(img, dtype=np.float32) / 255.0
    brightness = arr.max(axis=2)
    w = np.clip((brightness - threshold) / (1.0 - threshold + 1e-8), 0.0, 1.0) * strength
    w = w[:, :, np.newaxis]
    out = arr * (1.0 - w) + np.ones_like(arr) * w
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), mode="RGB")


def targeted_saturation(img, dark_factor=0.64, star_factor=0.94,
                         midpoint=0.78, steepness=25.0):
    arr = np.asarray(img, dtype=np.float32) / 255.0
    brightness = arr.max(axis=2)
    lum = (0.2126 * arr[:,:,0] + 0.7152 * arr[:,:,1]
           + 0.0722 * arr[:,:,2])[:, :, np.newaxis]
    blend = 1.0 / (1.0 + np.exp(-steepness * (brightness - midpoint)))
    factor = dark_factor + (star_factor - dark_factor) * blend
    f3 = factor[:, :, np.newaxis]
    out = arr * f3 + lum * (1.0 - f3)
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), mode="RGB")


def orange_to_red(img, reduction=0.28, val_lo=0.20, val_hi=0.65, gb_ratio=1.5):
    """Targeted G-reduction for DEEPLY orange (G >> B) R-dominant pixels.

    Only applies where G > gb_ratio × B (i.e., G/B > 1.5, hue ≈ 25-90°).
    Near-warm pixels (G/B < 1.5, hue < 25°) are NOT touched → no purple flip.

    Effect at reduction=0.28:
      hue=30° (G/B=2.0) → ~12° (deep red-orange) ✓
      hue=25° (G/B=1.5) → borderline, ~15° ✓
      hue=20° (G/B=1.2) → not touched (G/B < 1.5) ✓
    """
    arr = np.asarray(img, dtype=np.float32) / 255.0
    brightness = arr.max(axis=2)
    is_mid      = (brightness >= val_lo) & (brightness < val_hi)
    R_dominant  = (arr[:,:,0] == brightness)
    deep_orange = arr[:,:,1] > gb_ratio * arr[:,:,2]   # G >> B: safely reduce G
    mask = is_mid & R_dominant & deep_orange
    b_norm = np.clip((brightness - val_lo) / (val_hi - val_lo), 0.0, 1.0)
    w = np.sin(np.pi * b_norm) * reduction
    arr[:,:,1] = arr[:,:,1] * (1.0 - np.where(mask, w, 0.0))
    return Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), mode="RGB")


def warm_dust_filter(img, reduction=0.10, val_lo=0.20, val_hi=0.65):
    """Gentle B-reduction — keeps warm pixels from flipping to purple-cool.
    Also nudges circular mean hue slightly toward 336° (B reduction → less B → mean shifts).
    Small value (0.10) — not enough to cause hue regression, just stabilizes warmth.
    """
    arr = np.asarray(img, dtype=np.float32) / 255.0
    brightness = arr.max(axis=2)
    is_mid     = (brightness >= val_lo) & (brightness < val_hi)
    R_dominant = (arr[:,:,0] == brightness)
    mask       = is_mid & R_dominant
    b_norm = np.clip((brightness - val_lo) / (val_hi - val_lo), 0.0, 1.0)
    w = np.sin(np.pi * b_norm) * reduction
    arr[:,:,2] = arr[:,:,2] * (1.0 - np.where(mask, w, 0.0))
    return Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), mode="RGB")


def blue_floor_background(img, hard_thresh=0.16, dark_thresh=0.22):
    arr = np.asarray(img, dtype=np.float32) / 255.0
    brightness = arr.max(axis=2)
    target_norm = np.array([34.0/37.0, 29.0/37.0, 1.0])
    w = np.clip(1.0 - (brightness - hard_thresh) / (dark_thresh - hard_thresh), 0.0, 1.0)
    for ch, t in enumerate(target_norm):
        target_ch = brightness * t
        arr[:, :, ch] = arr[:, :, ch] * (1.0 - w) + target_ch * w
    return Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), mode="RGB")


def fit_canvas(img, w, h):
    ratio = img.width / img.height
    if ratio > w / h:
        nw, nh = w, round(w / ratio)
    else:
        nh, nw = h, round(h * ratio)
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(img.resize((nw, nh), Image.LANCZOS), ((w-nw)//2, (h-nh)//2))
    return canvas


def report(img, label):
    import colorsys
    arr = np.asarray(img, dtype=np.float32) / 255.0
    h, wd = arr.shape[:2]
    step = max(1, int(((h * wd) / 500_000) ** 0.5))
    s = arr[::step, ::step]
    r, g, b = s[:,:,0], s[:,:,1], s[:,:,2]
    bright = s.max(axis=2) > 0.80
    mid    = (s.max(axis=2) >= 0.15) & (~bright)
    dark   = s.max(axis=2) < 0.15
    print(f"\n  [{label}]", flush=True)
    print(f"    Overall R={r.mean():.3f} G={g.mean():.3f} B={b.mean():.3f}"
          f"  R/G={r.mean()/max(g.mean(),1e-4):.3f} B/G={b.mean()/max(g.mean(),1e-4):.3f}",
          flush=True)
    for mask, name in [(bright, "stars"), (mid, "nebula"), (dark, "bg")]:
        m = s[mask]
        if not len(m): continue
        mr, mg, mb = m[:,0].mean(), m[:,1].mean(), m[:,2].mean()
        tot = mr + mg + mb
        hsvs = np.array([colorsys.rgb_to_hsv(*p) for p in m[::8]])
        sins = np.sin(2*np.pi*hsvs[:,0]); coss = np.cos(2*np.pi*hsvs[:,0])
        mhue = (np.arctan2(sins.mean(), coss.mean()) / (2*np.pi) % 1) * 360
        h_arr = hsvs[:,0]*360
        warm_pct = (((h_arr >= 15) & (h_arr <= 90)).sum() / max(len(h_arr),1)) * 100
        red_pct  = (h_arr < 15).sum() / max(len(h_arr),1) * 100
        print(f"    {name:6s} n={len(m):>7,}  R/G/B%={mr/tot*100:.0f}/{mg/tot*100:.0f}/{mb/tot*100:.0f}"
              f"  hue={mhue:.0f}°  sat={hsvs[:,1].mean():.3f}  val={hsvs[:,2].mean():.3f}"
              f"  warm%={warm_pct:.0f}%  red%={red_pct:.0f}%",
              flush=True)
    print(f"  NASA: stars 39/33/29 hue=17° sat=0.36 | nebula 37/29/33 hue=336° sat=0.55 warm%=25%"
          f" | BG 34/29/37 hue=271° | R/G=1.26 B/G=1.11", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
print("Loading …", flush=True)
d444 = load_fits(F444W)
d335 = load_fits(F335M)
d200 = load_fits(F200W)
d187 = load_fits(F187N)
d090 = load_fits(F090W)

print("Aligning …", flush=True)
min_h = min(x.shape[0] for x in [d444, d335, d200, d187, d090])
min_w = min(x.shape[1] for x in [d444, d335, d200, d187, d090])
d444, d335, d200, d187, d090 = (x[:min_h, :min_w]
                                 for x in [d444, d335, d200, d187, d090])

print("Sky subtract (pct_lo=10) …", flush=True)
d444, d335, d200, d187, d090 = (sky_subtract(x) for x in [d444, d335, d200, d187, d090])

# Channel mapping: identical to v29
r_raw  = d444 + 0.5 * d335
g_raw  = d335 + 0.5 * d187
b_neb  = d090 + 0.5 * d187
b_star = d200

print("Normalising …", flush=True)
r     = p99_norm_clipped(r_raw,  gain=1.60)
g     = p99_norm_clipped(g_raw,  gain=0.80)
b_n_n = p99_norm_clipped(b_neb,  gain=0.60)
b_s_n = p99_norm_clipped(b_star, gain=0.20)
b     = np.clip(b_n_n + b_s_n, 0.0, MAX_CLIP)

print("Lupton (Q=8, stretch=0.30) …", flush=True)
from astropy.visualization import make_lupton_rgb
with warnings.catch_warnings():
    warnings.simplefilter("ignore", RuntimeWarning)
    rgb = make_lupton_rgb(r, g, b, Q=8.0, stretch=0.30)

img = Image.fromarray(rgb, mode="RGB")
report(img, "post-Lupton")

arr = np.asarray(img, dtype=np.float32) / 255.0
arr = apply_scurve(arr, strength=0.15)
img = Image.fromarray((arr * 255).clip(0, 255).astype(np.uint8), mode="RGB")

img = gamma_correct(img, gamma=0.82)
report(img, "post-gamma")

img = whiten_stars(img, threshold=0.68, strength=0.90)
report(img, "post-star-whiten")

img = targeted_saturation(img, dark_factor=0.64, star_factor=0.94,
                           midpoint=0.78, steepness=25.0)
report(img, "post-targeted-sat")

# Step 1: convert deeply orange (G/B > 1.5) pixels toward red
img = orange_to_red(img, reduction=0.28, val_lo=0.20, val_hi=0.65, gb_ratio=1.5)
report(img, "post-orange-to-red")

# Step 2: gentle B-reduction for remaining warm pixels → keeps them from flipping purple
img = warm_dust_filter(img, reduction=0.10, val_lo=0.20, val_hi=0.65)
report(img, "post-warm-filter")

img = blue_floor_background(img, hard_thresh=0.16, dark_thresh=0.22)
report(img, "post-blue-floor")

img = img.transpose(Image.FLIP_TOP_BOTTOM)
img = fit_canvas(img, WIDTH, HEIGHT)
print(f"\nSaving → {OUTPUT} …", flush=True)
img.save(str(OUTPUT), format="PNG", optimize=False)
print(f"  ✓ {OUTPUT.stat().st_size / 1e6:.1f} MB", flush=True)
print(f"RENDER_COMPLETE:{OUTPUT}", flush=True)
