"""Query NASA's MAST archive for JWST calibrated images and download FITS files.

JWST file naming conventions used here:
  *_i2d.fits  — 2-D drizzled combined science image  (best for wallpapers)
  *_cal.fits  — individual calibrated exposure

We specifically request Level 3 (combined) products when available, falling
back to Level 2 calibrated frames.
"""

from __future__ import annotations

import re
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

# MAST product type filters
_SCIENCE_TYPES = {"SCIENCE"}
_I2D_PATTERN = re.compile(r"_i2d\.fits$", re.IGNORECASE)
_CAL_PATTERN = re.compile(r"_cal\.fits$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_observations(
    target: str,
    instrument: str = "NIRCam",
    max_results: int = 10,
) -> Table:
    """Return a table of JWST observations matching *target*.

    Uses ``query_object`` (Sesame name resolver) so common names like
    ``"Carina Nebula"`` or ``"Pillars of Creation"`` work without knowing the
    exact MAST target_name string.  Falls back to a wildcard ``query_criteria``
    search if the resolver returns nothing.

    Parameters
    ----------
    target:
        Object name accepted by the CDS Sesame name resolver, or a wildcard
        string for MAST (e.g. ``"*carina*"``).
    instrument:
        JWST instrument name: ``NIRCam``, ``MIRI``, ``NIRSpec``, or ``NIRISS``.
    max_results:
        Maximum number of rows to return.

    Returns
    -------
    astropy.table.Table
        Observations table.
    """
    console.print(f"[bold cyan]Searching MAST[/] for [yellow]{target}[/] ({instrument})…")

    # --- Strategy 1: Sesame name resolver (handles common object names) ------
    try:
        obs = Observations.query_object(
            target,
            radius="5 arcmin",
        )
        if obs is not None and len(obs) > 0:
            # instrument_name in MAST is like "NIRCAM/IMAGE" — use substring match
            instr_upper = instrument.upper().replace("NIR", "NIR")  # normalise
            mask = (
                (obs["obs_collection"] == "JWST")
                & ([instr_upper in str(v).upper() for v in obs["instrument_name"]])
                & (obs["dataproduct_type"] == "image")
                & ([str(r).upper() in ("PUBLIC", "OPEN")
                    for r in obs["dataRights"]])
            )
            filtered = obs[mask]
            if len(filtered) > 0:
                filtered.sort("t_exptime", reverse=True)
                return filtered[:max_results]
            # If everything is proprietary, warn but still return for the error msg
            jwst_mask = (
                (obs["obs_collection"] == "JWST")
                & ([instr_upper in str(v).upper() for v in obs["instrument_name"]])
            )
            if obs[jwst_mask] is not None and len(obs[jwst_mask]) > 0:
                console.print(
                    f"  [yellow]Found {len(obs[jwst_mask])} JWST observation(s) but all are "
                    f"proprietary. Try a different target or provide a MAST token.[/]"
                )
    except Exception as exc:
        console.print(f"  [dim]query_object failed ({exc}), trying wildcard…[/]")

    # --- Strategy 2: Wildcard target_name match (public only) ----------------
    wildcard = f"*{target.replace(' ', '*')}*"
    try:
        obs = Observations.query_criteria(
            target_name=wildcard,
            obs_collection="JWST",
            instrument_name=f"*{instrument.upper()}*",
            dataproduct_type="image",
            dataRights="PUBLIC",
        )
        if obs is not None and len(obs) > 0:
            obs.sort("t_exptime", reverse=True)
            return obs[:max_results]
    except Exception:
        pass

    return Table()


def search_by_coordinates(
    ra: float,
    dec: float,
    radius_arcmin: float = 3.0,
    instrument: str = "NIRCam",
    max_results: int = 10,
) -> Table:
    """Search MAST by RA/Dec position.

    Parameters
    ----------
    ra, dec:
        ICRS coordinates in degrees.
    radius_arcmin:
        Search cone radius in arcminutes.
    """
    from astropy.coordinates import SkyCoord
    import astropy.units as u

    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    console.print(f"[bold cyan]Searching MAST[/] at RA={ra:.4f} Dec={dec:.4f}…")
    obs = Observations.query_region(
        coord,
        radius=radius_arcmin * u.arcmin,
    )
    if obs is None or len(obs) == 0:
        return Table()

    mask = (obs["obs_collection"] == "JWST") & (obs["instrument_name"] == instrument)
    obs = obs[mask]
    obs.sort("t_exptime", reverse=True)
    return obs[:max_results]


# ---------------------------------------------------------------------------
# Product selection
# ---------------------------------------------------------------------------

