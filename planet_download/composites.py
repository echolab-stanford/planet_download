import concurrent.futures
import contextlib
import math
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from shapely.geometry import shape
from tqdm import tqdm


def parse_planet_filesystem(root_dir: str | Path) -> dict[int, dict[str, list[Path]]]:
    """Parse the Planet Downloads file structure to get a dict of quads across time"""

    # Mosaic directories: these are per year quarter/month
    mos_dirs = [d for d in Path(root_dir).iterdir() if d.is_dir()]

    # Create dict of directories by year
    year_directories = {}
    for d in mos_dirs:
        year = int(d.name.split("_")[-2][:4])

        if year not in year_directories:
            year_directories[year] = [d]
        else:
            year_directories[year].append(d)

    # Unpack all paths for each year
    year_quads = {}
    for year, dirs in year_directories.items():
        year_paths_quads = []
        for d in dirs:
            year_paths_quads.append([f for f in d.glob("*.tif")])

        year_quads[year] = [quad for sublist in year_paths_quads for quad in sublist]

    # Create a dict of quads per year where each quad is a dict of paths per period.
    year_quads_files = {}
    for year, quad_paths in year_quads.items():
        year_quads = {}
        unique_quads = list({f.stem for f in quad_paths})
        for quad in unique_quads:
            year_quads[quad] = [p for p in quad_paths if p.stem == quad]

        year_quads_files[year] = year_quads

    return year_quads_files


def create_composite(
    list_rasters: list[Path],
    save_path: str | Path,
    operation="median",
    only_rgb=False,
    overwrite=False,
    dtype: str | None = None,
) -> None:
    """Create a composite raster from a set of raster images (RGB)

    This function will calculate the mean or median composite of a list of quarterly/monthly
    Planet mosaics and save them as COG (Cloud Optimized GeoTIFF) files using the same
    specifications as the original mosaics. This function assumes the images passed to `list_rasters` are all covering the same extent (i.e. they have the same quad planet ID).

    Parameters
    ----------
    list_rasters : list[Path]
        List of raster images to create the composite from.
    save_path : str | Path
        Path to save the composite raster.
    operation : str, optional
        Operation to perform on the rasters, either "mean" or "median". Default is "median".
    only_rgb : bool, optional
        Whether to take only the RGB bands for the composite. Default is False (takes all bands).
    overwrite : bool, optional
        Whether to overwrite the output file if it already exists. Default is False.
    dtype : str, optional
        Output dtype to cast the composite to before writing (e.g. "uint8").
        ``mean``/``median`` produce floats; passing "uint8" rounds and clips the
        values to ``[0, 255]`` so RGB basemaps are stored compactly. Default is
        None (keep the computed float dtype).

    Returns
    -------
    None
    """

    # Define name of the output file
    unique_name = list({p.stem for p in list_rasters})

    if len(unique_name) > 1:
        warnings.warn(
            f"Multiple unique names found in the list of rasters: {unique_name}. "
            "Using the first one for the output file name."
        )

    unique_name = unique_name[0]

    output_file = Path(save_path) / f"{unique_name}_composite.tif"

    if output_file.exists() and not overwrite:
        return

    bbox: list = []

    arr_agg: list[np.ndarray] = []
    arr_meta: list[dict] = []
    failed_files: list[Path] = []

    for idx, quad in enumerate(list_rasters):
        try:
            with rasterio.open(quad, "r") as src:
                if idx == 0:
                    bbox = list(src.bounds)
                elif idx > 0 and bbox == list(src.bounds):
                    bbox = list(src.bounds)
                else:
                    raise ValueError(
                        f"All rasters must cover the same extent. Raster {quad} has different bounds: {src.bounds} compared to {bbox}"
                    )

                # Take all the bands. We default to only take RGB for composites.
                arr = src.read()
                arr_agg.append(arr)
                arr_meta.append(src.profile)
        except Exception as e:
            print(f"ERROR: Failed to read {quad}: {e}")
            failed_files.append(quad)
            continue

    # Check if we have any valid files to process
    if len(arr_agg) == 0:
        print(
            f"WARNING: No valid rasters found for {unique_name}. Skipping composite creation."
        )
        return

    # Print summary of failed files if any
    if failed_files:
        print(f"Warning: {len(failed_files)} file(s) failed for {unique_name}:")
        for failed_file in failed_files:
            print(f"  - {failed_file}")

    # Stack the arrays and calculate the mean or median
    if operation == "mean":
        arr = np.mean(np.stack(arr_agg, axis=0), axis=0)
    elif operation == "median":
        arr = np.median(np.stack(arr_agg, axis=0), axis=0)
    else:
        raise ValueError(
            f"Operation must be either 'mean' or 'median', not {operation}"
        )

    if only_rgb:
        # Take only RGB
        arr = arr[0:3, :, :]

    if dtype is not None:
        if np.dtype(dtype).kind in ("u", "i"):
            info = np.iinfo(dtype)
            arr = np.clip(np.rint(arr), info.min, info.max)
        arr = arr.astype(dtype)

    with MemoryFile() as memfile:
        # Re-write the new dtype/band count (`meta` aliases arr_meta[0], so the
        # update below also propagates to the profile passed to memfile.open).
        meta = arr_meta[0]
        meta.update({"dtype": arr.dtype, "count": arr.shape[0]})

        with memfile.open(**arr_meta[0]) as mem:
            mem.write(arr)

            dst_profile = cog_profiles.get("deflate")
            cog_translate(
                mem,
                output_file,
                dst_profile,
                in_memory=True,
                quiet=True,
            )


