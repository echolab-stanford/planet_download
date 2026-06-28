"""Extract non-overlapping 512x512 RGB patches from Planet *composite* quads as
plain PNG files packed into uniformly-sized shard folders, with one manifest.csv.

This is the composite-input sibling of ``patchify.py``. The composites are
per-YEAR temporal medians (q1/q2/q3 collapsed into one image), so they are
effectively cloud-free -- residual clouds only survive where a pixel was cloudy
in >=2 of 3 quarters. Use this instead of ``patchify.py`` to avoid the clouds
present in the raw per-quarter quads.

Differences from patchify.py (raw quads):
- Source layout is ``<src>/<YYYY>/<x>_<y>_composite.tif`` (one file per quad per
  year), NOT ``<src>/global_quarterly_<YYYYqQ>/<x>_<y>.tif``.
- Composites are float64 (values ~5..255); they are rounded/clipped to uint8
  before PNG encoding.
- There is no quarter dimension: keys/filenames are
  ``<BLOCK>_<YYYY>_<x>_<y>_<rowcol>`` and the manifest has a ``year`` column
  (no ``quarter``).
- Cluster crosswalks are keyed by ``(year, 'x-y')``, aggregating DHS clusters
  across the three quarters (deduped by DHSID).
- Composites are fully covered (alpha == 255 everywhere), so ``--min-valid`` is
  effectively a no-op; an optional ``--max-cloud`` patch filter is provided as a
  cheap safety net for the rare residual-cloud patch. ``cloud_frac`` is always
  recorded in the manifest.

Output (identical scheme to patchify.py):

    <out>/shard_<WW>_<NNNNN>/<key>.png   each folder holds <= --shard-size PNGs
        e.g. out/shard_03_00012/N10W006_2020_1986_2167_0000.png
    <out>/manifest.csv                   one row per patch (provenance + clusters)

Folder sizing, deterministic sampling (--sample-frac/--seed), and resume
(--resume) behave exactly as in patchify.py: a resumed run MUST reuse the SAME
--workers/--grid-deg/--shard-size/--sample-frac/--seed (and --max-cloud, which
also affects which patches are written).

The patch grid IS the COG internal 512x512 block grid, so reads decode nothing
extra. 4096px quads -> 64 patches; 2048px quads (2017) -> 16 patches.

Usage:
    python patchify_composite.py --src $SCRATCH/planet_composite_sa_dhs \\
                                 --out $SCRATCH/planet_patches_composite \\
                                 --crosswalks $SCRATCH/planet_dhs_sa_africa \\
                                 --workers 48 --grid-deg 1 --shard-size 512 \\
                                 --min-valid 0.5 --max-cloud 1.0 \\
                                 --sample-frac 1.0 --seed 0 [--resume]
"""

import argparse
import csv
import hashlib
import json
import math
import glob
import os
import shutil
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import rasterio
from rasterio.transform import xy as transform_xy, rowcol as transform_rowcol
from rasterio.warp import transform as warp_transform
from PIL import Image

PATCH = 512
# Cloud heuristic (RGB-only): a pixel is "cloud-like" if it is bright in ALL
# channels AND nearly colorless (white/gray). This separates clouds from tan
# sand / colorful land; it is intentionally conservative.
CLOUD_BRIGHT = 180  # min(R,G,B) above this
CLOUD_SAT = 0.15  # (max-min)/max below this
MANIFEST_COLS = [
    "path",
    "block",
    "lat",
    "lon",
    "year",
    "quad_x",
    "quad_y",
    "row",
    "col",
    "mosaic",
    "valid_frac",
    "cloud_frac",
    "dhsids",
    "n_clusters",
]


def block_label(lat, lon, grid_deg):
    """Grid-block label from the block's lower-left corner, e.g. 'S05E034'."""
    la = math.floor(lat / grid_deg) * grid_deg
    lo = math.floor(lon / grid_deg) * grid_deg
    ns, ew = ("S", "N")[la >= 0], ("W", "E")[lo >= 0]
    return f"{ns}{abs(int(la)):02d}{ew}{abs(int(lo)):03d}"


def parse_name(path):
    """'.../2020/1217_1019_composite.tif' -> (2020, 1217, 1019)."""
    p = Path(path)
    year = int(p.parent.name)
    x, y = p.stem.replace("_composite", "").split("_")
    return year, int(x), int(y)


