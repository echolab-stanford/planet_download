# Planet Download

Planet Download is a Python library and CLI for programmatically searching and downloading Planet imagery. It wraps the Planet Basemaps API and the PlanetScope scene APIs (Data + Orders), making it easy to:

- List available mosaics and series
- Download all imagery quads for a given area of interest (AOI) and time range
- Automate bulk downloads for geospatial workflows
- Use a filtering geometry to retrieve specific quads rather than a whole AOI
- Search and download PlanetScope (PSScene) surface-reflectance scenes clipped
  to an AOI, and build yearly cloud-masked median composites at 3 m with an
  NIR band

The library is inspired by the official [Planet Basemaps API notebooks](https://github.com/planetlabs/notebooks/blob/master/jupyter-notebooks/Basemaps-API/basemaps_api_introduction.ipynb), but provides a streamlined interface for both scripting and command-line use.

## Installation

Install the package and its dependencies (an editable install is recommended for
development). Using [`uv`](https://github.com/astral-sh/uv):

```bash
uv pip install -e .
```

or with plain `pip`:

```bash
pip install -e .
```

This pulls in everything the library needs: `requests`, `geopandas`, `pandas`,
`numpy`, `shapely`, `rasterio`, `rio-cogeo`, `click`, `tqdm`, `python-dotenv`,
and the official [`planet`](https://planet-sdk-for-python.readthedocs.io/) SDK
(used for the PlanetScope scene workflow).

## Setup

Create a `.env` file in your project root with your Planet API key:

```env
PL_API_KEY=your_planet_api_key_here
```

> **Note:** Your Planet API key must be entitled to **Basemaps / Mosaics**
> products. The Basemaps API authenticates any valid key (returning HTTP 200),
> but it only returns the mosaics and series your plan is provisioned for — an
> account without a basemaps entitlement will see empty `series`/`mosaics`
> listings (and lookups like `Global Quarterly` will fail with "not found").
> Scene-catalog (Data API) access does not imply basemap access. Confirm your
> plan's basemap entitlement and available series names with your Planet account
> contact if listings come back empty.

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

The same `planet-download` entry point exposes the full processing CLI
(`download`, `download-aoi`, `composite`, `resample`, `merge`, `grid-mosaic`,
and the `ps-*` commands below); run `planet-download --help` for the list.
You can also run it without installing, straight from the repo or from GitHub,
with [`uvx`](https://docs.astral.sh/uv/guides/tools/):

```bash
uvx --from . planet-download ps-search ...                # local checkout
uvx --from git+https://github.com/echolab-stanford/planet_download \
  planet-download ps-search ...                           # straight from GitHub
```

(`uvx` builds the package in an isolated environment; the CLI reads
`PL_API_KEY` from the `.env` in your working directory as usual.)

## PlanetScope scenes (PSScene)

Besides basemaps, the CLI can download raw PlanetScope scenes for an AOI and
composite them into one cloud-free image per year (4-band **B, G, R, NIR**
`uint16` GeoTIFF at 3 m, in the AOI's UTM zone). Scenes are ordered through the
Orders API with the surface-reflectance + udm2 bundle (`analytic_sr_udm2`),
Sentinel-2 harmonization, and a server-side clip to the AOI. The pipeline is
four steps: **search → download → mask → composite**.

Start with a free dry run to see how many scenes match and what they would
cost in download quota:

```bash
planet-download ps-search \
  --geojson kenya.geojson --filter-col COUNTY_NAM --filter-val SIAYA \
  --start-year 2022 --end-year 2023
```

Then download, apply the udm2 cloud mask, and composite:

```bash
planet-download ps-download \
  --geojson kenya.geojson --filter-col COUNTY_NAM --filter-val SIAYA \
  --start-year 2022 --end-year 2023 \
  --per-month 30 --min-aoi-coverage 1.0 \
  --save-dir data/ps_scenes

# Mask each scene with its udm2 clear band (cloud/shadow/haze/snow -> nodata)
planet-download ps-mask \
  --data-dir data/ps_scenes --output-dir data/ps_masked

planet-download ps-composite \
  --data-dir data/ps_masked --output-dir data/ps_composites \
  --geojson kenya.geojson --filter-col COUNTY_NAM --filter-val SIAYA \
  --nthreads 8
```

Only surface-reflectance is retrieved (`ortho_analytic_4b_sr`); SR is not
licensed for the whole archive (e.g. ~2/3 of 2017 PS2 scenes), so `ps-search`/
`ps-download` verify SR availability per scene and drop the rest — otherwise the
Orders API rejects the whole order.

Useful flags for tuning cost, quality, and speed:

- `--per-month N` — scenes kept per month (default 3). Higher N gives a smoother
  median and more diversity, at proportionally more quota.
- `--min-aoi-coverage PCT` — drop scenes whose footprint covers less than `PCT`
  of the AOI (default 0 = keep all). A **sliver filter**: a single PlanetScope
  scene only covers a few percent of a county-sized AOI, so this spends per-month
  slots on full-swath scenes instead of edge clips. It does *not* reduce quota
  (premium billing charges per km², and slivers are already cheap) — it improves
  coverage/median quality per scene.
- `ps-download` skips scenes already present under `--save-dir`, so raising
  `--per-month` later only orders (and bills for) the new scenes.
- `--nthreads N` on `ps-composite` — the composite runs one worker process per
  output tile group; 8 is a good default on a multi-core box.

> **Note on quota:** unlike basemap quads, PSScene downloads consume an
> area-based quota — every clipped scene bills its intersection with the AOI.
> Downloading *all* clear scenes of a year would cost ~200× the AOI area, so
> by default only the `--per-month 3` clearest scenes are kept (~36 per year,
> plenty for a median composite). `ps-search` always prints the estimated
> billed km² first, and `ps-download` asks for confirmation before placing
> orders (skip with `--yes`).

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


