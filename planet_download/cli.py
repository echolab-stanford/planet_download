"""Command line interface for Planet Download (basemaps + PlanetScope scenes)."""

import os

import click
import geopandas as gpd
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from shapely.geometry import mapping
from tqdm import tqdm

from planet_download.client import BasemapsClient
from planet_download.composites import (
    merge_composites_by_date,
    parse_planet_filesystem,
    process_composites_parallel,
    process_scene_composites,
    resample_resolution_parallel,
)
from planet_download.scenes import (
    estimate_billed_area,
    order_scenes,
    search_scenes,
    select_scenes,
)

# Load environment variables at module level. find_dotenv(usecwd=True) resolves
# the .env relative to the working directory, so the CLI picks up PL_API_KEY
# regardless of where the installed entry point is invoked from.
load_dotenv(find_dotenv(usecwd=True))


@click.group()
def cli():
    """Planet Download CLI - basemap quads, PlanetScope scenes, and composites."""
    pass


@cli.command(
    "download-all",
    help="Download all images for a GeoJSON AOI from a Planet basemap series. Example:\n  planet-download download-all --geojson kenya.geojson --start-date 2020-01-01 --end-date 2020-12-31 --cadence 'Global Quarterly'",
)
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to GeoJSON file defining the AOI.",
)
@click.option("--start-date", required=True, type=str, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", required=True, type=str, help="End date (YYYY-MM-DD)")
@click.option(
    "--cadence",
    default="Global Quarterly",
    show_default=True,
    help="Basemap series cadence (e.g. 'Global Quarterly')",
)
def download_all(geojson, start_date, end_date, cadence):
    """Download all images for a GeoJSON AOI using BasemapsClient and MosaicSeries."""
    # Start the client (dotenv is loaded at module level)
    client = BasemapsClient()

    # Load the geojson and get bounds
    gdf = gpd.read_file(geojson)
    gdf_bounds = gdf.total_bounds

    # Get the series
    series = client.series(name=cadence)

    # Download all images in the AOI and date range
    downloads = series.download_quads(
        bbox=gdf_bounds, start_date=start_date, end_date=end_date
    )

    # Trigger the downloads. `downloads` is a generator, so its length is not
    # known up front; tqdm will simply show a count without a progress bar.
    click.echo("Starting downloads...")
    for d in tqdm(downloads, desc="Downloading files"):
        pass


def _load_aoi_region(
    geojson: str, filter_col: str | None, filter_val: str | None
) -> dict:
    """Load a GeoJSON AOI and return a single WGS84 GeoJSON geometry (region).

    Optionally filter to the feature(s) whose ``filter_col`` equals ``filter_val``
    (e.g. a Siaya county polygon in ``kenya.geojson``). All selected features are
    dissolved into one (multi)polygon suitable for server-side quad filtering.
    """
    gdf = gpd.read_file(geojson)

    if filter_col is not None and filter_val is not None:
        matched = gdf[gdf[filter_col].astype(str).str.upper() == filter_val.upper()]
        if matched.empty:
            raise click.ClickException(
                f"No features in {geojson} where {filter_col} == {filter_val!r}. "
                f"Available: {sorted(gdf[filter_col].dropna().unique().tolist())[:20]}"
            )
        gdf = matched

    # Dissolve to a single geometry and express it in WGS84 for the Basemaps API.
    region_geom = gdf.to_crs("EPSG:4326").union_all()
    return mapping(region_geom)


@cli.command()
@click.option(
    "--labels",
    required=True,
    type=click.Path(exists=True),
    help="Path to the CSV file with label points.",
)
@click.option(
    "--save-dir",
    required=True,
    type=click.Path(),
    help="Directory to save the downloaded images.",
)
@click.option(
    "--cadence",
    default="Global Quarterly",
    show_default=True,
    help="Basemap series cadence (e.g. 'Global Quarterly', 'Global Monthly').",
)
@click.option(
    "--lat",
    default="lat",
    show_default=True,
    help="Name of the latitude column in the labels file.",
)
@click.option(
    "--lon",
    default="lon",
    show_default=True,
    help="Name of the longitude column in the labels file.",
)
@click.option(
    "--nthreads",
    default=10,
    show_default=True,
    type=int,
    help="Number of threads for downloading.",
)
@click.option(
    "--buffer-size",
    default=5000,
    show_default=True,
    type=float,
    help="Buffer size in meters for each label point.",
)
def download(
    labels: str,
    save_dir: str,
    cadence: str,
    lat: str,
    lon: str,
    nthreads: int,
    buffer_size: int,
) -> None:
    """
    Download Planet Basemap quads for buffered label points using optimized spatial filtering.

    This command will:
    - Buffer each label point in the CSV by the specified buffer size
    - Create a spatial union of buffered points for each year
    - Use server-side spatial filtering to find only relevant quads
    - Download the selected quads to the specified directory
    - Process each year in the labels file separately if a 'year' column is present
    """
    # Create save directory if it doesn't exist
    os.makedirs(save_dir, exist_ok=True)

    # Start the client
    client = BasemapsClient()

    series = client.series(name=cadence)

    # Load labels
    labels_aoi = pd.read_csv(labels)

    # Transform labels to GeoDataFrame
    labels_aoi = gpd.GeoDataFrame(
        labels_aoi,
        geometry=gpd.points_from_xy(x=labels_aoi[lon], y=labels_aoi[lat]),
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")

    # Buffer labels to create a 5000m radius around each point
    labels_aoi["geometry"] = labels_aoi.geometry.buffer(buffer_size, cap_style=3)

    # Take back to degrees for plotting
    labels_aoi = labels_aoi.to_crs("EPSG:4326")

    if "year" in labels_aoi.columns:
        for year in labels_aoi["year"].unique():
            print(f"Processing year: {year}")
            start_date = f"{year}-01-01"
            end_date = f"{year}-12-31"

            # Get labels for this specific year
            year_labels = labels_aoi[labels_aoi["year"] == year]

            # Create spatial union of buffered labels for this year
            year_union = year_labels.union_all()
            region_dict = mapping(year_union)

            print(f"Fetching quads for {len(year_labels)} labels in {year}...")

            # Use region-based filtering instead of bbox - much more efficient!
            quads, df = series.all_quads_overlap(
                start_date=start_date,
                end_date=end_date,
                region=region_dict,  # Server-side spatial filtering
            )

            print(f"Found {len(quads)} relevant quads")

            # No need for spatial join anymore - server already did the filtering!
            # But we still need to associate quads with labels for crosswalk
            overlap = gpd.sjoin(
                df,
                year_labels,
                how="inner",
                predicate="intersects",
            )

            # Droping duplicates between id and mosaic names (year-quarter). We can have multiple
            # quads per label, so we keep those too.
            idx = overlap.drop_duplicates(subset=["id", "mosaic_name"]).index.tolist()
            sel_quads = [quads[i] for i in idx]

            # Save crosswalk file
            overlap.to_file(
                os.path.join(save_dir, f"crosswalk_quads_planet_{year}.geojson"),
                driver="GeoJSON",
            )

            print(f"Downloading {len(sel_quads)} quads for {year}...")
            downloads = series.download_selection(
                sel_quads,
                flat=False,
                save_dir=save_dir,
                filename_template="{x}_{y}.tif",
                nthreads=nthreads,
            )
            for download in tqdm(
                downloads, desc=f"Downloading {year} files...", total=len(sel_quads)
            ):
                pass


@cli.command()
@click.option(
    "--data-dir",
    required=True,
    type=click.Path(exists=True),
    help="Path to the directory containing downloaded Planet quads.",
)
@click.option(
    "--output-dir",
    type=click.Path(),
    help="Directory to save composite images (defaults to data-dir).",
)
@click.option(
    "--operation",
    type=click.Choice(["mean", "median"], case_sensitive=False),
    default="median",
    show_default=True,
    help="Operation to create composites (mean or median).",
)
@click.option(
    "--nthreads",
    default=10,
    show_default=True,
    type=int,
    help="Number of threads for parallel composite processing.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite existing composite files.",
)
@click.option(
    "--only-rgb/--all-bands",
    default=True,
    show_default=True,
    help="Keep only the RGB bands (drop the alpha/coverage band).",
)
@click.option(
    "--dtype",
    type=click.Choice(["uint8", "float64"], case_sensitive=False),
    default="uint8",
    show_default=True,
    help="Output dtype. 'uint8' rounds/clips RGB basemaps to [0, 255] for compact storage.",
)
def composite(
    data_dir: str,
    output_dir: str | None,
    operation: str,
    nthreads: int,
    overwrite: bool,
    only_rgb: bool,
    dtype: str,
) -> None:
    """
    Create composite images from downloaded Planet basemap quads.

    This command will:
    - Parse the Planet filesystem structure to identify quads by year
    - Create mean or median composites for each unique quad across time periods
    - Save composites in {output-dir}/{year}/{quad_name}_composite.tif format
    """
    # Use data_dir as output_dir if not specified
    if output_dir is None:
        output_dir = data_dir

    cast = None if dtype.lower() == "float64" else dtype.lower()

    print(f"Parsing Planet filesystem in: {data_dir}")
    dict_paths = parse_planet_filesystem(data_dir)

    print(
        f"Creating {operation} composites ({'RGB' if only_rgb else 'all bands'}, {dtype})..."
    )
    print(f"Output directory: {output_dir}")

    process_composites_parallel(
        dict_paths=dict_paths,
        save_dir=output_dir,
        operation=operation.lower(),
        overwrite=overwrite,
        nthreads=nthreads,
        only_rgb=only_rgb,
        dtype=cast,
    )

    print("Composite creation complete!")


def _download_aoi(
    client: BasemapsClient,
    cadence: str,
    region: dict,
    save_dir: str,
    start_year: int,
    end_year: int,
    nthreads: int,
) -> None:
    """Download every quad overlapping ``region`` for each year in the range.

    Quads land in ``{save_dir}/{mosaic_name}/{x}_{y}.tif`` — the exact layout
    ``parse_planet_filesystem`` expects for compositing.
    """
    os.makedirs(save_dir, exist_ok=True)
    series = client.series(name=cadence)

    for year in range(start_year, end_year + 1):
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"
        print(f"Fetching quads overlapping the AOI for {year}...")

        quads, _ = series.all_quads_overlap(
            start_date=start_date, end_date=end_date, region=region
        )
        print(f"  found {len(quads)} quads for {year}")

        if not quads:
            continue

        downloads = series.download_selection(
            quads,
            flat=False,
            save_dir=save_dir,
            filename_template="{x}_{y}.tif",
            nthreads=nthreads,
        )
        for _ in tqdm(downloads, desc=f"Downloading {year} quads", total=len(quads)):
            pass


@cli.command("download-aoi")
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to a GeoJSON polygon AOI (e.g. examples/kenya.geojson).",
)
@click.option(
    "--save-dir",
    required=True,
    type=click.Path(),
    help="Directory to save the downloaded quads.",
)
@click.option("--start-year", required=True, type=int, help="First year to download.")
@click.option(
    "--end-year", required=True, type=int, help="Last year to download (inclusive)."
)
@click.option(
    "--cadence",
    default="Global Quarterly",
    show_default=True,
    help="Basemap series cadence.",
)
@click.option(
    "--filter-col",
    default=None,
    help="Attribute column to filter the AOI by (e.g. COUNTY_NAM).",
)
@click.option(
    "--filter-val",
    default=None,
    help="Value to match in --filter-col (e.g. SIAYA).",
)
@click.option("--nthreads", default=10, show_default=True, type=int)
def download_aoi(
    geojson: str,
    save_dir: str,
    start_year: int,
    end_year: int,
    cadence: str,
    filter_col: str | None,
    filter_val: str | None,
    nthreads: int,
) -> None:
    """Download basemap quads overlapping a GeoJSON polygon AOI over a year range.

    Unlike `download` (which buffers CSV label points), this filters quads by an
    arbitrary polygon region, e.g. the SIAYA county feature in kenya.geojson.
    """
    client = BasemapsClient()
    region = _load_aoi_region(geojson, filter_col, filter_val)
    _download_aoi(client, cadence, region, save_dir, start_year, end_year, nthreads)


@cli.command()
@click.option(
    "--data-dir",
    required=True,
    type=click.Path(exists=True),
    help="Directory of composites (with per-year subfolders) to resample.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(),
    help="Directory to write the resampled composites.",
)
@click.option(
    "--target-resolution",
    default=5.0,
    show_default=True,
    type=float,
    help="Target pixel size in the raster CRS units (meters for EPSG:3857).",
)
@click.option("--nthreads", default=10, show_default=True, type=int)
def resample(
    data_dir: str, output_dir: str, target_resolution: float, nthreads: int
) -> None:
    """Resample composites to a target resolution, preserving the year subfolders."""
    print(f"Resampling {data_dir} -> {output_dir} at {target_resolution} m")
    resample_resolution_parallel(
        path_rasters=data_dir,
        path_save_rasters=output_dir,
        target_resolution=target_resolution,
        nthreads=nthreads,
    )
    print("Resampling complete!")


@cli.command()
@click.option(
    "--data-dir",
    required=True,
    type=click.Path(exists=True),
    help="Directory with per-year composite subfolders to merge.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(),
    help="Directory to write the merged mosaics.",
)
@click.option(
    "--out-prefix",
    default="planet_composite_mosaic",
    show_default=True,
    help="Filename prefix for the merged mosaic (output is <prefix>_<year>.tif).",
)
def merge(data_dir: str, output_dir: str, out_prefix: str) -> None:
    """Merge each year's composites into a single mosaic (tiled + DEFLATE)."""
    print(f"Merging composites in {data_dir} -> {output_dir}")
    merge_composites_by_date(
        path_to_composites=data_dir, save_path=output_dir, out_prefix=out_prefix
    )
    print("Merge complete!")


@cli.command("grid-mosaic")
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to a GeoJSON polygon AOI (e.g. examples/kenya.geojson).",
)
@click.option(
    "--work-dir",
    required=True,
    type=click.Path(),
    help="Base working directory for intermediate quads/composites.",
)
@click.option(
    "--mosaic-dir",
    required=True,
    type=click.Path(),
    help="Directory to write the final per-year mosaics.",
)
@click.option("--start-year", required=True, type=int)
@click.option("--end-year", required=True, type=int)
@click.option("--cadence", default="Global Quarterly", show_default=True)
@click.option("--filter-col", default=None, help="AOI attribute column to filter on.")
@click.option("--filter-val", default=None, help="Value to match in --filter-col.")
@click.option(
    "--operation",
    type=click.Choice(["mean", "median"], case_sensitive=False),
    default="median",
    show_default=True,
)
@click.option("--target-resolution", default=5.0, show_default=True, type=float)
@click.option(
    "--dtype",
    type=click.Choice(["uint8", "float64"], case_sensitive=False),
    default="uint8",
    show_default=True,
    help="Composite/mosaic output dtype.",
)
@click.option(
    "--only-rgb/--all-bands",
    default=True,
    show_default=True,
    help="Keep only RGB bands (drop the alpha/coverage band).",
)
@click.option("--nthreads", default=10, show_default=True, type=int)
@click.option(
    "--skip-download",
    is_flag=True,
    default=False,
    help="Reuse quads already present under <work-dir>/quads.",
)
@click.option("--overwrite", is_flag=True, default=False)
def grid_mosaic(
    geojson: str,
    work_dir: str,
    mosaic_dir: str,
    start_year: int,
    end_year: int,
    cadence: str,
    filter_col: str | None,
    filter_val: str | None,
    operation: str,
    target_resolution: float,
    dtype: str,
    only_rgb: bool,
    nthreads: int,
    skip_download: bool,
    overwrite: bool,
) -> None:
    """Run the full AOI pipeline: download -> composite -> resample -> merge.

    Reproduces the create_composites_grid notebook end-to-end, producing one
    <mosaic-dir>/planet_composite_mosaic_<year>.tif per year at the target
    resolution (RGB uint8 by default).
    """
    work = os.path.abspath(work_dir)
    quads_dir = os.path.join(work, "quads")
    composites_dir = os.path.join(work, "composites")
    resampled_dir = os.path.join(work, "composites_resampled")
    cast = None if dtype.lower() == "float64" else dtype.lower()

    region = _load_aoi_region(geojson, filter_col, filter_val)

    # 1. Download quads overlapping the AOI.
    if not skip_download:
        print("== Step 1/4: download quads ==")
        _download_aoi(
            BasemapsClient(),
            cadence,
            region,
            quads_dir,
            start_year,
            end_year,
            nthreads,
        )
    else:
        print("== Step 1/4: skipped (reusing existing quads) ==")

    # 2. Temporal composite per quad per year.
    print(
        f"== Step 2/4: {operation} composites ({'RGB' if only_rgb else 'all bands'}, {dtype}) =="
    )
    dict_paths = parse_planet_filesystem(quads_dir)
    process_composites_parallel(
        dict_paths=dict_paths,
        save_dir=composites_dir,
        operation=operation.lower(),
        overwrite=overwrite,
        nthreads=nthreads,
        only_rgb=only_rgb,
        dtype=cast,
    )

    # 3. Resample composites to target resolution.
    print(f"== Step 3/4: resample to {target_resolution} m ==")
    resample_resolution_parallel(
        path_rasters=composites_dir,
        path_save_rasters=resampled_dir,
        target_resolution=target_resolution,
        nthreads=nthreads,
    )

    # 4. Merge each year's resampled composites into a single mosaic.
    print("== Step 4/4: merge into per-year mosaics ==")
    merge_composites_by_date(path_to_composites=resampled_dir, save_path=mosaic_dir)

    print(f"Done. Mosaics written to {mosaic_dir}")


