"""
PlanetScope (PSScene) search, selection, and ordering.

Unlike the Basemaps API client in ``client.py``, PlanetScope scenes come from
the Data API (search) and the Orders API (server-side clip + delivery). Both
are accessed through Planet's official ``planet`` SDK, which handles
activation/order polling, pagination, rate-limit retries, and checksums.

The intended flow (see the ``ps-*`` CLI commands):

1. ``search_scenes``   - find PSScene items over an AOI/year range (free).
2. ``select_scenes``   - keep the top-K clearest scenes per month. Quota is
   billed per clipped scene ("premium" style), so downloading every clear
   scene of a year would cost ~200x the AOI area; a median composite
   saturates with a few dozen looks.
3. ``estimate_billed_area`` - preview the quota cost before ordering.
4. ``order_scenes``    - place clipped Orders API orders and download them.
"""

import datetime as dt
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
from planet import Auth, Planet, data_filter, order_request
from planet.http import Session
from shapely.geometry import shape

# Surface reflectance + usable-data-mask bundle; TOA fallback for scenes
# without an SR asset. 4-band (B, G, R, NIR) for 2017+ temporal consistency.
PRODUCT_BUNDLE = "analytic_sr_udm2"
FALLBACK_BUNDLE = "analytic_udm2"
ITEM_TYPE = "PSScene"

# Orders API caps orders at 500 items each.
MAX_ITEMS_PER_ORDER = 500


def get_planet_client(api_key: str | None = None) -> Planet:
    """Build a synchronous Planet SDK client authenticated with an API key.

    Parameters
    ----------
    api_key : str, optional
        Planet API key. Defaults to the PL_API_KEY environment variable
        (loaded from .env by ``planet_download.client`` on import).
    """
    if api_key is None:
        api_key = os.getenv("PL_API_KEY")
    if not api_key:
        raise ValueError(
            "No API key provided! Either set the PL_API_KEY environment "
            "variable or use the api_key parameter"
        )
    return Planet(Session(auth=Auth.from_key(api_key)))


def search_scenes(
    region: dict,
    start_year: int,
    end_year: int,
    max_cloud: float = 0.2,
    min_clear: float = 90.0,
    client: Planet | None = None,
) -> gpd.GeoDataFrame:
    """Search PSScene items overlapping an AOI over a year range.

    Search is free (no quota). Only scenes with a downloadable SR asset are
    returned, so every result can be ordered with ``PRODUCT_BUNDLE``.

    Parameters
    ----------
    region : dict
        GeoJSON geometry (WGS84) of the AOI, as from ``_load_aoi_region``.
    start_year, end_year : int
        Year range to search (inclusive).
    max_cloud : float, optional
        Maximum scene ``cloud_cover`` fraction. Default 0.2.
    min_clear : float, optional
        Minimum scene ``clear_percent`` (0-100). Default 90.
    client : Planet, optional
        SDK client; created from PL_API_KEY if not given.

    Returns
    -------
    gpd.GeoDataFrame
        One row per scene footprint with id, acquired, year, month,
        cloud_cover, clear_percent, and instrument columns.
    """
    if client is None:
        client = get_planet_client()

    search_filter = data_filter.and_filter(
        [
            data_filter.geometry_filter(region),
            data_filter.date_range_filter(
                "acquired",
                gte=dt.datetime(start_year, 1, 1),
                lt=dt.datetime(end_year + 1, 1, 1),
            ),
            data_filter.range_filter("cloud_cover", lte=max_cloud),
            data_filter.range_filter("clear_percent", gte=min_clear),
            data_filter.asset_filter(["ortho_analytic_4b_sr", "ortho_udm2"]),
            data_filter.permission_filter(),
        ]
    )

    items = client.data.search([ITEM_TYPE], search_filter=search_filter, limit=0)

    records = []
    for item in items:
        props = item["properties"]
        acquired = pd.Timestamp(props["acquired"])
        records.append(
            {
                "id": item["id"],
                "acquired": acquired,
                "year": acquired.year,
                "month": acquired.month,
                "cloud_cover": props.get("cloud_cover"),
                "clear_percent": props.get("clear_percent"),
                "instrument": props.get("instrument"),
                "geometry": shape(item["geometry"]),
            }
        )

    return gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")


