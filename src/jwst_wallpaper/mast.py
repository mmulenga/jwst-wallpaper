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

    Parameters
    ----------
    target:
        Object name or coordinates string accepted by MAST (e.g. ``"Carina Nebula"``).
    instrument:
        JWST instrument name: ``NIRCam``, ``MIRI``, ``NIRSpec``, or ``NIRISS``.
    max_results:
        Maximum number of rows to return.

    Returns
    -------
    astropy.table.Table
        Observations table with columns including ``obsid``, ``target_name``,
        ``t_exptime``, ``filters``, and ``dataURL``.
    """
    console.print(f"[bold cyan]Searching MAST[/] for [yellow]{target}[/] ({instrument})…")
    obs = Observations.query_criteria(
        target_name=target,
        obs_collection="JWST",
        instrument_name=instrument,
        dataproduct_type="IMAGE",
    )
    if obs is None or len(obs) == 0:
        return Table()

    # Sort by exposure time (longest first — most detail)
    obs.sort("t_exptime", reverse=True)
    return obs[:max_results]


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
                    f"mast:{row['dataURI']}",
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
    console.print(f"Found [bold]{len(products)}[/] science products.")

    return download_products(products, dest_dir, filters=filters)