def _search_and_report(
    region: dict,
    start_year: int,
    end_year: int,
    max_cloud: float,
    min_clear: float,
    per_month: int,
):
    """Search PSScene items, apply the per-month cap, and print the quota cost.

    Shared by ps-search (dry run) and ps-download so both always report the
    scene counts and the estimated billed area before any quota is spent.
    """
    print(f"Searching PSScene {start_year}-{end_year} over the AOI...")
    scenes = search_scenes(
        region, start_year, end_year, max_cloud=max_cloud, min_clear=min_clear
    )
    selected = select_scenes(scenes, per_month=per_month)

    print(
        f"Found {len(scenes)} scenes; selected {len(selected)} "
        f"(top {per_month or 'all'} clearest per month)."
    )
    if not selected.empty:
        counts = selected.groupby("year")["id"].count()
        for year, n in counts.items():
            print(f"  {year}: {n} scenes")
        print(
            f"Estimated billed area: {estimate_billed_area(selected, region):,.0f} km²"
        )

    return selected


@cli.command("ps-search")
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to a GeoJSON polygon AOI.",
)
@click.option("--start-year", required=True, type=int)
@click.option("--end-year", required=True, type=int)
@click.option("--max-cloud", default=0.2, show_default=True, type=float)
@click.option("--min-clear", default=90.0, show_default=True, type=float)
@click.option(
    "--per-month",
    default=3,
    show_default=True,
    type=int,
    help="Keep only the K clearest scenes per month (0 = keep all).",
)
@click.option("--filter-col", default=None, help="AOI attribute column to filter on.")
@click.option("--filter-val", default=None, help="Value to match in --filter-col.")
@click.option(
    "--save-footprints",
    default=None,
    type=click.Path(),
    help="Optional path to save the selected scene footprints as GeoJSON.",
)
def ps_search(
    geojson: str,
    start_year: int,
    end_year: int,
    max_cloud: float,
    min_clear: float,
    per_month: int,
    filter_col: str | None,
    filter_val: str | None,
    save_footprints: str | None,
) -> None:
    """Dry run: search PlanetScope scenes and preview the quota cost.

    Searching is free — no quota is consumed until `ps-download` places an
    order. Quota is billed per clipped scene, so use this to check the scene
    counts and billed km² before downloading.
    """
    region = _load_aoi_region(geojson, filter_col, filter_val)
    selected = _search_and_report(
        region, start_year, end_year, max_cloud, min_clear, per_month
    )

    if save_footprints and not selected.empty:
        selected.to_file(save_footprints, driver="GeoJSON")
        print(f"Footprints saved to {save_footprints}")


