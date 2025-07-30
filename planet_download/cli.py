"""Command line interface for Planet Download."""

import click
from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables at module level
load_dotenv()


@click.group()
def cli():
    """Planet Download CLI for basemap data."""
    pass


# --- Download All Images Command ---
@cli.command(
    help="Download all images for a GeoJSON AOI from a Planet basemap series. Example:\n  planet-download download-all --geojson kenya.geojson --start-date 2020-01-01 --end-date 2020-12-31 --cadence 'Global Quarterly'"
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
    import geopandas as gpd

    from planet_download.client import BasemapsClient

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

    # Trigger the downloads
    click.echo("Starting downloads...")
    for d in tqdm(downloads, desc="Downloading files", total=len(downloads)):
        pass


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
