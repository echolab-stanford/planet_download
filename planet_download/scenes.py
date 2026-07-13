"""
PlanetScope (PSScene) search, selection, and ordering.

Unlike the Basemaps API client in ``client.py``, PlanetScope scenes come from
the Data API (search) and the Orders API (server-side clip + delivery). Both
are accessed through Planet's official ``planet`` SDK, which handles
activation/order polling, pagination, rate-limit retries, and checksums.

The intended flow (see the ``ps-*`` CLI commands):

1. ``search_scenes``   - find PSScene items over an AOI/year range (free).
2. ``sr_downloadable`` - keep only scenes we can license surface reflectance
   for (SR is not available for the whole archive).
3. ``select_scenes``   - keep the top-K clearest scenes per month. Quota is
   billed per clipped scene ("premium" style), so downloading every clear
   scene of a year would cost ~200x the AOI area; a median composite
   saturates with a few dozen looks.
4. ``estimate_billed_area`` - preview the quota cost before ordering.
5. ``order_scenes``    - place clipped Orders API orders and download them.

Masking (udm2) and compositing then happen in ``composites.py``.
"""

import datetime as dt
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
from planet import Auth, Planet, data_filter, order_request
from planet.http import Session
from shapely.geometry import shape
from tqdm import tqdm

# Surface reflectance + usable-data-mask bundle: delivers ortho_analytic_4b_sr
# (4-band B, G, R, NIR) plus the ortho_udm2 cloud mask. No TOA fallback — we
# order SR only, so scenes without a licensed SR asset are filtered out before
# ordering (see ``sr_downloadable``) rather than silently downloaded as TOA.
PRODUCT_BUNDLE = "analytic_sr_udm2"
SR_ASSET = "ortho_analytic_4b_sr"
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

    Search is free (no quota). Results are pre-filtered to scenes that list an
    SR asset and grant generic download permission, but SR access must still be
    confirmed per item with ``sr_downloadable`` before ordering.

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


def filter_aoi_coverage(
    scenes: gpd.GeoDataFrame, region: dict, min_pct: float
) -> gpd.GeoDataFrame:
    """Drop scenes whose footprint covers less than ``min_pct`` of the AOI.

    A single PlanetScope scene only covers a few percent of a county-sized AOI,
    so this is a sliver filter: it removes scenes that merely clip an edge (a
    per-month slot better spent on a full-swath scene), not a "most of the AOI"
    filter.

    Parameters
    ----------
    scenes : gpd.GeoDataFrame
        Output of ``search_scenes``.
    region : dict
        GeoJSON geometry (WGS84) of the AOI.
    min_pct : float
        Minimum footprint ∩ AOI area as a percentage of the AOI. 0 keeps all.
    """
    if min_pct <= 0 or scenes.empty:
        return scenes

    aoi = gpd.GeoSeries([shape(region)], crs="EPSG:4326")
    utm = scenes.estimate_utm_crs()
    aoi_geom = aoi.to_crs(utm).iloc[0]
    clip = scenes.to_crs(utm).intersection(aoi_geom)
    pct = clip.area / aoi_geom.area * 100
    return scenes[pct.values >= min_pct]


def downloaded_scene_ids(save_dir: str | Path) -> set[str]:
    """Scene ids already downloaded under ``save_dir`` (from a prior order).

    Lets ``ps-download`` skip re-ordering scenes already on disk, so growing a
    selection (e.g. 15 → 30 per month) only spends quota on the new scenes.
    """
    ids = set()
    for path in Path(save_dir).rglob("*_3B_*.tif"):
        ids.add(path.name.split("_3B_")[0])
    return ids


def filter_months(scenes: gpd.GeoDataFrame, months: list[int]) -> gpd.GeoDataFrame:
    """Keep only scenes acquired in the given months (e.g. [6, 7, 8] for JJA)."""
    if not months or scenes.empty:
        return scenes
    return scenes[scenes["month"].isin(months)]


def select_best_day(
    scenes: gpd.GeoDataFrame, region: dict, client: Planet | None = None
) -> gpd.GeoDataFrame:
    """Return the scenes of the single acquisition day that best covers the AOI.

    A phenology-controlled alternative to a temporal median: within an already
    date-windowed set (e.g. JJA), pick the day whose scenes tile the most of the
    AOI, breaking ties toward lower cloud cover, and return that day's
    SR-downloadable scenes. A single day gives real (non-median) pixels from one
    coherent pass, and since PlanetScope images the AOI daily a clear day usually
    covers most of it.

    Parameters
    ----------
    scenes : gpd.GeoDataFrame
        Candidate scenes (output of ``search_scenes``, typically month-filtered).
    region : dict
        GeoJSON geometry (WGS84) of the AOI.
    client : Planet, optional
        SDK client for the SR-availability check; created if not given.

    Returns
    -------
    gpd.GeoDataFrame
        The chosen day's SR-downloadable scenes (empty if none).
    """
    if scenes.empty:
        return scenes

    utm = scenes.estimate_utm_crs()
    aoi_geom = gpd.GeoSeries([shape(region)], crs="EPSG:4326").to_crs(utm).iloc[0]
    projected = scenes.to_crs(utm)
    dates = scenes["acquired"].dt.date

    scored = []
    for day, idx in dates.groupby(dates).groups.items():
        coverage = projected.loc[idx].union_all().intersection(aoi_geom).area
        scored.append((day, coverage, scenes.loc[idx, "cloud_cover"].mean()))
    # Most AOI coverage first, then least cloud.
    scored.sort(key=lambda s: (-s[1], s[2]))
    best_day = scored[0][0]

    day_scenes = scenes[dates == best_day]
    sr_ids = sr_downloadable(day_scenes["id"].tolist(), client=client)
    return day_scenes[day_scenes["id"].isin(sr_ids)].reset_index(drop=True)


def sr_downloadable(scene_ids: list[str], client: Planet | None = None) -> set[str]:
    """Return the subset of scene ids whose SR asset we can download.

    Surface reflectance is only licensed for part of the PlanetScope archive
    (e.g. ~2/3 of the 2017 PS2 scenes). An Orders API order is rejected wholesale
    if any item lacks SR access, so callers must drop non-SR scenes first. This
    checks each item's asset permissions (a free Data API call per scene).

    Parameters
    ----------
    scene_ids : list[str]
        Candidate PSScene ids to check.
    client : Planet, optional
        SDK client; created from PL_API_KEY if not given.

    Returns
    -------
    set[str]
        Ids with a downloadable ``ortho_analytic_4b_sr`` asset.
    """
    if client is None:
        client = get_planet_client()

    keep = set()
    for sid in tqdm(scene_ids, desc="Checking SR availability"):
        try:
            assets = client.data.list_item_assets(ITEM_TYPE, sid)
        except Exception:
            continue
        if "download" in (assets.get(SR_ASSET, {}).get("_permissions") or []):
            keep.add(sid)
    return keep


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