@cli.command("ps-download")
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to a GeoJSON polygon AOI.",
)
@click.option(
    "--save-dir",
    required=True,
    type=click.Path(),
    help="Directory to save the downloaded scenes (per-year subfolders).",
)
@click.option("--start-year", required=True, type=int)
@click.option("--end-year", required=True, type=int)
@click.option("--max-cloud", default=0.2, show_default=True, type=float)
@click.option("--min-clear", default=90.0, show_default=True, type=float)
@click.option(
    "--per-month",
    default=3,
    show_default=True,
    type=int,
    help="Keep only the K clearest scenes per month (0 = keep all).",
)
@click.option("--filter-col", default=None, help="AOI attribute column to filter on.")
@click.option("--filter-val", default=None, help="Value to match in --filter-col.")
@click.option(
    "--harmonize/--no-harmonize",
    default=True,
    show_default=True,
    help="Apply Planet's Sentinel-2 harmonization (temporal consistency).",
)
@click.option(
    "--order-name",
    default="planet_download_ps",
    show_default=True,
    help="Order name prefix shown in the Planet dashboard.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the quota confirmation prompt.",
)
def ps_download(
    geojson: str,
    save_dir: str,
    start_year: int,
    end_year: int,
    max_cloud: float,
    min_clear: float,
    per_month: int,
    filter_col: str | None,
    filter_val: str | None,
    harmonize: bool,
    order_name: str,
    yes: bool,
) -> None:
    """Download PlanetScope scenes clipped to an AOI via the Orders API.

    Scenes are ordered with the SR + udm2 bundle (fallback TOA) and a clip
    tool, one order per year, and land in {save-dir}/{year}/{order_id}/ —
    the layout `ps-composite` expects. NOTE: this consumes download quota.
    """
    region = _load_aoi_region(geojson, filter_col, filter_val)
    selected = _search_and_report(
        region, start_year, end_year, max_cloud, min_clear, per_month
    )

    if selected.empty:
        print("Nothing to download.")
        return

    if not yes:
        click.confirm(
            "Placing these orders will consume the billed area above. Continue?",
            abort=True,
        )

    os.makedirs(save_dir, exist_ok=True)
    selected.to_file(
        os.path.join(save_dir, "ps_selected_scenes.geojson"), driver="GeoJSON"
    )

    order_scenes(selected, region, save_dir, order_name=order_name, harmonize=harmonize)
    print(f"Done. Scenes downloaded to {save_dir}")