def process_composites_parallel(
    dict_paths: dict[int, dict[str, list[Path]]],
    save_dir: str | Path,
    operation="mean",
    overwrite=False,
    nthreads: int = 4,
    only_rgb: bool = True,
    dtype: str | None = None,
) -> None:
    """
    Process composites in parallel for each quad in the dictionary.

    Parameters
    ----------
    d : dict[int, dict[str, list[Path]]]
        Dictionary generated by `parse_planet_filesystem`.
    save_dir : str | Path
        Directory to save the composite files.
    operation : str, optional
        Operation to perform on the rasters, either "mean" or "median". Default is "mean".

    Returns
    -------
    None
    """

    # Create save directory if it doesn't exist
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    for year, quads in dict_paths.items():
        # Add a year folder to the save dir
        year_save_dir = save_dir / str(year)
        year_save_dir.mkdir(parents=True, exist_ok=True)

        def process_quad(quad_name: str, quad_paths: list[Path]) -> None:
            """Process a single quad by creating a composite."""
            create_composite(
                quad_paths,
                year_save_dir,
                operation=operation,
                overwrite=overwrite,
                only_rgb=only_rgb,
                dtype=dtype,
            )

        tasks = [(quad_name, quad_paths) for quad_name, quad_paths in quads.items()]

        with tqdm(
            total=len(tasks), desc=f"Processing quads {year} -> composites"
        ) as pbar:
            # Use ThreadPoolExecutor for parallel processing
            with concurrent.futures.ThreadPoolExecutor(nthreads) as executor:
                futures = [
                    executor.submit(process_quad, quad_name, quad_paths)
                    for quad_name, quad_paths in tasks
                ]

                # Wait for all tasks to complete
                for future in concurrent.futures.as_completed(futures):
                    future.result()
                    pbar.update(1)

    return None


def resample_resolution(
    input_path: Path, output_path: Path, target_resolution: float
) -> None:
    """
    Resample a raster to a new resolution.

    Parameters
    ----------
    input_path : Path
        Path to the input raster file.
    output_path : Path
        Path to the output raster file.
    target_resolution : float
        Target resolution in the same units as the input raster.

    Returns
    -------
    None
    """

    # Let's assume equal resolution (square!)
    xres, yres = (
        target_resolution,
        target_resolution,
    )

    with rasterio.open(input_path, "r") as dataset:
        scale_factor_x = dataset.res[0] / xres
        scale_factor_y = dataset.res[1] / yres

        profile = dataset.profile.copy()
        # resample data to target shape
        data = dataset.read(
            out_shape=(
                dataset.count,
                int(dataset.height * scale_factor_y),
                int(dataset.width * scale_factor_x),
            ),
            resampling=Resampling.bilinear,
        )

        # scale image transform
        transform = dataset.transform * dataset.transform.scale(
            (1 / scale_factor_x), (1 / scale_factor_y)
        )
        profile.update(
            {
                "height": data.shape[-2],
                "width": data.shape[-1],
                "transform": transform,
            }
        )

    with rasterio.open(output_path, "w", **profile) as dataset:
        dataset.write(data)

    return None


