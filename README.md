# Planet Download

Planet Download is a Python library and CLI for programmatically searching and downloading Planet Basemaps imagery. It wraps the Planet Basemaps API, making it easy to:

- List available mosaics and series
- Download all imagery quads for a given area of interest (AOI) and time range
- Automate bulk downloads for geospatial workflows
- Use a filtering geometry to retrieve specific quads rather than a whole AOI

The library is inspired by the official [Planet Basemaps API notebooks](https://github.com/planetlabs/notebooks/blob/master/jupyter-notebooks/Basemaps-API/basemaps_api_introduction.ipynb), but provides a streamlined interface for both scripting and command-line use.

## Installation

Install the required dependencies:

```bash
pip install python-dotenv geopandas pandas click
```

## Setup

Create a `.env` file in your project root with your Planet API key:

```env
PL_API_KEY=your_planet_api_key_here
```

## Command Line Usage

Download all images for a GeoJSON AOI and date range:

```bash
planet-download download-all \
--geojson kenya.geojson \
--start-date 2020-01-01 \
--end-date 2020-12-31 \
--cadence "Global Quarterly"
```

This will download all available imagery quads for the AOI and time range from the specified basemap series (default: Global Quarterly).

## Python Example

You can also use the library directly in Python:

```python
import geopandas as gpd
from planet_download.client import BasemapsClient

# Start the client (expects PL_API_KEY in .env)
client = BasemapsClient()

# Load the geojson file
gdf = gpd.read_file("kenya.geojson")
gdf_bounds = gdf.total_bounds

# Choose the basemap series (default: Global Quarterly)
series = client.series(name="Global Quarterly")

# Download all images in the AOI and date range
downloads = series.download_quads(bbox=gdf_bounds, start_date="2020-01-01", end_date="2020-12-31")
list(downloads)
```

See the `examples/examples_api.ipynb` notebook for more advanced usage and geospatial workflows.

## What does this library do?

This package provides a simple interface to the Planet Basemaps API, allowing you to:

- Authenticate using a `.env` file and your Planet API key
- List available mosaics and series
- Download all imagery quads for a user-defined AOI and time range
- Integrate Planet imagery into Python and geospatial workflows

It is inspired by the official Planet Basemaps API notebooks, but is designed for automation and scripting.


