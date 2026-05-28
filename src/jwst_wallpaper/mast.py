"""Query NASA's MAST archive for JWST calibrated images and download FITS files.

JWST file naming conventions used here:
  *_i2d.fits  — 2-D drizzled combined science image  (best for wallpapers)
  *_cal.fits  — individual calibrated exposure

We specifically request Level 3 (combined) products when available, falling
back to Level 2 calibrated frames.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from astropy.table import Table
from astroquery.mast import Observations
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()

# MAST stores instrument names with a mode suffix, e.g. "NIRCAM/IMAGE".
# Map the user-friendly names the CLI accepts to what MAST actually stores.
_INSTRUMENT_MAP: dict[str, str] = {
    "nircam":   "NIRCAM/IMAGE",
    "miri":     "MIRI/IMAGE",
    "nirspec":  "NIRSPEC/MSA",
    "niriss":   "NIRISS/IMAGE",
}

# MAST product type filters
_SCIENCE_TYPES = {"SCIENCE"}
_I2D_PATTERN = re.compile(r"_i2d\.fits$", re.IGNORECASE)
_CAL_PATTERN = re.compile(r"_cal\.fits$", re.IGNORECASE)


def _mast_instrument(instrument: str) -> str:
    """Normalise a user-supplied instrument name to the MAST ``instrument_name`` value."""
    return _INSTRUMENT_MAP.get(instrument.lower(), instrument.upper() + "/IMAGE")


# ---------------------------------------------------------------------------
# Name resolution via Simbad
# ---------------------------------------------------------------------------

def resolve_target(name: str) -> Optional[tuple[float, float]]:
    """Resolve an object name to (RA, Dec) in degrees using Simbad.

    Returns ``None`` if the name cannot be resolved.
    """
    try:
        from astroquery.simbad import Simbad
        result = Simbad.query_object(name)
        if result is None or len(result) == 0:
            return None
        return float(result["ra"][0]), float(result["dec"][0])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_observations(
    target: str,
    instrument: str = "NIRCam",
    max_results: int = 10,
) -> Table:
    """Return a table of JWST observations matching *target* by name.

    Uses the MAST ``target_name`` field which stores the PI-assigned name
    (e.g. ``"M-16"``).  Prefer :func:`search_by_coordinates` for robust
    lookups — this function is kept as a fast-path when the exact name
    is already known.
    """
    mast_instr = _mast_instrument(instrument)
    console.print(f"[bold cyan]Searching MAST[/] for [yellow]{target}[/] ({mast_instr})…")
    try:
        obs = Observations.query_criteria(
            target_name=target,
            obs_collection="JWST",
            instrument_name=mast_instr,
            dataproduct_type="IMAGE",
        )
    except Exception as exc:
        console.print(f"[yellow]MAST name query failed:[/] {exc}")
        return Table()

    if obs is None or len(obs) == 0:
        return Table()

    obs.sort("t_exptime", reverse=True)
    return obs[:max_results]


def search_by_coordinates(
    ra: float,
    dec: float,
    radius_arcmin: float = 3.0,
    instrument: str = "NIRCam",
    max_results: int = 10,
) -> Table:
    """Search MAST by RA/Dec position and filter to JWST observations.

    Parameters
    ----------
    ra, dec:
        ICRS coordinates in degrees.
    radius_arcmin:
        Search cone radius in arcminutes.
    """
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    mast_instr = _mast_instrument(instrument)
    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    console.print(
        f"[bold cyan]Searching MAST[/] at RA={ra:.4f} Dec={dec:.4f} "
        f"(r={radius_arcmin}′, {mast_instr})…"
    )
    try:
        obs = Observations.query_region(coord, radius=radius_arcmin * u.arcmin)
    except Exception as exc:
        console.print(f"[yellow]MAST coordinate query failed:[/] {exc}")
        return Table()

    if obs is None or len(obs) == 0:
        return Table()

    # Filter to the requested JWST instrument
    mask = (obs["obs_collection"] == "JWST") & (obs["instrument_name"] == mast_instr)
    obs = obs[mask]
    if len(obs) == 0:
        # Show what instruments *were* available to help the user
        jwst_mask = obs["obs_collection"] == "JWST"
        if any(jwst_mask):
            available = sorted(set(obs["instrument_name"][jwst_mask]))
            console.print(
                f"[yellow]No {mast_instr} data at this position.[/] "
                f"Available: {', '.join(available)}"
            )
        return Table()

    obs.sort("t_exptime", reverse=True)
    return obs[:max_results]


# ---------------------------------------------------------------------------
# Product selection
# ---------------------------------------------------------------------------

def get_best_products(
    obs_table: Table,
    prefer_i2d: bool = True,
    public_only: bool = True,
) -> Table:
    """Retrieve data products for a set of observations, preferring i2d images.

    Parameters
    ----------
    obs_table:
        Output from :func:`search_observations` or :func:`search_by_coordinates`.
    prefer_i2d:
        When True, keep only ``_i2d.fits`` files (combined mosaics).
        Falls back to ``_cal.fits`` if none are found.
    public_only:
        When True (default), skip products with ``dataRights != 'PUBLIC'`` to
        avoid 401 errors on embargoed files.
    """
    if len(obs_table) == 0:
        return Table()

    products = Observations.get_product_list(obs_table)
    # Filter to science data only
    science = Observations.filter_products(
        products,
        productType="SCIENCE",
        calib_level=[3, 2],  # 3 = combined, 2 = calibrated
    )

    # Drop embargoed products
    if public_only and "dataRights" in science.colnames:
        science = science[science["dataRights"] == "PUBLIC"]

    if prefer_i2d:
        i2d = science[[bool(_I2D_PATTERN.search(str(r["productFilename"]))) for r in science]]
        if len(i2d) > 0:
            return i2d
        # Fall back to calibrated
    cal = science[[bool(_CAL_PATTERN.search(str(r["productFilename"]))) for r in science]]
    return cal  # empty Table is fine — caller must handle no-products case


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_products(
    products: Table,
    dest_dir: Path,
    limit: Optional[int] = None,
    filters: Optional[list[str]] = None,
) -> list[Path]:
    """Download FITS products to *dest_dir*.

    Parameters
    ----------
    products:
        Table from :func:`get_best_products`.
    dest_dir:
        Local directory to save files into.
    limit:
        Maximum number of files to download.
    filters:
        If given, only download products whose filename contains one of these
        filter strings (e.g. ``["F444W", "F277W"]``).

    Returns
    -------
    list[Path]
        Paths of successfully downloaded FITS files.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if filters:
        mask = [
            any(f.upper() in str(row["productFilename"]).upper() for f in filters)
            for row in products
        ]
        products = products[mask]

    if limit is not None:
        products = products[:limit]

    if len(products) == 0:
        console.print("[yellow]No matching products to download.[/]")
        return []

    downloaded: list[Path] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        for row in products:
            filename: str = row["productFilename"]
            dest_path = dest_dir / filename

            if dest_path.exists():
                console.print(f"  [dim]Cached[/] {filename}")
                downloaded.append(dest_path)
                continue

            task = progress.add_task(f"[cyan]{filename}", total=None)
            try:
                result = Observations.download_file(
                    row["dataURI"],
                    local_path=str(dest_path),
                )
                if result[0] == "COMPLETE":
                    downloaded.append(dest_path)
                    progress.update(task, description=f"[green]✓[/] {filename}")
                else:
                    console.print(f"  [red]Failed[/] {filename}: {result[1]}")
            except Exception as exc:
                console.print(f"  [red]Error[/] {filename}: {exc}")
            finally:
                progress.remove_task(task)

    return downloaded