def resample_resolution_parallel(
    path_rasters: str | Path,
    path_save_rasters: str | Path,
    target_resolution: float,
    nthreads: int,
) -> None:
    """Process the resample resolution function in parallel

    Execute the resample_resolution function for each raster in parallel using
    a list of GeoTIFFs and saving them using the same name in another folder.

    Parameters:
    -----------
    path_rasters : list[Path]
        List of input raster file paths.
    path_save_rasters : Path
        Directory to save the output raster files.
    target_resolution : float
        Target resolution for the output rasters.
    nthreads : int
        Number of threads to use for parallel processing.

    Returns:
    --------
        None
    """

    # Create saving folder if doesn't exist
    Path(path_save_rasters).mkdir(parents=True, exist_ok=True)

    # Convert to Path and get list of raster files
    path_rasters = Path(path_rasters)
    raster_files = list(path_rasters.glob("**/*.tif"))

    with tqdm(
        total=len(raster_files), desc=f"Taking to {target_resolution} meters"
    ) as pbar:
        with concurrent.futures.ProcessPoolExecutor(max_workers=nthreads) as executor:
            futures = []
            for input_raster in raster_files:
                # Keep the same structure including composites by year. We expect a
                # <path>/year/<composite_name>.tif
                input_raster = Path(input_raster)
                output_folder = Path(path_save_rasters) / input_raster.parts[-2]
                output_folder.mkdir(parents=True, exist_ok=True)
                output_raster = output_folder / input_raster.name
                futures.append(
                    executor.submit(
                        resample_resolution,
                        input_raster,
                        output_raster,
                        target_resolution,
                    )
                )

            for future in concurrent.futures.as_completed(futures):
                future.result()
                pbar.update(1)

    return None


