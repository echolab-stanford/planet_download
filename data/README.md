# `data/` — derived analysis artifacts

Pre-computed outputs from the coverage / inventory audit of the
**`planet_dhs_sa_africa`** Planet Global-Quarterly basemap dataset, generated on
**2026-06-26**. These are cached here so the analysis (which requires scanning
~58k GeoTIFFs on a slow network mount) does not have to be re-run.

The companion human-readable report is
[`../reports/planet_dhs_sa_africa_coverage.html`](../reports/planet_dhs_sa_africa_coverage.html).

## Source inputs (provenance roots)

All files below were derived from two inputs:

| Input | Location (at time of generation) |
|---|---|
| Planet quad TIFs + per-year crosswalks | `/mnt/sherlock/oak/embed_develop/data/raw/planet/planet_dhs_sa_africa/` |
| DHS cluster labels | `../examples/dhs_sa_africa_cluster.csv` |

The crosswalks (`crosswalk_quads_planet_{year}.geojson`, one per year 2017–2024)
were produced by the download step in `main/download_and_process.py`; each row is
a (quad, cluster) intersection pair with the quad footprint as geometry.

## Files

### `scan_results.json`
Full metadata inventory of **every** quad TIF (`global_quarterly_*/*.tif`,
58,379 files), read with rasterio (header only). Key fields:
- `total_tifs`, `readable`, `failed_count`
- `distinct_profiles` — unique `(width, height, bands, dtype, crs)` tuples with counts
- `size_counts`, `band_counts` — distributions
- `failed` — the 5 unreadable/corrupt files (all in `global_quarterly_2022q3_mosaic`)
- `by_mosaic_profile` — per-mosaic size breakdown

Headline result: all readable files are **4-band uint8, EPSG:3857**; sizes are
either **4096×4096** (51,246) or **2048×2048** (7,128 — 2017 Q1 & Q2 only).

### `coverage_summary.csv`
Per-year (2017–2024) coverage statistics, one row per year. Columns:
`year, csv_clusters, crosswalk_clusters, missing_from_crosswalk, quads, pairs,
clusters_multi_quad, pct_multi, max_quads_per_cluster, dist_quads_per_cluster`.
Computed by joining each year's crosswalk against the cluster CSV.

### `multi_quad_clusters.csv`
Every cluster that intersects **more than one** distinct quad, one row per
(year, cluster). Columns: `year, DHSID_EA, n_quads`. 15,095 cluster-years total.

### `per_cluster_quadcount.csv`
One row per cluster (2017–2024) with location and quad count. Columns:
`DHSID_EA, lat, lon, n_quads, year, cat` where `cat` ∈ {`1 (single)`, `2`,
`3-4`, `5+`}. Null-island (0,0) bad coordinates are dropped (19,241 rows).

### `all_quads.gpkg`
Unique Planet quad footprints per year (GeoPackage, EPSG:4326). One feature per
distinct quad-year. Columns: `id, x, y, mosaic_name, coverage, year, geometry`
(geometry = quad bounding box). 21,701 quad-year footprints.

### `map_overview.png`, `map_by_year.png`
Coverage maps rendered from the above:
- `map_overview.png` — all clusters coloured by number of intersecting quads.
- `map_by_year.png` — per-year quad footprints (grey) + clusters (blue=single,
  orange=multi-quad).

## Notes / caveats

- **Corrupt tiles:** the 5 files in `scan_results.json["failed"]` (all
  `global_quarterly_2022q3_mosaic`, `120{8,9},1211,1212,1222_883`) match the list
  in `scripts/redownload_corrupted.py`; the re-download never landed and they are
  excluded from all counts.
- **Missing cluster:** `ZM-2018-7#-00000179` is absent from the 2018 crosswalk
  because its coordinates in the source CSV are `(0, 0)` (bad value), not a
  download gap.
- Counts reflect the dataset as of the generation date; re-run the audit if the
  raw data changes.