# ---------------------------------------------------------------------------
# Latest observations + download plan
# ---------------------------------------------------------------------------

@dataclass
class DownloadPlan:
    """Summary of what will be downloaded, used by the ``latest`` command."""
    products: Table                          # all products (cached + new)
    already_cached: set[str] = field(default_factory=set)  # filenames on disk
    new_files: list[str] = field(default_factory=list)     # filenames to fetch
    total_bytes: int = 0                     # bytes for new_files
    eta_seconds: float = 0.0                # estimated download time
    mbps_estimate: float = 10.0             # assumed throughput MB/s


def get_latest_observations(
    instrument: str = "NIRCam",
    n: int = 5,
    days_back: int = 365,
) -> Table:
    """Return the *n* most-recently-observed public JWST observations.

    Queries MAST for Level-3 imaging taken within the last *days_back* days,
    then filters to observations whose products are publicly downloadable
    (``dataRights == 'PUBLIC'``).  Sorted newest-first by observation date.

    ``days_back`` defaults to 365 because JWST data has a 12-month exclusive
    use period; observations from the last 90 days are almost always still
    embargoed even when Level-3 products exist in MAST.
    """
    from astropy.time import Time

    mast_instr = _mast_instrument(instrument)
    cutoff_mjd = (Time.now() - days_back * u_day()).mjd

    console.print(
        f"[bold cyan]Searching MAST[/] for the latest public {mast_instr} observations "
        f"(last {days_back} days)…"
    )
    try:
        obs = Observations.query_criteria(
            obs_collection="JWST",
            instrument_name=mast_instr,
            dataproduct_type="IMAGE",
            calib_level=[3],        # Level-3 mosaics only — ensures i2d products exist
            t_min=[cutoff_mjd, 99999.0],
        )
    except Exception as exc:
        console.print(f"[yellow]MAST query failed:[/] {exc}")
        return Table()

    if obs is None or len(obs) == 0:
        return Table()

    # Sort by most recently observed and keep a candidate pool to filter from
    obs.sort("t_min", reverse=True)

    # Walk candidates until we have n observations with publicly available products
    public_obs_ids: list[int] = []
    checked = 0
    batch = 20  # check in batches to avoid huge product queries

    while len(public_obs_ids) < n and checked < len(obs):
        slice_ = obs[checked : checked + batch]
        try:
            products = Observations.get_product_list(slice_)
        except Exception:
            checked += batch
            continue

        for row in slice_:
            obs_id = row["obsid"]
            mask = products["parent_obsid"] == str(obs_id)
            obs_products = products[mask]
            has_public_i2d = any(
                r["dataRights"] == "PUBLIC" and _I2D_PATTERN.search(str(r["productFilename"]))
                for r in obs_products
            )
            if has_public_i2d:
                public_obs_ids.append(obs_id)
                if len(public_obs_ids) >= n:
                    break

        checked += batch

    if not public_obs_ids:
        return Table()

    # Return only the matching rows in original order
    keep_mask = [row["obsid"] in public_obs_ids for row in obs]
    return obs[keep_mask][:n]


