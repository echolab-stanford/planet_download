import os

import click
import geopandas as gpd
import pandas as pd
from tqdm import tqdm

from planet_download.client import BasemapsClient


@click.command()
@click.option(
    "--path-to-geom",
    required=True,
    type=click.Path(exists=True),
    help="Path to the GeoJSON file defining the AOI.",
)
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
    path_to_geom: str,
    labels: str,
    save_dir: str,
    cadence: str,
    lat: str,
    lon: str,
    nthreads: int,
    buffer_size: int,
) -> None:
    """
    Download all images for a small AOI defined by a GeoJSON file using BasemapsClient and MosaicSeries.

    This CLI will:
    - Buffer each label point in the CSV by the specified buffer size
    - Find all Planet Basemap quads overlapping the AOI and label buffers
    - Download the selected quads to the specified directory
    - Process each year in the labels file separately if a 'year' column is present
    """
    # Start the client
    client = BasemapsClient()

    # Load the geojson and get bounds
    gdf = gpd.read_file(path_to_geom)
    gdf_bounds = gdf.total_bounds

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

            quads, df = series.all_quads_overlap(
                start_date=start_date, end_date=end_date, bbox=gdf_bounds
            )

            overlap = gpd.sjoin(
                df,
                labels_aoi[labels_aoi["year"] == year],
                how="inner",
                predicate="intersects",
            )

        # Droping duplicates between id and mosaic names (year-quarter). We can have multiple
        # quads per label, so we keep those too.
        idx = overlap.drop_duplicates(subset=["id", "mosaic_name"]).index.tolist()
        sel_quads = [quads[i] for i in idx]

        # Save crosswalk file
        overlap.to_file(
            os.path.join(f"crosswalk_quads_planet_{year}.geojson"), driver="GeoJSON"
        )

        downloads = series.download_selection(
            sel_quads,
            flat=False,
            save_dir=save_dir,
            filename_template="{x}_{y}.tif",
            nthreads=nthreads,
        )
        for download in tqdm(
            downloads, desc="Downloading files...", total=len(sel_quads)
        ):
            pass
