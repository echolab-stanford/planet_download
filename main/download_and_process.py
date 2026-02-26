import os

import click
import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from tqdm import tqdm

from planet_download.client import BasemapsClient
from planet_download.composites import (
    parse_planet_filesystem,
    process_composites_parallel,
)


@click.group()
def cli():
    """Planet Download and Processing CLI - Download basemap quads and create composites."""
    pass


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
            year_union = year_labels.unary_union
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
def composite(
    data_dir: str,
    output_dir: str | None,
    operation: str,
    nthreads: int,
    overwrite: bool,
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

    print(f"Parsing Planet filesystem in: {data_dir}")
    dict_paths = parse_planet_filesystem(data_dir)

    print(f"Creating {operation} composites...")
    print(f"Output directory: {output_dir}")

    process_composites_parallel(
        dict_paths=dict_paths,
        save_dir=output_dir,
        operation=operation.lower(),
        overwrite=overwrite,
        nthreads=nthreads,
    )

    print("Composite creation complete!")


if __name__ == "__main__":
    cli()