def _u_day():
    """Lazy import of astropy units to avoid slow top-level import."""
    import astropy.units as u
    return u.day


def u_day():
    """Return astropy ``day`` unit (cached via module-level call)."""
    import astropy.units as u
    return u.day


def build_download_plan(
    obs_table: Table,
    dest_dir: Path,
    prefer_i2d: bool = True,
    mbps_estimate: float = 10.0,
) -> DownloadPlan:
    """Build a :class:`DownloadPlan` for *obs_table* without downloading anything.

    Inspects *dest_dir* to identify already-cached files so the CLI can show
    an accurate download summary before asking for confirmation.
    """
    products = get_best_products(obs_table, prefer_i2d=prefer_i2d)

    if len(products) == 0:
        return DownloadPlan(products=products)

    # Determine which files are already on disk
    already_cached: set[str] = set()
    new_files: list[str] = []
    total_bytes = 0

    for row in products:
        fname = str(row["productFilename"])
        if (dest_dir / fname).exists():
            already_cached.add(fname)
        else:
            new_files.append(fname)
            try:
                total_bytes += int(row["size"]) if "size" in products.colnames else 0
            except (TypeError, ValueError):
                pass

    eta_seconds = (total_bytes / (mbps_estimate * 1_048_576)) if total_bytes else 0.0

    return DownloadPlan(
        products=products,
        already_cached=already_cached,
        new_files=new_files,
        total_bytes=total_bytes,
        eta_seconds=eta_seconds,
        mbps_estimate=mbps_estimate,
    )


