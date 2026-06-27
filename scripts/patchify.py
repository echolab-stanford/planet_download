"""Extract non-overlapping 512x512 RGB patches from Planet quads as plain PNG
files packed into uniformly-sized shard folders, with a single manifest.csv.

Designed for an UNSUPERVISED training pipeline (no train/val/test split). Output:

    <out>/shard_<WW>_<NNNNN>/<key>.png   each folder holds <= --shard-size PNGs
        e.g. out/shard_03_00012/N10W006_2017q1_1986_2167_0000.png
    <out>/manifest.csv                   one row per patch (provenance + clusters)

Folder sizing:
- Folders are filled to EXACTLY --shard-size patches (default 512) then rolled,
  so file counts per folder are constant. Patches are ordered by
  (block, year-quarter, quad), so each folder stays geographically coherent;
  only the last folder of each worker is partial.
- Geography is in manifest.csv and in each PNG filename
  (<BLOCK>_<YYYYqQ>_<x>_<y>_<rowcol>), not the folder name.

Sampling (--sample-frac < 1.0):
- Keep a patch iff a stable hash of its key is below the fraction. This is
  DETERMINISTIC (independent of worker/run), so a resumed run selects exactly
  the same patches.

Resume (--resume):
- Each worker checkpoints (quads_done, patches_written) after every quad and
  streams its manifest rows to a durable per-worker fragment under <out>/.manifest/.
- On --resume, a worker skips already-finished quads (no re-read) and continues
  the folder numbering exactly where it left off, so paths are identical.
- The final manifest.csv is merged from the fragments and de-duplicated by path,
  so a crash mid-quad cannot create duplicate rows.
- IMPORTANT: a resumed run must use the SAME --workers/--grid-deg/--shard-size/
  --sample-frac/--seed as the original (the chunking + selection must match).

Other choices:
- The patch grid IS the COG internal 512x512 block grid, so reads decode nothing
  extra. 4096px quads -> 64 patches; 2048px quads (2017 Q1/Q2) -> 16 patches.
- Each quad is opened ONCE and read whole; patches come from one vectorized reshape.
- RGB bands are kept; the 4th (alpha) band only drops near-empty patches.

Block + cluster lookups come from crosswalk_quads_planet_*.geojson.

Usage:
    python patchify.py --src $SCRATCH/planet_dhs_sa_africa \\
                       --out $SCRATCH/planet_patches \\
                       --workers 48 --grid-deg 1 --shard-size 512 --min-valid 0.5 \\
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
MANIFEST_COLS = [
    "path",
    "block",
    "lat",
    "lon",
    "year",
    "quarter",
    "quad_x",
    "quad_y",
    "row",
    "col",
    "mosaic",
    "valid_frac",
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
    """('global_quarterly_2020q1_mosaic', '957_1173.tif') -> (2020, 'q1', 957, 1173)."""
    p = Path(path)
    tok = p.parent.name.split("_")[-2]  # 2020q1
    return int(tok[:4]), tok[4:], *(int(v) for v in p.stem.split("_"))


def keep_sample(key, seed, frac):
    """Deterministic, stateless sampler: True for ~frac of keys, stable across runs."""
    if frac >= 1.0:
        return True
    h = hashlib.blake2b(f"{seed}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64 < frac


def read_crosswalks(src, grid_deg):
    """Return (block_index, cluster_index) keyed by (mosaic_name, 'x-y')."""
    import geopandas as gpd

    block_idx, clust_idx = {}, defaultdict(list)
    for f in sorted(glob.glob(f"{src}/crosswalk_quads_planet_*.geojson")):
        g = gpd.read_file(f)
        bounds = g.geometry.bounds
        for mosaic, qid, dhsid, clat, clon, miny, maxy, minx, maxx in zip(
            g["mosaic_name"],
            g["id"],
            g["DHSID_EA"],
            g["lat"],
            g["lon"],
            bounds.miny,
            bounds.maxy,
            bounds.minx,
            bounds.maxx,
        ):
            key = (mosaic, qid)
            if key not in block_idx:
                block_idx[key] = block_label(
                    (miny + maxy) / 2, (minx + maxx) / 2, grid_deg
                )
            clust_idx[key].append((dhsid, float(clat), float(clon)))
    return block_idx, clust_idx


def patches_for(path, min_valid, clusters):
    """Open one quad; yield (iy, ix, rgb HxWx3, lat, lon, frac, dhsids)."""
    with rasterio.open(path) as s:
        a = s.read()
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
            rgb = np.ascontiguousarray(p[:3].transpose(1, 2, 0))
            yield iy, ix, rgb, lat, lon, frac, patch_dhs.get((iy, ix), [])


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
    compress,
    grid_deg,
    sample_frac,
    seed,
    resume,
):
    """Process a worker's quads (pre-sorted) into uniform shard folders.

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
        path, block, yq, clusters = items[i]
        try:
            year, quarter, qx, qy = parse_name(path)
            rows = []
            for iy, ix, rgb, lat, lon, frac, dhsids in patches_for(
                path, min_valid, clusters
            ):
                key = f"{block}_{yq}_{qx}_{qy}_{iy:02d}{ix:02d}"
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
                        quarter,
                        qx,
                        qy,
                        iy,
                        ix,
                        Path(path).parent.name,
                        round(frac, 4),
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
        "--src", required=True, help="dataset dir (global_quarterly_*/ + crosswalks)"
    )
    ap.add_argument("--out", required=True, help="output dir (local/SCRATCH)")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument(
        "--grid-deg", type=float, default=1.0, help="lat/lon block size (deg)"
    )
    ap.add_argument("--shard-size", type=int, default=512, help="PNGs per shard folder")
    ap.add_argument(
        "--min-valid", type=float, default=0.5, help="min non-alpha-zero fraction"
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

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.resume:  # fresh: clear prior bookkeeping
        for d in (".ckpt", ".manifest"):
            shutil.rmtree(out / d, ignore_errors=True)

    print("indexing quad blocks + clusters from crosswalks...", flush=True)
    block_idx, clust_idx = read_crosswalks(args.src, args.grid_deg)

    tifs = [
        p
        for d in sorted(glob.glob(args.src + "/global_quarterly_*"))
        for p in glob.glob(d + "/*.tif")
    ]
    print(
        f"{len(tifs)} quads; resolving blocks and ordering for locality...", flush=True
    )

    items, unindexed = [], 0
    for p in tifs:
        mosaic, qid = (
            os.path.basename(os.path.dirname(p)),
            Path(p).stem.replace("_", "-"),
        )
        block = block_idx.get((mosaic, qid))
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
        tok = mosaic.split("_")[-2]
        items.append((p, block, tok[:4] + tok[4:], clust_idx.get((mosaic, qid), [])))

    # deterministic order + chunking (must be identical across resume runs)
    items.sort(key=lambda it: (it[1], it[2], it[0]))
    W = max(1, args.workers)
    n = len(items)
    chunks = [items[k * n // W : (k + 1) * n // W] for k in range(W)]
    chunks = [(wid, c) for wid, c in enumerate(chunks) if c]
    mode = "RESUME" if args.resume else "fresh"
    print(
        f"{len(items)} quads ({unindexed} header fallback) -> {len(chunks)} chunks "
        f"[{mode}, sample_frac={args.sample_frac}, seed={args.seed}]",
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
