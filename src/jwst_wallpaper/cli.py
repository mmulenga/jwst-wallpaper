"""CLI entry point for jwst-wallpaper.

Commands
--------
  latest   Download the newest public JWST releases (with size preview + confirm)
  fetch    Download FITS images for a target from MAST
  render   Render downloaded FITS files into wallpaper PNGs
  set      Set a rendered wallpaper as the desktop background
  run      fetch + render + set in one step
  list     Show the local cache
  config   Show or modify configuration
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text

from . import __version__
from . import config as cfg_module
from . import cache, mast, renderer, wallpaper

app = typer.Typer(
    name="jwst-wallpaper",
    help="Download JWST FITS data from MAST and render it as your desktop background.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jwst-wallpaper [bold]{__version__}[/]")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    """Human-readable file size."""
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _fmt_seconds(s: float) -> str:
    """Human-readable duration."""
    if s >= 3600:
        h, m = divmod(int(s), 3600)
        return f"{h}h {m:02d}m"
    if s >= 60:
        m, sec = divmod(int(s), 60)
        return f"{m}m {sec:02d}s"
    return f"{int(s)}s"


def _filter_from_filename(name: str) -> str:
    """Extract a filter name like F444W from a JWST filename."""
    import re
    m = re.search(r"[_-](f\d{3}[mwn])[_-]", name, re.IGNORECASE)
    return m.group(1).upper() if m else "—"


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

@app.command()
def latest(
    instrument: str = typer.Option("NIRCam", "-i", "--instrument",
                                   help="JWST instrument: NIRCam, MIRI, NIRSpec, NIRISS."),
    n: int = typer.Option(5, "-n", help="Number of recent observations to fetch."),
    days_back: int = typer.Option(90, "-d", "--days",
                                  help="How many days back to search for newly-public data."),
    rgb: bool = typer.Option(False, "--rgb",
                             help="Attempt a false-colour RGB composite when ≥3 filters "
                                  "are available for the same observation."),
    colormap: Optional[str] = typer.Option(None, "-c", "--colormap",
                                           help="Colormap for single-band renders."),
    stretch: Optional[str] = typer.Option(None, "-s", "--stretch",
                                          help="Stretch: asinh | log | sqrt | linear | histeq"),
    no_set: bool = typer.Option(False, "--no-set", help="Skip setting the wallpaper after render."),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip the download confirmation prompt."),
) -> None:
    """Download the latest public JWST releases, render, and set as wallpaper.

    Shows a summary of file sizes and estimated download time before asking
    for confirmation.  Pass [bold]-y[/] to skip the prompt.
    """
    loaded_cfg = cfg_module.load()
    if colormap:
        loaded_cfg.colormap = colormap  # type: ignore[assignment]
    if stretch:
        loaded_cfg.stretch = stretch  # type: ignore[assignment]

    dest = cfg_module.fits_dir()

    # 1. Query MAST for the newest public observations
    obs_table = mast.get_latest_observations(
        instrument=instrument,
        n=n,
        days_back=days_back,
    )
    if len(obs_table) == 0:
        console.print(
            f"[red]No public JWST {instrument} imaging found in the last {days_back} days.[/]\n"
            f"Try [cyan]--days 180[/] to widen the search window."
        )
        raise typer.Exit(1)

    console.print(f"Found [bold]{len(obs_table)}[/] observation(s).\n")

    # 2. Build the download plan (no downloading yet)
    plan = mast.build_download_plan(obs_table, dest_dir=dest)

    if len(plan.products) == 0:
        console.print("[red]No downloadable science products found.[/]")
        raise typer.Exit(1)

    # 3. Display summary table
    # Build a filter lookup from obs_table (covers cases where the filename
    # doesn't embed the filter name, e.g. per-detector _cal.fits files)
    obs_filter_lookup: dict[str, str] = {}
    if "obs_id" in obs_table.colnames and "filters" in obs_table.colnames:
        for row in obs_table:
            obs_filter_lookup[str(row["obs_id"])] = str(row["filters"])

    tbl = RichTable(show_header=True, header_style="bold", show_lines=False,
                    box=None, padding=(0, 1))
    tbl.add_column("Observation / file", style="cyan", no_wrap=True)
    tbl.add_column("Filter", style="yellow")
    tbl.add_column("Size", justify="right")
    tbl.add_column("Status")

    # Group products by parent obs for cleaner display
    shown_obs: set[str] = set()
    new_bytes = 0
    for row in plan.products:
        fname  = str(row["productFilename"])
        filt   = _filter_from_filename(fname)
        # Fallback: look up from obs table via parent_obsid → obs_id
        if filt == "—":
            parent = str(row.get("parent_obsid", row.get("obsID", "")))
            filt = obs_filter_lookup.get(parent, "—")
            # Clean up multi-filter strings like "F444W;WLM8" → "F444W"
            if ";" in filt:
                filt = filt.split(";")[0]
        cached = fname in plan.already_cached
        size_bytes = 0
        try:
            size_bytes = int(row["size"]) if "size" in plan.products.colnames else 0
        except (TypeError, ValueError):
            pass

        status = Text("cached", style="dim") if cached else Text("to download", style="green")
        size_str = _fmt_bytes(size_bytes) if size_bytes else "?"
        if not cached:
            new_bytes += size_bytes
        tbl.add_row(fname, filt, size_str, status)

    eta_str = _fmt_seconds(plan.eta_seconds) if plan.total_bytes else "?"
    speed_str = f"{plan.mbps_estimate:.0f} MB/s"

    summary_lines = [
        f"[bold]{len(plan.new_files)}[/] file(s) to download"
        + (f"  ·  [bold]{_fmt_bytes(plan.total_bytes)}[/]" if plan.total_bytes else ""),
    ]
    if plan.total_bytes and plan.mbps_estimate:
        summary_lines.append(
            f"Estimated time: [bold]~{eta_str}[/]  [dim](measured ~{speed_str})[/]"
        )
    if plan.already_cached:
        summary_lines.append(
            f"[dim]{len(plan.already_cached)} file(s) already cached — will be skipped[/]"
        )
    if rgb:
        summary_lines.append(
            "[yellow]⬡ RGB mode:[/] will composite ≥3 filters into a false-colour image"
        )

    console.print(tbl)
    console.print()
    console.print(Panel("\n".join(summary_lines), title="Download summary", border_style="cyan"))
    console.print()

    # 4. Confirm
    if not yes and len(plan.new_files) > 0:
        confirmed = typer.confirm("Proceed with download?", default=False)
        if not confirmed:
            console.print("[dim]Aborted.[/]")
            raise typer.Exit(0)
    elif len(plan.new_files) == 0:
        console.print("[dim]All files already cached — skipping download.[/]")

    # 5. Download
    if plan.new_files:
        paths = mast.download_products(plan.products, dest_dir=dest)
    else:
        paths = [dest / str(r["productFilename"]) for r in plan.products]

    if not paths:
        console.print("[red]Download failed — nothing to render.[/]")
        raise typer.Exit(1)

    for p in paths:
        entry = cache.entry_from_fits(p, instrument=instrument)
        cache.add_entry(entry)

    # 6. Render
    out_path: Optional[Path] = None

    if rgb:
        out_path = _try_rgb_composite(paths, loaded_cfg)

    if out_path is None:
        # Single-band: render the first (largest) file
        paths_sorted = sorted(paths, key=lambda p: p.stat().st_size, reverse=True)
        fits_path = paths_sorted[0]
        console.print(f"\nRendering [cyan]{fits_path.name}[/]…")
        out_path = renderer.render_single(fits_path, loaded_cfg)
        console.print(f"[green]Wallpaper saved:[/] {out_path}")

    # 7. Set
    if not no_set and out_path:
        try:
            wallpaper.set_wallpaper(out_path)
            console.print("[green]✓ Wallpaper set.[/]")
        except (wallpaper.WallpaperError, FileNotFoundError) as exc:
            console.print(f"[yellow]Warning — could not set wallpaper:[/] {exc}")


def _try_rgb_composite(paths: list[Path], loaded_cfg) -> Optional[Path]:
    """Attempt to find three filter files among *paths* and composite them.

    Uses a simple heuristic: long-wavelength filter → red, mid → green,
    shortest → blue.  Returns None if fewer than 3 distinct filters are found.
    """
    import re

    filter_re = re.compile(r"[_-](f\d{3}[mwn])[_-]", re.IGNORECASE)

    # Group files by filter, keep the largest file per filter
    by_filter: dict[str, Path] = {}
    for p in paths:
        m = filter_re.search(p.name)
        if m:
            filt = m.group(1).upper()
            if filt not in by_filter or p.stat().st_size > by_filter[filt].stat().st_size:
                by_filter[filt] = p

    if len(by_filter) < 3:
        console.print(
            f"[yellow]RGB mode:[/] only {len(by_filter)} filter(s) found "
            f"({', '.join(sorted(by_filter))}). Need ≥3. Falling back to single-band."
        )
        return None

    # Sort filters by wavelength — JWST filter names encode wavelength in nm
    # F090W → 900 nm, F277W → 2770 nm, F444W → 4440 nm
    def _wave(name: str) -> int:
        m = re.match(r"F(\d+)[MWN]", name, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    sorted_filters = sorted(by_filter.keys(), key=_wave)
    # Assign: shortest → blue, middle → green, longest → red
    blue_filt  = sorted_filters[0]
    green_filt = sorted_filters[len(sorted_filters) // 2]
    red_filt   = sorted_filters[-1]

    console.print(
        f"\n[yellow]⬡ RGB composite[/]: "
        f"[red]{red_filt}[/]=R  [green]{green_filt}[/]=G  [blue]{blue_filt}[/]=B"
    )

    try:
        out = renderer.render_rgb(
            by_filter[red_filt],
            by_filter[green_filt],
            by_filter[blue_filt],
            loaded_cfg,
        )
        console.print(f"[green]RGB wallpaper saved:[/] {out}")
        return out
    except Exception as exc:
        console.print(f"[yellow]RGB render failed ({exc}) — falling back to single-band.[/]")
        return None


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

@app.command()
def fetch(
    target: str = typer.Argument(..., help="Target name or object (e.g. 'Carina Nebula')."),
    instrument: str = typer.Option("NIRCam", "-i", "--instrument",
                                   help="JWST instrument: NIRCam, MIRI, NIRSpec, NIRISS."),
    max_obs: int = typer.Option(10, "-n", "--max-obs",
                                help="Max observations to query."),
    filters: Optional[list[str]] = typer.Option(None, "-f", "--filter",
                                                  help="Only download files matching these filter names (repeatable)."),
    rgb: bool = typer.Option(False, "--rgb",
                             help="Fetch a 3-filter set and immediately render a full-colour Lupton RGB wallpaper."),
    no_set: bool = typer.Option(False, "--no-set", help="Skip setting the wallpaper (only with --rgb)."),
) -> None:
    """Download FITS images for TARGET from the MAST archive.

    With [bold]--rgb[/], automatically selects the best 3-filter combination,
    downloads one mosaic per channel, composites them with the Lupton algorithm,
    and sets the result as your wallpaper.
    """
    loaded_cfg = cfg_module.load()
    dest = cfg_module.fits_dir()

    # ── RGB shortcut ──────────────────────────────────────────────────────────
    if rgb:
        result = mast.fetch_rgb_set(
            target=target,
            dest_dir=dest,
            instrument=instrument,
            max_observations=max_obs,
        )
        if result is None:
            console.print("[red]Could not assemble a 3-filter RGB set.[/]")
            raise typer.Exit(1)

        red_path, green_path, blue_path = result
        for p in (red_path, green_path, blue_path):
            entry = cache.entry_from_fits(p, target=target, instrument=instrument)
            cache.add_entry(entry)

        console.print("\nRendering Lupton RGB composite…")
        out = renderer.render_lupton_rgb(red_path, green_path, blue_path, loaded_cfg)
        console.print(f"[green]RGB wallpaper saved:[/] {out}")

        if not no_set:
            try:
                wallpaper.set_wallpaper(out)
                console.print("[green]✓ Wallpaper set.[/]")
            except (wallpaper.WallpaperError, FileNotFoundError) as exc:
                console.print(f"[yellow]Warning — could not set wallpaper:[/] {exc}")
        return

    # ── Single-band / multi-file fetch ────────────────────────────────────────
    paths = mast.fetch(
        target=target,
        dest_dir=dest,
        instrument=instrument,
        max_observations=max_obs,
        filters=list(filters) if filters else None,
    )
    if not paths:
        console.print("[red]Nothing downloaded.[/]")
        raise typer.Exit(1)

    console.print(f"\n[green]Downloaded {len(paths)} file(s)[/] to [dim]{dest}[/]")
    for p in paths:
        entry = cache.entry_from_fits(p, target=target, instrument=instrument)
        cache.add_entry(entry)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@app.command()
def render(
    fits_file: Optional[Path] = typer.Argument(
        None, help="Path to a specific FITS file. Defaults to most-recent cached file.",
    ),
    colormap: Optional[str] = typer.Option(None, "-c", "--colormap",
                                            help="Matplotlib colormap name."),
    stretch: Optional[str] = typer.Option(None, "-s", "--stretch",
                                           help="Stretch: asinh | log | sqrt | linear | histeq"),
    width: Optional[int] = typer.Option(None, "-W", "--width", help="Output width in pixels."),
    height: Optional[int] = typer.Option(None, "-H", "--height", help="Output height in pixels."),
    rgb: bool = typer.Option(False, "--rgb", help="Composite RGB from three filter FITS files."),
    red: Optional[Path] = typer.Option(None, "--red", help="FITS file for red channel (RGB mode)."),
    green: Optional[Path] = typer.Option(None, "--green", help="FITS file for green channel."),
    blue: Optional[Path] = typer.Option(None, "--blue", help="FITS file for blue channel."),
    lupton_q: Optional[float] = typer.Option(None, "--q",
                                              help="Lupton Q softening (default 8; lower = richer colour)."),
    lupton_stretch: Optional[float] = typer.Option(None, "--lupton-stretch",
                                                    help="Lupton stretch scale (default 0.4; lower = more faint detail)."),
    saturation: Optional[float] = typer.Option(None, "--saturation",
                                               help="Colour saturation boost after Lupton (default 1.8; 1.0 = off)."),
    r_gain: Optional[float] = typer.Option(None, "--r-gain",
                                           help="Red channel gain after normalisation (default 0.85)."),
    g_gain: Optional[float] = typer.Option(None, "--g-gain",
                                           help="Green channel gain after normalisation (default 1.0)."),
    b_gain: Optional[float] = typer.Option(None, "--b-gain",
                                           help="Blue channel gain after normalisation (default 1.3)."),
) -> None:
    """Render a FITS file into a desktop wallpaper PNG."""
    loaded_cfg = cfg_module.load()

    # Apply CLI overrides
    if colormap:
        loaded_cfg.colormap = colormap  # type: ignore[assignment]
    if stretch:
        loaded_cfg.stretch = stretch  # type: ignore[assignment]
    if width:
        loaded_cfg.width = width
    if height:
        loaded_cfg.height = height

    if rgb:
        if not (red and green and blue):
            console.print(
                "[red]--rgb requires --red, --green, and --blue FITS files.[/]\n"
                "[dim]Tip: use [cyan]fetch --rgb <target>[/] to download & render in one step.[/]"
            )
            raise typer.Exit(1)
        console.print("Rendering Lupton RGB composite…")
        rgb_kwargs: dict = {}
        if lupton_q is not None:
            rgb_kwargs["Q"] = lupton_q
        if lupton_stretch is not None:
            rgb_kwargs["stretch"] = lupton_stretch
        if saturation is not None:
            rgb_kwargs["saturation"] = saturation
        if r_gain is not None:
            rgb_kwargs["r_gain"] = r_gain
        if g_gain is not None:
            rgb_kwargs["g_gain"] = g_gain
        if b_gain is not None:
            rgb_kwargs["b_gain"] = b_gain
        out = renderer.render_lupton_rgb(red, green, blue, loaded_cfg, **rgb_kwargs)
        console.print(f"[green]RGB wallpaper rendered:[/] {out}")
        return

    # Single-band
    if fits_file is None:
        entries = cache.get_all()
        if not entries:
            console.print("[red]No cached FITS files found. Run `fetch` first.[/]")
            raise typer.Exit(1)
        fits_file = entries[-1].fits_file
        console.print(f"Using most recent: [dim]{fits_file.name}[/]")

    console.print(f"Rendering [cyan]{fits_file.name}[/] with colormap=[yellow]{loaded_cfg.colormap}[/] "
                  f"stretch=[yellow]{loaded_cfg.stretch}[/]…")
    out = renderer.render_single(fits_file, loaded_cfg)
    console.print(f"[green]Wallpaper saved:[/] {out}")


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

@app.command(name="set")
def set_cmd(
    wallpaper_file: Optional[Path] = typer.Argument(
        None, help="Path to a PNG/JPEG. Defaults to most recently rendered wallpaper.",
    ),
) -> None:
    """Set a PNG image as the desktop background."""
    if wallpaper_file is None:
        rendered = cache.get_rendered()
        if not rendered:
            console.print("[red]No rendered wallpapers. Run `render` first.[/]")
            raise typer.Exit(1)
        wallpaper_file = rendered[-1].wallpaper_file  # type: ignore[assignment]
        assert wallpaper_file is not None
        console.print(f"Using most recent: [dim]{wallpaper_file.name}[/]")

    console.print(f"Setting wallpaper → [cyan]{wallpaper_file}[/]")
    try:
        wallpaper.set_wallpaper(wallpaper_file)
        console.print("[green]✓ Wallpaper set.[/]")
    except (wallpaper.WallpaperError, FileNotFoundError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# run (all-in-one)
# ---------------------------------------------------------------------------

@app.command()
def run(
    target: str = typer.Argument(..., help="Target name (e.g. 'Pillars of Creation')."),
    instrument: str = typer.Option("NIRCam", "-i", "--instrument"),
    colormap: Optional[str] = typer.Option(None, "-c", "--colormap"),
    stretch: Optional[str] = typer.Option(None, "-s", "--stretch"),
    no_set: bool = typer.Option(False, "--no-set", help="Skip setting the wallpaper."),
) -> None:
    """Fetch from MAST, render, and set as wallpaper in one step."""
    loaded_cfg = cfg_module.load()
    if colormap:
        loaded_cfg.colormap = colormap  # type: ignore[assignment]
    if stretch:
        loaded_cfg.stretch = stretch  # type: ignore[assignment]

    # 1. Fetch
    paths = mast.fetch(
        target=target,
        dest_dir=cfg_module.fits_dir(),
        instrument=instrument,
        max_observations=loaded_cfg.max_results,
    )
    if not paths:
        console.print("[red]Nothing downloaded. Aborting.[/]")
        raise typer.Exit(1)

    # 2. Render the first one
    fits_path = paths[0]
    entry = cache.entry_from_fits(fits_path, target=target, instrument=instrument)
    cache.add_entry(entry)

    console.print(f"Rendering [cyan]{fits_path.name}[/]…")
    out = renderer.render_single(fits_path, loaded_cfg)
    console.print(f"[green]Wallpaper rendered:[/] {out}")

    cache.mark_rendered(entry.obs_id, entry.filter_name, out.name)
    cache.purge_oldest_wallpapers(loaded_cfg.max_wallpapers)

    # 3. Set
    if not no_set:
        try:
            wallpaper.set_wallpaper(out)
            console.print("[green]✓ Wallpaper set.[/]")
        except (wallpaper.WallpaperError, FileNotFoundError) as exc:
            console.print(f"[yellow]Warning — could not set wallpaper:[/] {exc}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@app.command(name="list")
def list_cmd(
    rendered_only: bool = typer.Option(False, "--rendered", help="Show only rendered entries."),
) -> None:
    """List cached FITS files and rendered wallpapers."""
    entries = cache.get_rendered() if rendered_only else cache.get_all()
    if not entries:
        console.print("[dim]Cache is empty.[/]")
        return

    table = RichTable(title="JWST Wallpaper Cache", show_lines=True)
    table.add_column("Target", style="cyan")
    table.add_column("Instrument", style="yellow")
    table.add_column("Filter")
    table.add_column("FITS")
    table.add_column("Wallpaper")
    table.add_column("Fetched")

    for e in entries:
        table.add_row(
            e.target or "—",
            e.instrument or "—",
            e.filter_name or "—",
            e.fits_path,
            e.wallpaper_path or "[dim]not rendered[/]",
            e.fetched_at[:10] if e.fetched_at else "—",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="View or modify configuration.", add_completion=False)
app.add_typer(config_app, name="config")


@config_app.command(name="show")
def config_show() -> None:
    """Print the current configuration."""
    import dataclasses
    c = cfg_module.load()
    table = RichTable(title="jwst-wallpaper configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    for field in dataclasses.fields(c):
        table.add_row(field.name, str(getattr(c, field.name)))
    console.print(table)


@config_app.command(name="set")
def config_set(
    key: str = typer.Argument(..., help="Config key (e.g. colormap)."),
    value: str = typer.Argument(..., help="New value."),
) -> None:
    """Set a configuration value."""
    import dataclasses
    c = cfg_module.load()
    fields = {f.name: f for f in dataclasses.fields(c)}
    if key not in fields:
        console.print(f"[red]Unknown setting:[/] {key}")
        console.print(f"Valid settings: {', '.join(fields.keys())}")
        raise typer.Exit(1)
    f = fields[key]
    try:
        if f.type in ("int", "float"):
            setattr(c, key, f.type.__class__(value))
        elif f.type == "bool":
            setattr(c, key, value.lower() in ("true", "1", "yes"))
        else:
            setattr(c, key, value)
    except (ValueError, TypeError) as exc:
        console.print(f"[red]Invalid value:[/] {exc}")
        raise typer.Exit(1)
    cfg_module.save(c)
    console.print(f"[green]Set[/] {key} = {value}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