# ---------------------------------------------------------------------------
# Convenience all-in-one helper
# ---------------------------------------------------------------------------

def fetch(
    target: str,
    dest_dir: Path,
    instrument: str = "NIRCam",
    max_observations: int = 5,
    filters: Optional[list[str]] = None,
) -> list[Path]:
    """Search MAST and download the best available FITS images for *target*.

    Resolution order
    ----------------
    1. Direct ``target_name`` query (fast, works when the PI used the same name).
    2. Simbad name → RA/Dec → cone search (robust fallback for popular names).

    This is the high-level convenience wrapper used by the CLI.
    """
    # 1. Fast path: exact name query
    obs = search_observations(target, instrument=instrument, max_results=max_observations)

    # 2. Fallback: resolve via Simbad then search by coordinates
    if len(obs) == 0:
        console.print(
            f"[dim]No results by name — trying Simbad resolution for '{target}'…[/]"
        )
        coords = resolve_target(target)
        if coords is None:
            console.print(
                f"[red]Could not resolve '{target}' via Simbad.[/] "
                "Try a catalog name (e.g. 'M16', 'NGC 3324') or use --ra/--dec."
            )
            return []
        ra, dec = coords
        console.print(f"[dim]Resolved to RA={ra:.4f} Dec={dec:.4f}[/]")
        obs = search_by_coordinates(
            ra, dec, instrument=instrument, max_results=max_observations
        )

    if len(obs) == 0:
        console.print(
            f"[red]No JWST {instrument} observations found for '{target}'.[/]"
        )
        return []

    console.print(f"Found [bold]{len(obs)}[/] observations.")
    products = get_best_products(obs)
    console.print(f"Found [bold]{len(products)}[/] science products.")

    return download_products(products, dest_dir, filters=filters)


# ---------------------------------------------------------------------------
# RGB filter-set fetch
# ---------------------------------------------------------------------------

# Preferred NIRCam RGB filter triplets, ordered by scientific popularity.
# Each tuple is (red_filter, green_filter, blue_filter) by wavelength.
# Wavelength encoded in filter name: F444W = 4.44 µm, F090W = 0.90 µm.
_NIRCAM_RGB_PRESETS: list[tuple[str, str, str]] = [
    # Wide-wavelength (SW+LW) — best colour separation, but SW files are ~5 GB
    ("F444W", "F277W", "F090W"),   # classic deep-field palette (Cosmic Cliffs etc.)
    ("F444W", "F277W", "F115W"),   # good when F090W not observed
    ("F356W", "F200W", "F090W"),   # shorter wavelengths — bluer result
    ("F444W", "F335M", "F115W"),   # medium-band for sharper colour contrast
    ("F470N", "F335M", "F187N"),   # narrow-band Pillars-of-Creation palette
    ("F444W", "F277W", "F150W"),   # alternative blue substitution
    # Long-wavelength only — all ~900 MB; great for nebulae with H2 / PAH emission
    ("F444W", "F335M", "F470N"),   # LW trio: warm dust / PAHs / H2 (Carina, M16)
    ("F444W", "F356W", "F335M"),   # LW broadband trio
]

_MIRI_RGB_PRESETS: list[tuple[str, str, str]] = [
    ("F2100W", "F1130W", "F770W"),
    ("F1800W", "F1130W", "F560W"),
]


