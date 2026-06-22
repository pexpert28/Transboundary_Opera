#!/usr/bin/env python3
"""
Process a single InSAR DISP-S1 frame: reformat -> download static data ->
build geometry rasters -> export to MintPy format -> set reference point.

Designed to be called **once per frame** from a shell loop so that each
invocation gets a fresh process and a clean memory space:

    for aquifer in "$DATA_DIR"/*/; do
        for frame in "$aquifer"/*/; do
            python process_frame.py \\
                --data-dir "$DATA_DIR" \\
                --aquifer "$(basename "$aquifer")" \\
                --frame "$(basename "$frame")" \\
                --start-date 20200101 \\
                --end-date 20231231
        done
    done
"""

from __future__ import annotations

import argparse
import gc
import os
import shutil
import sys
import time
from pathlib import Path

import h5py
import numpy as np
import rioxarray
import xarray as xr
import rasterio as rio
from pyproj import Transformer

from transboundary_opera import run1_download_DISP_S1_Static
from opera_utils.disp import _reformat, mintpy

# Defaults
CHUNKING = (32, 256, 256)  # (time, y, x)
PROCESS_CHUNKS = (512, 512)  # internal processing tile size

DROPPED_VARS = [
    "phase_similarity",
    "persistent_scatterer_mask",
    "timeseries_inversion_residuals",
    "connected_component_labels",
    "estimated_phase_quality",
    "shp_counts",
]

REFERENCE_METHOD = "high_coherence"
COHERENCE_THRESHOLD = 0.7
BORDER_PIXELS = 0.3
RELIABILITY_THRESHOLD = 0.7

# When True, write a self-contained (non-virtual) timeseries.h5 so it survives
# the upload/download round-trip to Allas object storage. A virtual file is just
# a pointer to its displacement source; if it is uploaded without that source,
# reads return NaN/0 on the decomposition node while the metadata survives --
# the exact "data empty but reference points present" failure. Set MATERIALIZE_TS=0
# to keep the lighter virtual file for purely in-place local runs.
MATERIALIZE_TIMESERIES = os.environ.get("MATERIALIZE_TS", "1") == "1"

MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds (base; doubles each attempt)


# Helpers
def _cleanup(*objs) -> None:
    """Delete references then run a garbage-collection sweep."""
    for obj in objs:
        try:
            del obj
        except Exception:
            pass
    gc.collect()


def _download_static(
    frame: str,
    start_date: str,
    end_date: str,
    disp_dir: Path,
    static_dir: Path,
    geom_dir: Path,
    max_retries: int = MAX_RETRIES,
    retry_delay: int = RETRY_DELAY,
) -> None:
    """Download DISP-S1 static data with exponential-backoff retries.

    Wraps run1_download_DISP_S1_Static without permanently clobbering
    sys.argv by saving and restoring it.
    """
    saved_argv = sys.argv
    sys.argv = [
        "process_frame",
        "--frameID",
        str(frame),
        "--startDate",
        start_date,
        "--endDate",
        end_date,
        "--dispDir",
        str(disp_dir),
        "--staticDir",
        str(static_dir),
        "--geomDir",
        str(geom_dir),
        "--staticOnly",
    ]
    try:
        for attempt in range(1, max_retries + 1):
            try:
                inps = run1_download_DISP_S1_Static.createParser()
                run1_download_DISP_S1_Static.main(inps)
                return
            except (ConnectionError, OSError) as exc:
                if attempt < max_retries:
                    wait = retry_delay * (2 ** (attempt - 1))
                    print(f"  Warning: attempt {attempt} failed: {exc}")
                    print(f"    Retrying in {wait}s ...")
                    time.sleep(wait)
                else:
                    raise
    finally:
        sys.argv = saved_argv