def merge_composites_by_date(
    path_to_composites: str | Path,
    save_path: str | Path,
    year: None | int = None,
    out_prefix: str = "planet_composite_mosaic",
    compress: str | None = "deflate",
    tiled: bool = True,
    blocksize: int = 256,
) -> None:
    """Merge overlapping yearly/monthly composites

    Each subdirectory of ``path_to_composites`` (one per year/period) is mosaicked
    into a single ``<out_prefix>_<period>.tif``. The output is written tiled and
    DEFLATE-compressed by default so the merged mosaic matches the COG-style
    layout of the reference basemaps.

    Parameters
    ----------
    path_to_composites : str | Path
        Path to the directory containing the composite TIFF files.
    save_path : str | Path
        Path to the directory where the merged TIFF file will be saved.
    year : None | int
        Year to filter the composites (if None, all years will be processed).
    out_prefix : str, optional
        Filename prefix for the merged mosaic. Default "planet_composite_mosaic".
    compress : str | None, optional
        Raster compression to apply (e.g. "deflate"). None disables compression.
    tiled : bool, optional
        Whether to write internal tiles. Default True.
    blocksize : int, optional
        Internal tile size when ``tiled`` is True. Default 256.

    Returns
    -------
        None
    """

    if isinstance(path_to_composites, str):
        path_to_composites = Path(path_to_composites)

    comp_time_folder = [d for d in path_to_composites.iterdir() if d.is_dir()]
    if year is not None:
        comp_time_folder = [d for d in comp_time_folder if str(year) in d.name]

    output_dir = Path(save_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    for time_dir in tqdm(comp_time_folder, desc="Merging composites..."):
        paths_month = list(time_dir.glob("*.tif"))
        if not paths_month:
            continue
        tiff_files_open = [rasterio.open(p) for p in paths_month]

        try:
            # Merge the TIFF files
            mosaic, out_trans = merge(tiff_files_open)

            output_file = output_dir / f"{out_prefix}_{time_dir.name}.tif"

            # Use first element meta
            out_meta = tiff_files_open[0].meta.copy()

            out_meta.update(
                {
                    "driver": "GTiff",
                    "height": mosaic.shape[1],
                    "width": mosaic.shape[2],
                    "dtype": mosaic.dtype,
                    "crs": tiff_files_open[0].crs,
                    "transform": out_trans,
                }
            )
            if compress is not None:
                out_meta["compress"] = compress
            if tiled:
                out_meta.update(
                    {"tiled": True, "blockxsize": blocksize, "blockysize": blocksize}
                )

            with rasterio.open(output_file, "w", **out_meta) as dst:
                dst.write(mosaic)
        finally:
            for src in tiff_files_open:
                src.close()
    return None


def find_scene_files(year_dir: str | Path) -> list[tuple[Path, Path]]:
    """Pair downloaded PSScene SR images with their udm2 masks for one year.

    Order deliveries are nested (``<year>/<order_id>/PSScene/...``), and tool
    chains suffix the filenames (e.g. ``_harmonized_clip``), so files are
    paired by their scene id: everything before the ``_3B_`` marker.

    Parameters
    ----------
    year_dir : str | Path
        A per-year directory written by ``scenes.order_scenes``.

    Returns
    -------
    list[tuple[Path, Path]]
        (sr_path, udm2_path) per scene that has both files.
    """
    images: dict[str, Path] = {}
    masks: dict[str, Path] = {}

    for path in Path(year_dir).rglob("*_3B_*.tif"):
        scene_id = path.name.split("_3B_")[0]
        if "udm2" in path.name:
            masks[scene_id] = path
        elif "AnalyticMS" in path.name:
            images[scene_id] = path

    missing = set(images) ^ set(masks)
    if missing:
        print(f"WARNING: {len(missing)} scene(s) missing an SR or udm2 file, skipped.")

    return [(images[sid], masks[sid]) for sid in sorted(set(images) & set(masks))]


def _aoi_grid(region: dict, resolution: float) -> dict:
    """Build the target grid (rasterio profile pieces) for an AOI.

    The grid covers the AOI bounds in its estimated UTM zone at the requested
    resolution. Scenes are warped onto this grid so they can be stacked even
    though every PSScene footprint (and possibly UTM zone) differs.
    """
    aoi = gpd.GeoSeries([shape(region)], crs="EPSG:4326")
    crs = aoi.estimate_utm_crs()
    minx, miny, maxx, maxy = aoi.to_crs(crs).total_bounds

    return {
        "crs": rasterio.crs.CRS.from_user_input(crs),
        "transform": from_origin(minx, maxy, resolution, resolution),
        "width": math.ceil((maxx - minx) / resolution),
        "height": math.ceil((maxy - miny) / resolution),
    }


def mask_scene(
    sr_path: str | Path,
    udm2_path: str | Path,
    out_path: str | Path,
    overwrite: bool = False,
) -> None:
    """Write a udm2-cloud-masked copy of one PSScene SR image.

    Pixels that the udm2 mask does not flag as "clear" (band 1 == 1, i.e. any
    cloud, shadow, haze, or snow) are set to the nodata value 0, so the masked
    image contains only usable surface-reflectance pixels.

    Parameters
    ----------
    sr_path : str | Path
        Surface-reflectance scene (``*_AnalyticMS_SR*.tif``).
    udm2_path : str | Path
        Matching usable-data mask (``*_udm2*.tif``).
    out_path : str | Path
        Output masked GeoTIFF (4-band B, G, R, NIR uint16, nodata 0).
    overwrite : bool, optional
        Whether to overwrite an existing output. Default False.
    """
    out_path = Path(out_path)
    if out_path.exists() and not overwrite:
        return

    with rasterio.open(sr_path) as sr, rasterio.open(udm2_path) as udm2:
        profile = sr.profile.copy()
        data = sr.read()
        clear = udm2.read(1)

    # udm2 is occasionally 1 px larger/smaller than the image; crop both to the
    # shared top-left overlap (the transform origin is unchanged, so this stays
    # georeferenced).
    height = min(data.shape[1], clear.shape[0])
    width = min(data.shape[2], clear.shape[1])
    data = data[:, :height, :width]
    clear = clear[:height, :width]

    data[:, clear != 1] = 0

    profile.update(
        driver="GTiff",
        height=height,
        width=width,
        nodata=0,
        compress="deflate",
        tiled=True,
        blockxsize=512,
        blockysize=512,
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(data)
        for band, name in enumerate(["blue", "green", "red", "nir"], start=1):
            dst.set_band_description(band, name)


def process_scene_masks(
    data_dir: str | Path,
    save_dir: str | Path,
    overwrite: bool = False,
) -> None:
    """Apply the udm2 cloud mask to every downloaded SR scene, per year.

    Reads the ``<data_dir>/<year>/`` scene downloads and writes masked copies
    to ``<save_dir>/<year>/<scene_id>_masked.tif`` — the layout
    ``process_scene_composites`` expects.

    Parameters
    ----------
    data_dir : str | Path
        Directory of per-year scene downloads (from ``scenes.order_scenes``).
    save_dir : str | Path
        Directory to write the masked scenes.
    overwrite : bool, optional
        Whether to overwrite existing masked files. Default False.
    """
    save_dir = Path(save_dir)

    year_dirs = sorted(
        d for d in Path(data_dir).iterdir() if d.is_dir() and d.name.isdigit()
    )
    for year_dir in year_dirs:
        scene_pairs = find_scene_files(year_dir)
        out_year = save_dir / year_dir.name
        out_year.mkdir(parents=True, exist_ok=True)
        for sr_path, udm2_path in tqdm(scene_pairs, desc=f"Masking {year_dir.name}"):
            scene_id = sr_path.name.split("_3B_")[0]
            mask_scene(
                sr_path,
                udm2_path,
                out_year / f"{scene_id}_masked.tif",
                overwrite=overwrite,
            )

    return None


def create_scene_composite(
    masked_paths: list[Path],
    output_file: str | Path,
    region: dict,
    resolution: float = 3.0,
    operation: str = "median",
    overwrite: bool = False,
    blocksize: int = 1024,
) -> None:
    """Composite pre-masked PSScene SR images into one image over the AOI.

    Each masked scene (clouds already set to nodata 0 by ``mask_scene``) is
    warped onto a common UTM grid over the AOI, then aggregated per pixel with
    a nodata-aware mean/median. The composite is computed in ``blocksize``
    windows so memory stays bounded regardless of AOI size.

    Parameters
    ----------
    masked_paths : list[Path]
        Masked SR scenes (``*_masked.tif`` from ``process_scene_masks``).
    output_file : str | Path
        Output GeoTIFF path (4-band B, G, R, NIR uint16, nodata 0).
    region : dict
        GeoJSON geometry (WGS84) of the AOI defining the output grid.
    resolution : float, optional
        Output pixel size in meters. Default 3.0 (PSScene native).
    operation : str, optional
        "mean" or "median". Default "median".
    overwrite : bool, optional
        Whether to overwrite an existing output file. Default False.
    blocksize : int, optional
        Processing window size in pixels. Default 1024.
    """
    output_file = Path(output_file)
    if output_file.exists() and not overwrite:
        return

    if not masked_paths:
        print(f"WARNING: no scenes found for {output_file.name}. Skipping.")
        return

    if operation == "mean":
        aggregate = np.nanmean
    elif operation == "median":
        aggregate = np.nanmedian
    else:
        raise ValueError(
            f"Operation must be either 'mean' or 'median', not {operation}"
        )

    grid = _aoi_grid(region, resolution)
    profile = {
        "driver": "GTiff",
        "count": 4,
        "dtype": "uint16",
        "nodata": 0,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        **grid,
    }

    with contextlib.ExitStack() as ctx:
        # Open each scene as a VRT on the target grid, and record the grid
        # pixel window its footprint spans. Each PSScene covers only a few
        # percent of the AOI, so per tile we read just the handful of scenes
        # that overlap it instead of all of them.
        vrts = []
        scene_windows = []
        for path in masked_paths:
            src = ctx.enter_context(rasterio.open(path))
            bounds = transform_bounds(src.crs, grid["crs"], *src.bounds)
            win = from_bounds(*bounds, transform=grid["transform"])
            scene_windows.append(
                (
                    max(0, math.floor(win.row_off)),
                    min(grid["height"], math.ceil(win.row_off + win.height)),
                    max(0, math.floor(win.col_off)),
                    min(grid["width"], math.ceil(win.col_off + win.width)),
                )
            )
            vrts.append(
                ctx.enter_context(
                    WarpedVRT(src, resampling=Resampling.bilinear, **grid)
                )
            )

        dst = ctx.enter_context(rasterio.open(output_file, "w", **profile))
        for band, name in enumerate(["blue", "green", "red", "nir"], start=1):
            dst.set_band_description(band, name)

        row_offs = range(0, grid["height"], blocksize)
        col_offs = range(0, grid["width"], blocksize)
        n_windows = len(row_offs) * len(col_offs)

        with tqdm(total=n_windows, desc=f"Compositing {output_file.name}") as pbar:
            for row_off in row_offs:
                for col_off in col_offs:
                    pbar.update(1)
                    bh = min(blocksize, grid["height"] - row_off)
                    bw = min(blocksize, grid["width"] - col_off)

                    # Scenes whose footprint overlaps this tile.
                    overlap = [
                        vrts[i]
                        for i, (r0, r1, c0, c1) in enumerate(scene_windows)
                        if r0 < row_off + bh
                        and r1 > row_off
                        and c0 < col_off + bw
                        and c1 > col_off
                    ]
                    if not overlap:
                        # No coverage here; tile stays nodata (0).
                        continue

                    window = Window(col_off, row_off, bw, bh)
                    stack = []
                    for vrt in overlap:
                        arr = vrt.read(indexes=[1, 2, 3, 4], window=window).astype(
                            "float32"
                        )
                        # nodata (masked clouds, blackfill, outside footprint)
                        arr[arr == 0] = np.nan
                        stack.append(arr)

                    with warnings.catch_warnings():
                        # All-NaN pixels (no clear view) are expected; they
                        # become nodata below.
                        warnings.simplefilter("ignore", RuntimeWarning)
                        composite = aggregate(np.stack(stack), axis=0)

                    composite = np.nan_to_num(composite)
                    composite = np.clip(np.rint(composite), 0, 65535)
                    dst.write(composite.astype("uint16"), window=window)


def process_scene_composites(
    masked_dir: str | Path,
    save_dir: str | Path,
    region: dict,
    resolution: float = 3.0,
    operation: str = "median",
    overwrite: bool = False,
    out_prefix: str = "planet_ps_composite",
) -> None:
    """Create one PSScene composite per year of masked scenes.

    Parameters
    ----------
    masked_dir : str | Path
        Directory of per-year masked scenes (from ``process_scene_masks``).
    save_dir : str | Path
        Directory to write ``<out_prefix>_<year>.tif`` files.
    region : dict
        GeoJSON geometry (WGS84) of the AOI defining the output grid.
    resolution, operation, overwrite :
        Passed through to ``create_scene_composite``.
    out_prefix : str, optional
        Output filename prefix. Default "planet_ps_composite".
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    year_dirs = sorted(
        d for d in Path(masked_dir).iterdir() if d.is_dir() and d.name.isdigit()
    )
    for year_dir in year_dirs:
        masked_paths = sorted(year_dir.glob("*_masked.tif"))
        print(f"{year_dir.name}: {len(masked_paths)} scene(s)")
        create_scene_composite(
            masked_paths,
            save_dir / f"{out_prefix}_{year_dir.name}.tif",
            region,
            resolution=resolution,
            operation=operation,
            overwrite=overwrite,
        )

    return None
