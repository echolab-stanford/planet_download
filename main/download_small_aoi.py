import os

import click
import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from tqdm import tqdm

from planet_download.client import BasemapsClient


@click.command()
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
def download_planet_labels_aoi(
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

    This CLI will:
    - Buffer each label point in the CSV by the specified buffer size
    - Create a spatial union of buffered points for each year
    - Use server-side spatial filtering to find only relevant quads
    - Download the selected quads to the specified directory
    - Process each year in the labels file separately if a 'year' column is present
    """
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
                region=region_dict  # Server-side spatial filtering
            )

            print(f"Found {len(quads)} relevant quads (vs potentially thousands with bbox approach)")

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
                driver="GeoJSON"
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


if __name__ == "__main__":
    download_planet_labels_aoi()