def select_scenes(scenes: gpd.GeoDataFrame, per_month: int = 3) -> gpd.GeoDataFrame:
    """Keep the top-K clearest scenes per (year, month).

    This is the quota guard: under premium billing every clipped scene bills
    its area, and ~3 scenes/month is plenty for a yearly median composite.

    Parameters
    ----------
    scenes : gpd.GeoDataFrame
        Output of ``search_scenes``.
    per_month : int, optional
        Scenes to keep per month. Default 3. Use 0 to keep everything.
    """
    if per_month <= 0 or scenes.empty:
        return scenes

    return (
        scenes.sort_values("clear_percent", ascending=False)
        .groupby(["year", "month"], group_keys=False)
        .head(per_month)
        .sort_values("acquired")
        .reset_index(drop=True)
    )


def estimate_billed_area(scenes: gpd.GeoDataFrame, region: dict) -> float:
    """Estimate the quota cost (km²) of ordering these scenes clipped to the AOI.

    Premium-style billing charges the clipped area of every delivered scene,
    i.e. the sum over scenes of area(footprint ∩ AOI).
    """
    if scenes.empty:
        return 0.0

    aoi = gpd.GeoSeries([shape(region)], crs="EPSG:4326")
    utm = scenes.estimate_utm_crs()
    clipped = scenes.to_crs(utm).intersection(aoi.to_crs(utm).iloc[0])
    return float(clipped.area.sum() / 1e6)


def order_scenes(
    scenes: gpd.GeoDataFrame,
    region: dict,
    save_dir: str | Path,
    order_name: str = "planet_download_ps",
    harmonize: bool = True,
    client: Planet | None = None,
) -> list[Path]:
    """Order scenes clipped to the AOI and download them per year.

    One order is placed per year (split into chunks of ``MAX_ITEMS_PER_ORDER``)
    with the SR + udm2 bundle and a clip tool, so quota is only spent on the
    AOI. Downloads land in ``{save_dir}/{year}/{order_id}/...`` - the layout
    ``find_scene_files`` expects for compositing.

    NOTE: placing an order consumes download quota. Preview the cost with
    ``estimate_billed_area`` first.

    Parameters
    ----------
    scenes : gpd.GeoDataFrame
        Scenes to order (output of ``search_scenes``/``select_scenes``).
    region : dict
        GeoJSON geometry (WGS84) used as the clip AOI.
    save_dir : str | Path
        Base directory for the downloaded orders.
    order_name : str, optional
        Prefix for the order names shown in the Planet dashboard.
    harmonize : bool, optional
        Apply Planet's Sentinel-2 harmonization tool (recommended for
        temporal consistency across instrument generations). Default True.
    client : Planet, optional
        SDK client; created from PL_API_KEY if not given.

    Returns
    -------
    list[Path]
        Paths of all downloaded files.
    """
    if client is None:
        client = get_planet_client()

    tools = [order_request.clip_tool(aoi=region)]
    if harmonize:
        tools.insert(0, order_request.harmonize_tool("Sentinel-2"))

    downloaded: list[Path] = []
    for year, year_scenes in scenes.groupby("year"):
        year_dir = Path(save_dir) / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)

        ids = year_scenes["id"].tolist()
        chunks = [
            ids[i : i + MAX_ITEMS_PER_ORDER]
            for i in range(0, len(ids), MAX_ITEMS_PER_ORDER)
        ]
        for i, chunk in enumerate(chunks):
            request = order_request.build_request(
                name=f"{order_name}_{year}_{i:02d}",
                products=[
                    order_request.product(
                        chunk,
                        product_bundle=PRODUCT_BUNDLE,
                        item_type=ITEM_TYPE,
                        fallback_bundle=FALLBACK_BUNDLE,
                    )
                ],
                tools=tools,
            )

            order = client.orders.create_order(request)
            order_id = order["id"]
            print(f"Order {order_id} ({year}, {len(chunk)} scenes) submitted...")

            client.orders.wait(order_id, max_attempts=0)
            paths = client.orders.download_order(
                order_id, directory=year_dir, progress_bar=True
            )
            downloaded.extend(paths)

    return downloaded