def _build_los_enu(geom_dir: Path) -> None:
    """Compute the UP component and write a 3-band ENU GeoTIFF."""
    with rio.open(geom_dir / "los_east.tif") as src:
        east = src.read(1).astype(np.float32, copy=False)
        crs = src.crs
        transform = src.transform

    with rio.open(geom_dir / "los_north.tif") as src:
        north = src.read(1).astype(np.float32, copy=False)

    up = np.sqrt(np.clip(1.0 - east**2 - north**2, 0.0, 1.0)).astype(
        np.float32, copy=False
    )

    with rio.open(
        geom_dir / "los_enu.tif",
        "w",
        driver="GTiff",
        height=east.shape[0],
        width=east.shape[1],
        count=3,
        dtype="float32",
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(east, 1)
        dst.write(north, 2)
        dst.write(up, 3)

    _cleanup(east, north, up)


def _clip_geometry_to_frame(frame_nc: Path, geom_dir: Path) -> None:
    """Reproject-match geometry rasters to the DISP frame extent."""
    tifs = ["layover_shadow_mask.tif", "los_enu.tif", "los_east.tif", "los_north.tif"]
    with xr.open_dataset(frame_nc, cache=False) as nc_ds:
        for tif in tifs:
            tif_path = geom_dir / tif
            if not tif_path.exists():
                continue
            with rioxarray.open_rasterio(
                tif_path, chunks={"x": 512, "y": 512}
            ) as tiff_ds:
                clipped = tiff_ds.rio.reproject_match(nc_ds)
                clipped.rio.to_raster(tif_path)
            _cleanup(clipped)


def _set_reference_point(mintpy_dir: str) -> None:
    """Find the highest-coherence pixel and write REF attrs into HDF5 files."""
    with h5py.File(f"{mintpy_dir}/geometryGeo.h5", "r") as geom:
        gattrs = dict(geom.attrs)
    epsg = gattrs["EPSG"]
    x_first = float(gattrs["X_FIRST"])
    y_first = float(gattrs["Y_FIRST"])
    x_step = float(gattrs["X_STEP"])
    y_step = float(gattrs["Y_STEP"])
    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

    with xr.open_dataset(
        f"{mintpy_dir}/avgSpatialCoh.h5", engine="h5netcdf", cache=False
    ) as coh_ds:
        coh_ds = coh_ds.rename({"phony_dim_0": "y", "phony_dim_1": "x"})
        coherence = coh_ds["avgSpatialCoh"].values

    max_flat_idx = int(np.nanargmax(coherence))
    y_idx, x_idx = np.unravel_index(max_flat_idx, coherence.shape)

    x_coord = x_first + x_idx * x_step
    y_coord = y_first + y_idx * y_step

    _cleanup(coherence)

    ref_lon, ref_lat = transformer.transform(x_coord, y_coord)
    attrs = {
        "REF_LAT": float(ref_lat),
        "REF_LON": float(ref_lon),
        "REF_Y": int(y_idx),
        "REF_X": int(x_idx),
    }

    for fname in ["velocity.h5", "timeseries.h5", "geometryGeo.h5"]:
        fpath = f"{mintpy_dir}/{fname}"
        with h5py.File(fpath, "r+") as f:
            f.attrs.update(attrs)
        print(f"  -> Updated {fpath}")
        print(
            f"    REF_LAT={attrs['REF_LAT']:.6f}  REF_LON={attrs['REF_LON']:.6f}  "
            f"REF_Y={attrs['REF_Y']}  REF_X={attrs['REF_X']}"
        )


def _validate_timeseries(mintpy_dir: Path) -> None:
    """Check the just-written timeseries.h5 for the empty / broken-virtual-link
    signature, and warn loudly before it is ever uploaded. Non-fatal: never
    blocks processing if the checker is unavailable."""
    try:
        from transboundary_opera.ts_integrity import inspect_timeseries
    except Exception as exc:  # noqa: BLE001
        print(f"  [4b] integrity check skipped ({exc})")
        return

    rep = inspect_timeseries(mintpy_dir / "timeseries.h5")
    print("  [4b] timeseries integrity: " + rep.one_line())
    if not rep.ok:
        print(
            "       WARNING: timeseries looks empty / virtual-broken. Do NOT "
            "upload as-is -- reads will be NaN on another node. See ts_integrity.py."
        )


# Main pipeline


def process_frame(
    data_dir: Path,
    aquifer: str,
    frame: str,
    start_date: str,
    end_date: str,
) -> None:
    """End-to-end processing for a single frame."""

    out_path = data_dir / aquifer / frame

    # Skip if already done
    if (out_path / "mintpy").exists():
        print(f"  Already completed (mintpy/ exists) -- skipping.")
        return

    # Locate NetCDF inputs
    nc_files = sorted(out_path.glob("*/*.nc"))
    if not nc_files:
        print(f"  No NetCDF files found -- skipping.")
        return

    #  1. Reformat stack
    print("  [1/5] Reformatting displacement stack ...")
    _reformat.reformat_stack(
        input_files=nc_files,
        output_name=str(out_path / f"disp-stack-{out_path.stem}.nc"),
        out_chunks=CHUNKING,
        shard_factors=(1, 4, 4),
        drop_vars=DROPPED_VARS,
        apply_solid_earth_corrections=True,
        apply_ionospheric_corrections=True,
        reference_method=REFERENCE_METHOD,
        reference_coherence_threshold=COHERENCE_THRESHOLD,
        process_chunk_size=PROCESS_CHUNKS,
        do_round=True,
    )
    gc.collect()

    frame_nc = out_path / f"disp-stack-{frame}.nc"

    # 2. Download static / geometry data
    print("  [2/5] Downloading static geometry data ...")
    static_dir = out_path / "orbit_data"
    disp_dir = out_path / "disp_data"
    geom_dir = out_path / "geom_data"
    for d in (static_dir, disp_dir, geom_dir):
        d.mkdir(parents=True, exist_ok=True)

    _download_static(
        frame=frame,
        start_date=start_date,
        end_date=end_date,
        disp_dir=disp_dir,
        static_dir=static_dir,
        geom_dir=geom_dir,
    )

    # 3. Build LOS ENU & clip to frame extent
    print("  [3/5] Building LOS ENU raster & clipping geometry ...")
    _build_los_enu(geom_dir)
    _clip_geometry_to_frame(frame_nc, geom_dir)

    # Clean up the (likely empty) disp_data dir
    try:
        os.rmdir(disp_dir)
    except OSError:
        pass

    # 4. Export to MintPy
    print("  [4/5] Converting to MintPy format ...")
    mintpy.disp_nc_to_mintpy(
        str(out_path / f"disp-stack-{out_path.stem}.nc"),
        sample_disp_nc=nc_files[0],
        los_enu_path=geom_dir / "los_enu.tif",
        dem_path=None,
        layover_shadow_mask_path=geom_dir / "layover_shadow_mask.tif",
        outdir=out_path / "mintpy",
        # virtual=False by default so timeseries.h5 is self-contained and survives
        # the Allas round-trip. See MATERIALIZE_TIMESERIES above.
        virtual=not MATERIALIZE_TIMESERIES,
        reliability_threshold=RELIABILITY_THRESHOLD,
    )
    gc.collect()

    # 4b. Validate the timeseries before relying on / uploading it.
    _validate_timeseries(out_path / "mintpy")

    # 5. Set reference point
    print("  [5/5] Setting reference point from coherence ...")
    _set_reference_point(str(out_path / "mintpy"))

    # Cleanup temp dirs
    for tmp in ("geom_data", "orbit_data", "subset-ncs"):
        shutil.rmtree(out_path / tmp, ignore_errors=True)

    _cleanup()
    print(f"  Done.\n")


# CLI
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Process a single DISP-S1 frame for LOS decomposition.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Root data directory (contains aquifer subdirs).",
    )
    p.add_argument(
        "--aquifer",
        type=str,
        required=True,
        help="Aquifer name (subdirectory of data-dir).",
    )
    p.add_argument("--frame", type=str, required=True, help="Frame ID to process.")
    p.add_argument(
        "--start-date", type=str, required=True, help="Search start date (YYYYMMDD)."
    )
    p.add_argument(
        "--end-date", type=str, required=True, help="Search end date (YYYYMMDD)."
    )
    p.add_argument(
        "--skip-frames",
        nargs="*",
        default=["3067"],
        help="Frame IDs to skip (default: 3067).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.frame in args.skip_frames:
        print(f"Skipping frame {args.frame} (in skip list).")
        return

    print(f"{'=' * 72}")
    print(f" Aquifer: {args.aquifer}  |  Frame: {args.frame}")
    print(f"{'=' * 72}")

    process_frame(
        data_dir=args.data_dir,
        aquifer=args.aquifer,
        frame=args.frame,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()