@cli.command("ps-composite")
@click.option(
    "--data-dir",
    required=True,
    type=click.Path(exists=True),
    help="Directory of per-year scene downloads (from ps-download).",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(),
    help="Directory to write the per-year composites.",
)
@click.option(
    "--geojson",
    required=True,
    type=click.Path(exists=True),
    help="Path to the GeoJSON polygon AOI (defines the output grid).",
)
@click.option("--filter-col", default=None, help="AOI attribute column to filter on.")
@click.option("--filter-val", default=None, help="Value to match in --filter-col.")
@click.option(
    "--operation",
    type=click.Choice(["mean", "median"], case_sensitive=False),
    default="median",
    show_default=True,
)
@click.option(
    "--target-resolution",
    default=3.0,
    show_default=True,
    type=float,
    help="Output pixel size in meters (3 m = PSScene native).",
)
@click.option("--overwrite", is_flag=True, default=False)
def ps_composite(
    data_dir: str,
    output_dir: str,
    geojson: str,
    filter_col: str | None,
    filter_val: str | None,
    operation: str,
    target_resolution: float,
    overwrite: bool,
) -> None:
    """Build cloud-masked yearly composites from downloaded PlanetScope scenes.

    Each scene is masked with its udm2 clear band, warped onto a common UTM
    grid over the AOI, and aggregated per pixel — one 4-band (B, G, R, NIR)
    uint16 GeoTIFF per year.
    """
    region = _load_aoi_region(geojson, filter_col, filter_val)
    process_scene_composites(
        data_dir=data_dir,
        save_dir=output_dir,
        region=region,
        resolution=target_resolution,
        operation=operation.lower(),
        overwrite=overwrite,
    )
    print(f"Done. Composites written to {output_dir}")


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