def keep_sample(key, seed, frac):
    """Deterministic, stateless sampler: True for ~frac of keys, stable across runs."""
    if frac >= 1.0:
        return True
    h = hashlib.blake2b(f"{seed}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64 < frac


def cloud_fraction(rgb):
    """Fraction of cloud-like pixels in an HxWx3 uint8 patch (see constants)."""
    r = rgb.astype(np.float32)
    mn = r.min(2)
    mx = r.max(2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
    return float(((mn > CLOUD_BRIGHT) & (sat < CLOUD_SAT)).mean())


def read_crosswalks(src, grid_deg):
    """Return (block_index, cluster_index) keyed by (year, 'x-y').

    Clusters are aggregated across the three quarters and deduped by DHSID, so a
    quad maps to the union of clusters it contains in that year.
    """
    import geopandas as gpd

    block_idx, clust_idx = {}, defaultdict(dict)
    for f in sorted(glob.glob(f"{src}/crosswalk_quads_planet_*.geojson")):
        g = gpd.read_file(f)
        bounds = g.geometry.bounds
        for qid, year, dhsid, clat, clon, miny, maxy, minx, maxx in zip(
            g["id"],
            g["year"],
            g["DHSID_EA"],
            g["lat"],
            g["lon"],
            bounds.miny,
            bounds.maxy,
            bounds.minx,
            bounds.maxx,
        ):
            key = (int(year), qid)
            if key not in block_idx:
                block_idx[key] = block_label(
                    (miny + maxy) / 2, (minx + maxx) / 2, grid_deg
                )
            clust_idx[key][dhsid] = (dhsid, float(clat), float(clon))
    return block_idx, {k: list(v.values()) for k, v in clust_idx.items()}


def patches_for(path, min_valid, max_cloud, clusters):
    """Open one composite quad; yield (iy, ix, rgb HxWx3 uint8, lat, lon, frac,
    cloud_frac, dhsids). Drops patches below --min-valid or above --max-cloud."""
    with rasterio.open(path) as s:
        a = s.read()  # float64, C x H x W
        tr, crs = s.transform, s.crs
    C, H, W = a.shape
    ny, nx = H // PATCH, W // PATCH
    a = a[:, : ny * PATCH, : nx * PATCH]
    grid = a.reshape(C, ny, PATCH, nx, PATCH).transpose(1, 3, 0, 2, 4)
    has_alpha = C >= 4

    rows = [iy * PATCH + PATCH // 2 for iy in range(ny) for ix in range(nx)]
    cols = [ix * PATCH + PATCH // 2 for iy in range(ny) for ix in range(nx)]
    xs, ys = transform_xy(tr, rows, cols, offset="center")
    lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)

    patch_dhs = defaultdict(list)
    if clusters:
        cx, cy = warp_transform(
            "EPSG:4326", crs, [c[2] for c in clusters], [c[1] for c in clusters]
        )
        rr, cc = transform_rowcol(tr, cx, cy)
        for (dhsid, _, _), r, c in zip(clusters, np.atleast_1d(rr), np.atleast_1d(cc)):
            if 0 <= r < H and 0 <= c < W:
                patch_dhs[(int(r) // PATCH, int(c) // PATCH)].append(dhsid)

    k = 0
    for iy in range(ny):
        for ix in range(nx):
            p = grid[iy, ix]
            lat, lon = lats[k], lons[k]
            k += 1
            frac = float((p[3] > 0).mean()) if has_alpha else 1.0
            if frac < min_valid:
                continue
            rgb = np.ascontiguousarray(
                np.clip(np.rint(p[:3]), 0, 255).astype(np.uint8).transpose(1, 2, 0)
            )
            cf = cloud_fraction(rgb)
            if cf > max_cloud:
                continue
            yield iy, ix, rgb, lat, lon, frac, cf, patch_dhs.get((iy, ix), [])


class ChunkWriter:
    """Fills uniform shard folders (<= shard_size PNGs each) for one worker."""

    def __init__(self, out, wid, shard_size, compress):
        self.out, self.wid = Path(out), wid
        self.shard_size, self.compress = shard_size, compress
        self.idx, self.in_dir, self.total, self.cur = -1, 0, 0, None

    def seek(self, p):
        """Position the writer as if `p` patches were already written."""
        self.total = p
        self.idx = p // self.shard_size
        self.in_dir = p % self.shard_size
        self.cur = self.out / f"shard_{self.wid:02d}_{self.idx:05d}"
        self.cur.mkdir(parents=True, exist_ok=True)

    def _roll(self):
        self.idx += 1
        self.in_dir = 0
        self.cur = self.out / f"shard_{self.wid:02d}_{self.idx:05d}"
        self.cur.mkdir(parents=True, exist_ok=True)

    def write(self, key, rgb):
        if self.cur is None or self.in_dir >= self.shard_size:
            self._roll()
        target = self.cur / f"{key}.png"
        if not target.exists():  # idempotent on re-process
            Image.fromarray(rgb).save(target, "PNG", compress_level=self.compress)
        self.in_dir += 1
        self.total += 1
        return f"{self.cur.name}/{key}.png"


def process_chunk(
    wid,
    items,
    out,
    shard_size,
    min_valid,
    max_cloud,
    compress,
    grid_deg,
    sample_frac,
    seed,
    resume,
):
    """Process a worker's composite quads (pre-sorted) into uniform shard folders.

    Checkpoints after every quad; writes manifest rows to a durable per-worker
    fragment so the run is resumable. Returns counts + read errors only.
    """
    out = Path(out)
    ckpt = out / ".ckpt" / f"w{wid:02d}.json"
    frag = out / ".manifest" / f"w{wid:02d}.csv"
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    frag.parent.mkdir(parents=True, exist_ok=True)

    start_i = 0
    if resume and ckpt.exists():
        d = json.loads(ckpt.read_text())
        start_i, p0 = d["quads_done"], d["patches_written"]
        fmode = "a"
    else:
        p0, fmode = 0, "w"  # fresh: truncate fragment

    w = ChunkWriter(out, wid, shard_size, compress)
    w.seek(p0)
    errors = []
    fragf = open(frag, fmode, newline="")
    fw = csv.writer(fragf)
    for i in range(start_i, len(items)):
        path, block, year, clusters = items[i]
        try:
            _, qx, qy = parse_name(path)
            rows = []
            for iy, ix, rgb, lat, lon, frac, cf, dhsids in patches_for(
                path, min_valid, max_cloud, clusters
            ):
                key = f"{block}_{year}_{qx}_{qy}_{iy:02d}{ix:02d}"
                if not keep_sample(key, seed, sample_frac):
                    continue
                rel = w.write(key, rgb)
                rows.append(
                    (
                        rel,
                        block_label(lat, lon, grid_deg),
                        round(lat, 6),
                        round(lon, 6),
                        year,
                        qx,
                        qy,
                        iy,
                        ix,
                        f"planet_composite_{year}",
                        round(frac, 4),
                        round(cf, 4),
                        ";".join(dhsids),
                        len(dhsids),
                    )
                )
            fw.writerows(rows)
            fragf.flush()
            os.fsync(fragf.fileno())
            tmp = ckpt.with_suffix(".tmp")  # atomic checkpoint
            tmp.write_text(
                json.dumps({"quads_done": i + 1, "patches_written": w.total})
            )
            tmp.replace(ckpt)
        except Exception as e:
            errors.append((str(path), str(e)[:160]))
    fragf.close()
    return {"patches": w.total, "errors": errors}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--src",
        required=True,
        help="composite dir (<YYYY>/<x>_<y>_composite.tif)",
    )
    ap.add_argument("--out", required=True, help="output dir (local/SCRATCH)")
    ap.add_argument(
        "--crosswalks",
        default=None,
        help="dir with crosswalk_quads_planet_*.geojson (default: --src)",
    )
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument(
        "--grid-deg", type=float, default=1.0, help="lat/lon block size (deg)"
    )
    ap.add_argument("--shard-size", type=int, default=512, help="PNGs per shard folder")
    ap.add_argument(
        "--min-valid", type=float, default=0.5, help="min non-alpha-zero fraction"
    )
    ap.add_argument(
        "--max-cloud",
        type=float,
        default=1.0,
        help="drop patches whose cloud_frac exceeds this (1.0 = keep all)",
    )
    ap.add_argument("--compress", type=int, default=6, help="PNG compress_level 0-9")
    ap.add_argument(
        "--sample-frac",
        type=float,
        default=1.0,
        help="keep this fraction of patches (deterministic). 1.0 = all",
    )
    ap.add_argument("--seed", type=int, default=0, help="sampling seed")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue a prior run (skip finished quads); reuse the SAME flags",
    )
    args = ap.parse_args()
    assert 0.0 < args.sample_frac <= 1.0, "--sample-frac must be in (0, 1]"
    assert 0.0 <= args.max_cloud <= 1.0, "--max-cloud must be in [0, 1]"
    crosswalks = args.crosswalks or args.src

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.resume:  # fresh: clear prior bookkeeping
        for d in (".ckpt", ".manifest"):
            shutil.rmtree(out / d, ignore_errors=True)

    print("indexing quad blocks + clusters from crosswalks...", flush=True)
    block_idx, clust_idx = read_crosswalks(crosswalks, args.grid_deg)

    tifs = [
        p
        for d in sorted(glob.glob(args.src + "/*"))
        if os.path.isdir(d) and os.path.basename(d).isdigit()
        for p in glob.glob(d + "/*_composite.tif")
    ]
    print(
        f"{len(tifs)} composites; resolving blocks and ordering for locality...",
        flush=True,
    )

    items, unindexed = [], 0
    for p in tifs:
        year, qx, qy = parse_name(p)
        qid = f"{qx}-{qy}"
        block = block_idx.get((year, qid))
        if block is None:  # not in crosswalk -> read header
            try:
                with rasterio.open(p) as s:
                    b = s.bounds
                    lons, lats = warp_transform(
                        s.crs,
                        "EPSG:4326",
                        [(b.left + b.right) / 2],
                        [(b.bottom + b.top) / 2],
                    )
                block = block_label(lats[0], lons[0], args.grid_deg)
                unindexed += 1
            except Exception:
                continue
        items.append((p, block, year, clust_idx.get((year, qid), [])))

    # deterministic order + chunking (must be identical across resume runs)
    items.sort(key=lambda it: (it[1], it[2], it[0]))
    W = max(1, args.workers)
    n = len(items)
    chunks = [items[k * n // W : (k + 1) * n // W] for k in range(W)]
    chunks = [(wid, c) for wid, c in enumerate(chunks) if c]
    mode = "RESUME" if args.resume else "fresh"
    print(
        f"{len(items)} composites ({unindexed} header fallback) -> {len(chunks)} chunks "
        f"[{mode}, max_cloud={args.max_cloud}, sample_frac={args.sample_frac}, "
        f"seed={args.seed}]",
        flush=True,
    )

    all_errors, done = [], 0
    with ProcessPoolExecutor(max_workers=W) as ex:
        futs = [
            ex.submit(
                process_chunk,
                wid,
                c,
                str(out),
                args.shard_size,
                args.min_valid,
                args.max_cloud,
                args.compress,
                args.grid_deg,
                args.sample_frac,
                args.seed,
                args.resume,
            )
            for wid, c in chunks
        ]
        for f in as_completed(futs):
            r = f.result()
            done += 1
            all_errors += r["errors"]
            print(f"  chunk {done}/{len(futs)} done", flush=True)

    # merge per-worker fragments -> manifest.csv, de-duplicated by path
    seen = set()
    with open(out / "manifest.csv", "w", newline="") as mf:
        mw = csv.writer(mf)
        mw.writerow(MANIFEST_COLS)
        for frag in sorted((out / ".manifest").glob("w*.csv")):
            with open(frag, newline="") as ff:
                for row in csv.reader(ff):
                    if row and row[0] not in seen:
                        seen.add(row[0])
                        mw.writerow(row)

    folders = len(list(out.glob("shard_*")))
    (out / "patchify_summary.json").write_text(
        json.dumps(
            {
                "patches": len(seen),
                "folders": folders,
                "shard_size": args.shard_size,
                "avg_per_folder": round(len(seen) / max(folders, 1), 1),
                "grid_deg": args.grid_deg,
                "min_valid": args.min_valid,
                "max_cloud": args.max_cloud,
                "sample_frac": args.sample_frac,
                "seed": args.seed,
                "n_errors": len(all_errors),
                "errors": all_errors[:200],
            },
            indent=2,
        )
    )
    print(
        f"DONE: {len(seen)} patches in {folders} folders "
        f"(~{len(seen) / max(folders, 1):.0f}/folder); {len(all_errors)} read errors.",
        flush=True,
    )


if __name__ == "__main__":
    main()
