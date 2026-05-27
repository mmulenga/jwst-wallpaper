"""CLI entry point for jwst-wallpaper.

Commands
--------
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
from rich.table import Table as RichTable

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
# fetch
# ---------------------------------------------------------------------------

@app.command()
def fetch(
    target: str = typer.Argument(..., help="Target name or object (e.g. 'Carina Nebula')."),
    instrument: str = typer.Option("NIRCam", "-i", "--instrument",
                                   help="JWST instrument: NIRCam, MIRI, NIRSpec, NIRISS."),
    max_obs: int = typer.Option(5, "-n", "--max-obs",
                                help="Max observations to query."),
    filters: Optional[list[str]] = typer.Option(None, "-f", "--filter",
                                                  help="Only download files matching these filter names (repeatable)."),
) -> None:
    """Download FITS images for TARGET from the MAST archive."""
    loaded_cfg = cfg_module.load()
    dest = cfg_module.fits_dir()

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
            console.print("[red]--rgb requires --red, --green, and --blue FITS files.[/]")
            raise typer.Exit(1)
        out = renderer.render_rgb(red, green, blue, loaded_cfg)
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