def get_best_products(obs_table: Table, prefer_i2d: bool = True) -> Table:
    """Retrieve data products for a set of observations, preferring i2d images.

    Only returns products from *public* observations (``dataRights == 'PUBLIC'``
    or ``dataRights == 'OPEN'``).  Proprietary data requires a MAST auth token;
    see ``--mast-token`` if you have one.

    Parameters
    ----------
    obs_table:
        Output from :func:`search_observations` or :func:`search_by_coordinates`.
    prefer_i2d:
        When True, keep only ``_i2d.fits`` files (combined mosaics).
        Falls back to ``_cal.fits`` if none are found.
    """
    if len(obs_table) == 0:
        return Table()

    # Filter observations to those that are publicly accessible
    if "dataRights" in obs_table.colnames:
        pub_mask = [str(r).upper() in ("PUBLIC", "OPEN") for r in obs_table["dataRights"]]
        public_obs = obs_table[pub_mask]
        if len(public_obs) == 0:
            console.print("[yellow]All matched observations are proprietary. "
                          "Try --mast-token or a different target.[/]")
            return Table()
        if len(public_obs) < len(obs_table):
            console.print(f"  [dim]{len(obs_table) - len(public_obs)} proprietary observation(s) skipped.[/]")
        obs_table = public_obs

    products = Observations.get_product_list(obs_table)
    # Filter to science data only
    science = Observations.filter_products(
        products,
        productType="SCIENCE",
        calib_level=[3, 2],  # 3 = combined, 2 = calibrated
    )

    if prefer_i2d:
        i2d = science[[bool(_I2D_PATTERN.search(str(r["productFilename"]))) for r in science]]
        if len(i2d) > 0:
            return i2d
        # Fall back to calibrated
    cal = science[[bool(_CAL_PATTERN.search(str(r["productFilename"]))) for r in science]]
    return cal if len(cal) > 0 else science


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

    # Check which files are already cached
    already: list[Path] = []
    to_download = []
    for row in products:
        dest_path = dest_dir / str(row["productFilename"])
        if dest_path.exists():
            console.print(f"  [dim]Cached[/] {row['productFilename']}")
            already.append(dest_path)
        else:
            to_download.append(row)

    if not to_download:
        return already

    # Use the bulk downloader — it handles auth, retries, and flat file layout
    from astropy.table import vstack
    to_dl_table = products[[
        str(r["productFilename"]) in {str(x["productFilename"]) for x in to_download}
        for r in products
    ]]

    console.print(f"Downloading [bold]{len(to_dl_table)}[/] file(s)…")
    try:
        manifest = Observations.download_products(
            to_dl_table,
            download_dir=str(dest_dir),
            flat=True,  # put all files directly in dest_dir (no mastDownload/ nesting)
        )
    except TypeError:
        # Older astroquery versions don't support flat=
        manifest = Observations.download_products(
            to_dl_table,
            download_dir=str(dest_dir),
        )

    downloaded: list[Path] = list(already)
    if manifest is not None:
        for row in manifest:
            status = str(row.get("Status", row.get("status", ""))).upper()
            local = row.get("Local Path", row.get("local_path", ""))
            if status == "COMPLETE" and local:
                p = Path(str(local))
                if not p.exists():
                    # flat=False puts files under mastDownload/JWST/<obsid>/
                    # search for the filename anywhere under dest_dir
                    matches = list(dest_dir.rglob(p.name))
                    if matches:
                        p = matches[0]
                if p.exists():
                    # Move to flat dest_dir if nested
                    flat_dest = dest_dir / p.name
                    if p != flat_dest:
                        p.rename(flat_dest)
                        p = flat_dest
                    downloaded.append(p)
                    console.print(f"  [green]✓[/] {p.name}")
            else:
                console.print(f"  [red]Failed[/] {local}: {status}")

    return downloaded


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

    This is the high-level convenience wrapper used by the CLI.
    """
    obs = search_observations(target, instrument=instrument, max_results=max_observations)
    if len(obs) == 0:
        console.print(f"[red]No JWST {instrument} observations found for '{target}'.[/]")
        return []

    console.print(f"Found [bold]{len(obs)}[/] observations.")
    products = get_best_products(obs)
    # Cap to one i2d file per observation so we don't flood the cache
    cap = max_observations * 2
    if len(products) > cap:
        console.print(
            f"Found [bold]{len(products)}[/] science products — "
            f"downloading the first [bold]{cap}[/] (one per detector/filter)."
        )
        products = products[:cap]
    else:
        console.print(f"Found [bold]{len(products)}[/] science products.")

    return download_products(products, dest_dir, filters=filters)