def _filter_wavelength(name: str) -> int:
    """Return the approximate wavelength in nm encoded in a JWST filter name.

    E.g. ``F444W`` → 4440, ``F090W`` → 900.
    """
    m = re.match(r"F(\d+)[MWNI]", name.strip(), re.IGNORECASE)
    return int(m.group(1)) * 10 if m else 0


def _best_rgb_triplet(
    available_filters: list[str],
    presets: list[tuple[str, str, str]],
) -> Optional[tuple[str, str, str]]:
    """Return the first preset triplet whose three filters are all available.

    Falls back to automatically choosing the three most-separated filters
    by wavelength if no preset matches.
    """
    upper = {f.upper() for f in available_filters}

    # Try presets first (known-good palettes)
    for red, green, blue in presets:
        if {red, green, blue}.issubset(upper):
            return red, green, blue

    # Auto-select: pick three filters maximally spread in wavelength
    if len(upper) < 3:
        return None
    sorted_f = sorted(upper, key=_filter_wavelength)
    if len(sorted_f) == 3:
        return sorted_f[2], sorted_f[1], sorted_f[0]
    # More than 3: pick shortest, middle, longest
    mid = sorted_f[len(sorted_f) // 2]
    return sorted_f[-1], mid, sorted_f[0]


def fetch_rgb_set(
    target: str,
    dest_dir: Path,
    instrument: str = "NIRCam",
    max_observations: int = 10,
    max_size_mb: int = 1000,
    search_radius_arcmin: float = 20.0,
) -> Optional[tuple[Path, Path, Path]]:
    """Fetch a three-filter set for *target* and return (red, green, blue) paths.

    Searches for JWST observations of *target* (using the same name-resolution
    logic as :func:`fetch`), selects the observation with the most filters, then
    downloads the three filters that best span the available wavelength range.

    Returns ``None`` if fewer than three filter bands are available.
    """
    presets = _MIRI_RGB_PRESETS if "miri" in instrument.lower() else _NIRCAM_RGB_PRESETS

    # Find observations — try name, then Simbad+coordinates with a tight radius
    obs = search_observations(target, instrument=instrument, max_results=max_observations)
    if len(obs) == 0:
        console.print(f"[dim]No results by name — trying Simbad resolution for '{target}'…[/]")
        coords = resolve_target(target)
        if coords is None:
            console.print(
                f"[red]Could not resolve '{target}' via Simbad.[/] "
                "Try a catalog name (e.g. 'M16', 'NGC 3324')."
            )
            return None
        ra, dec = coords
        console.print(f"[dim]Resolved to RA={ra:.4f} Dec={dec:.4f}[/]")
        # Use a moderate radius — large enough to catch offset pointings, small
        # enough to avoid pulling in hundreds of unrelated observations.
        obs = search_by_coordinates(
            ra, dec,
            radius_arcmin=min(search_radius_arcmin, 5.0),
            instrument=instrument,
            max_results=max_observations,
        )

    if len(obs) == 0:
        console.print(f"[red]No JWST {instrument} observations found for '{target}'.[/]")
        return None

    # Get all public i2d products
    products = get_best_products(obs, public_only=True)
    if len(products) == 0:
        console.print("[red]No public Level-3 products found.[/]")
        return None

    filter_re = re.compile(r"[_-](f\d{3}[mwni])[_-]", re.IGNORECASE)
    # Matches the association prefix: jw02731-o001_t017 — programme + target pointing.
    # Files sharing this prefix cover the same field regardless of filter.
    prefix_re = re.compile(r"^(jw\d+(?:-o\d+)?_t\d+)", re.IGNORECASE)
    max_size_bytes = max_size_mb * 1_048_576

    # Group products by field pointing (filename prefix) so all three channels
    # come from the same sky footprint.
    # Key = jw[prog]-o[obs]_t[tgt], value = {filter: [rows]}.
    obs_groups: dict[str, dict[str, list]] = {}
    for row in products:
        fname = str(row["productFilename"])
        fm = filter_re.search(fname)
        pm = prefix_re.match(fname)
        if not fm or not pm:
            continue
        filt = fm.group(1).upper()
        pointing = pm.group(1).lower()
        obs_groups.setdefault(pointing, {}).setdefault(filt, []).append(row)

    if not obs_groups:
        console.print("[red]No filter-tagged i2d products found.[/]")
        return None

    # Score each observation group: prefer groups where the selected triplet
    # has all files under max_size_mb, then prefer widest wavelength spread.
    best_obs_id: Optional[str] = None
    best_triplet: Optional[tuple[str, str, str]] = None
    best_score = -1

    for obs_id, f2rows in obs_groups.items():
        # Prefer filters with at least one file under the size cap
        small_filters = [
            f for f, rows in f2rows.items()
            if any(int(r["size"] or 0) < max_size_bytes for r in rows)
        ]
        candidate_filters = small_filters if len(small_filters) >= 3 else list(f2rows.keys())
        triplet = _best_rgb_triplet(candidate_filters, presets)
        if triplet is None:
            continue
        r, g, b = triplet
        spread = _filter_wavelength(r) - _filter_wavelength(b)
        # Bonus if all three channels are within the size cap
        size_ok = all(
            any(int(row["size"] or 0) < max_size_bytes for row in f2rows[f])
            for f in triplet
        )
        score = spread + (10_000 if size_ok else 0)
        if score > best_score:
            best_score = score
            best_obs_id = obs_id
            best_triplet = triplet

    if best_triplet is None or best_obs_id is None:
        console.print("[red]Could not find an observation with ≥ 3 usable filters.[/]")
        return None

    filter_to_products = obs_groups[best_obs_id]
    all_filters = sorted(filter_to_products.keys(), key=_filter_wavelength)
    console.print(
        f"Using observation [dim]{best_obs_id}[/] — "
        f"[bold]{len(all_filters)}[/] filter(s): "
        + "  ".join(f"[yellow]{f}[/]" for f in all_filters)
    )

    red_f, green_f, blue_f = best_triplet
    console.print(
        f"\n[bold]Selected palette:[/] "
        f"[red]{red_f}[/] = R   [green]{green_f}[/] = G   [blue]{blue_f}[/] = B"
    )

    # Download one file per channel — smallest file under the size cap,
    # falling back to the smallest overall if all exceed the cap.
    dest_dir.mkdir(parents=True, exist_ok=True)
    channel_paths: list[Path] = []
    for filt in (red_f, green_f, blue_f):
        rows = filter_to_products[filt]
        under_cap = [r for r in rows if int(r["size"] or 0) < max_size_bytes]
        pool = under_cap if under_cap else rows
        best = max(pool, key=lambda r: int(r["size"]) if r["size"] else 0)
        fname = str(best["productFilename"])
        size_mb = int(best["size"] or 0) // 1_048_576
        dest_path = dest_dir / fname

        if dest_path.exists() and dest_path.stat().st_size >= int(best["size"] or 0) * 0.99:
            console.print(f"  [dim]Cached[/]   [{filt}] {fname}")
            channel_paths.append(dest_path)
            continue
        elif dest_path.exists():
            console.print(f"  [yellow]Resuming[/] [{filt}] {fname} (truncated, re-downloading)")
            dest_path.unlink()

        console.print(f"  Downloading [{filt}] {fname} ({size_mb} MB)…")
        try:
            result = Observations.download_file(best["dataURI"], local_path=str(dest_path))
            if result[0] == "COMPLETE":
                console.print(f"  [green]✓[/] {fname}")
                channel_paths.append(dest_path)
            else:
                console.print(f"  [red]Failed[/] {fname}: {result[1]}")
                return None
        except Exception as exc:
            console.print(f"  [red]Error[/] {fname}: {exc}")
            return None

    return channel_paths[0], channel_paths[1], channel_paths[2]
