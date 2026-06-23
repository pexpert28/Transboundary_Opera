#!/usr/bin/env python3
"""
run_cog_mosaic.py -- Phase 4: mosaic per-frame rasters into aquifer-wide COGs.

MVP scope: VELOCITY ONLY. Reads each frame's mintpy/velocity.h5, reprojects all
frames to a common CRS, merges them into one raster, clips to the aquifer
boundary, and writes a Cloud-Optimized GeoTIFF: velocity_mosaic_cog.tif.

It is deliberately structured so the vertical/horizontal decomposition products
(dhorz/dvert) can be added later as additional entries in the PRODUCTS registry
once the velocity path is validated -- see the TODO block near PRODUCTS.

Exit codes:
  0 = success (a COG was written)
  2 = nothing to do (no input rasters found)        -> job skips upload cleanly
  1 = error

Inputs are staged by cog_job.sh under --aquifer-dir as:
    <aquifer-dir>/<frame>/mintpy/velocity.h5

Usage (matches cog_job.sh):
    python run_cog_mosaic.py \
        --aquifer-code N015 \
        --aquifer-dir  /work/N015 \
        --shapefile    /work/shapefiles/TBA_full.shp \
        --outdir       /work/N015/cog
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

# Heavy geo deps are imported lazily inside functions so that --help and import
# work even where the full stack isn't installed.


# ── Product registry ──────────────────────────────────────────────────────────
# A product describes one mosaic to build. Velocity is implemented now. When the
# velocity path is validated, add vertical/horizontal by registering entries that
# point at the dhorz/dvert pairs and supply a reducer that collapses the 3D
# timeseries to a 2D summary (e.g. cumulative = last - first). The build pipeline
# (reproject -> merge -> clip -> COG) is product-agnostic and won't need changes.

@dataclass
class Product:
    name: str                 # short id, e.g. "velocity"
    out_name: str             # output COG filename
    source_glob: str          # glob under aquifer-dir to find source .h5 files
    dataset: str              # HDF5 dataset name to read
    reducer: Callable[[np.ndarray], np.ndarray]  # source array -> 2D float array


def _identity_2d(arr: np.ndarray) -> np.ndarray:
    """Velocity is already a 2D (length, width) field."""
    return np.asarray(arr, dtype="float32")


# def _cumulative(arr: np.ndarray) -> np.ndarray:
#     """TODO (phase 2): dhorz/dvert are 3D (date, y, x); cumulative = last - first."""
#     a = np.asarray(arr, dtype="float32")
#     return a[-1] - a[0]


PRODUCTS = {
    "velocity": Product(
        name="velocity",
        out_name="velocity_mosaic_cog.tif",
        source_glob="*/mintpy/velocity.h5",
        dataset="velocity",
        reducer=_identity_2d,
    ),
    # TODO (phase 2 — after velocity is validated):
    # "vertical": Product("vertical", "vertical_mosaic_cog.tif",
    #                     "*_dvert.h5", "timeseries", _cumulative),
    # "horizontal": Product("horizontal", "horizontal_mosaic_cog.tif",
    #                       "*_dhorz.h5", "timeseries", _cumulative),
}


# ── Raster IO ─────────────────────────────────────────────────────────────────

def _read_mintpy_raster(path: Path, dataset: str, reducer):
    """Read a MintPy HDF5 raster into a CRS-aware rioxarray DataArray.

    The geotransform lives in MintPy file attributes (X_FIRST/Y_FIRST/X_STEP/
    Y_STEP/EPSG), not in an embedded CRS, so we reconstruct coordinates from them.
    """
    import h5py
    import xarray as xr
    import rioxarray  # noqa: F401  (registers the .rio accessor)

    with h5py.File(path, "r") as f:
        if dataset not in f:
            raise KeyError(f"{path}: dataset '{dataset}' not found")
        raw = f[dataset][()]
        attrs = {k: (v.decode() if isinstance(v, bytes) else v)
                 for k, v in f.attrs.items()}

    data = reducer(raw)
    if data.ndim != 2:
        raise ValueError(f"{path}: expected 2D after reduction, got {data.shape}")

    try:
        x_first = float(attrs["X_FIRST"]); y_first = float(attrs["Y_FIRST"])
        x_step = float(attrs["X_STEP"]);   y_step = float(attrs["Y_STEP"])
        epsg = int(float(attrs["EPSG"]))
    except KeyError as e:
        raise KeyError(f"{path}: missing geotransform attr {e}")

    ny, nx = data.shape
    # pixel-centre coordinates
    xs = x_first + (np.arange(nx) + 0.5) * x_step
    ys = y_first + (np.arange(ny) + 0.5) * y_step

    da = xr.DataArray(data, coords={"y": ys, "x": xs}, dims=("y", "x"))
    da = da.rio.write_crs(f"EPSG:{epsg}")
    # Treat NaN as nodata. (A genuine 0.0 velocity is valid, so we do NOT mask 0.)
    da = da.rio.write_nodata(np.nan, encoded=False)
    return da


def _build_mosaic(arrays, dst_crs: str, resolution: Optional[float]):
    """Reproject all arrays to dst_crs and merge into one raster.

    NOTE (phase 3): overlap handling is currently the rioxarray default (first
    non-nodata wins). Per-frame reference points differ, so seams may appear in
    overlap zones; a coherence-weighted merge + reference reconciliation is the
    planned upgrade. The merge call is isolated here so that change is localized.
    """
    from rioxarray.merge import merge_arrays

    reproj = []
    for da in arrays:
        r = da.rio.reproject(dst_crs)
        reproj.append(r)

    if resolution is not None:
        mosaic = merge_arrays(reproj, res=(resolution, resolution))
    else:
        mosaic = merge_arrays(reproj)
    return mosaic


def _clip_to_aquifer(mosaic, shapefile: Path, aquifer_code: str, dst_crs: str):
    """Clip the mosaic to the aquifer polygon. Returns mosaic unchanged if the
    shapefile is missing (clip is best-effort for the MVP)."""
    if not Path(shapefile).exists():
        print(f"  WARNING: shapefile not found ({shapefile}); skipping clip.")
        return mosaic

    import geopandas as gpd

    gdf = gpd.read_file(shapefile)
    if "CODE_2021" not in gdf.columns:
        print("  WARNING: 'CODE_2021' not in shapefile; skipping clip.")
        return mosaic

    sel = gdf[gdf["CODE_2021"] == aquifer_code]
    if sel.empty:
        print(f"  WARNING: aquifer {aquifer_code} not in shapefile; skipping clip.")
        return mosaic

    sel = sel.to_crs(dst_crs)
    return mosaic.rio.clip(sel.geometry.values, sel.crs, drop=True, all_touched=True)


def _write_cog(mosaic, out_path: Path):
    """Write a Cloud-Optimized GeoTIFF atomically (temp file -> rename), so a
    crash never leaves a half-written file for the job to upload."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".tif", dir=str(out_path.parent))
    os.close(fd)
    try:
        mosaic.rio.to_raster(
            tmp,
            driver="COG",
            dtype="float32",
            compress="DEFLATE",
            num_threads="all_cpus",
            overview_resampling="average",
        )
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def build_product(product: Product, aquifer_dir: Path, aquifer_code: str,
                  shapefile: Path, outdir: Path, dst_crs: str,
                  resolution: Optional[float]) -> Optional[Path]:
    """Build one product's COG. Returns the output path, or None if no inputs."""
    sources = sorted(Path(p) for p in glob.glob(str(aquifer_dir / product.source_glob)))
    if not sources:
        print(f"[{product.name}] no source rasters under {aquifer_dir}/{product.source_glob}")
        return None

    print(f"[{product.name}] {len(sources)} source raster(s):")
    arrays = []
    for s in sources:
        try:
            arrays.append(_read_mintpy_raster(s, product.dataset, product.reducer))
            print(f"    + {s.parent.parent.name}/{s.name}")
        except Exception as e:  # noqa: BLE001 -- skip unreadable frames, keep going
            print(f"    ! skipping {s} ({e})")

    if not arrays:
        print(f"[{product.name}] no readable rasters.")
        return None

    print(f"[{product.name}] reprojecting to {dst_crs} and merging...")
    mosaic = _build_mosaic(arrays, dst_crs, resolution)

    print(f"[{product.name}] clipping to aquifer {aquifer_code}...")
    mosaic = _clip_to_aquifer(mosaic, shapefile, aquifer_code, dst_crs)

    out_path = outdir / product.out_name
    print(f"[{product.name}] writing COG -> {out_path}")
    _write_cog(mosaic, out_path)
    print(f"[{product.name}] done ({out_path.stat().st_size/1e6:.1f} MB)")
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aquifer-code", required=True)
    ap.add_argument("--aquifer-dir", required=True, type=Path)
    ap.add_argument("--shapefile", required=True, type=Path)
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--products", nargs="+", default=["velocity"],
                    choices=sorted(PRODUCTS.keys()),
                    help="which products to build (only 'velocity' is wired in the MVP)")
    ap.add_argument("--dst-crs", default="EPSG:4326",
                    help="target CRS for the mosaic (default geographic, web-friendly)")
    ap.add_argument("--resolution", type=float, default=None,
                    help="target pixel size in dst-crs units (default: from first frame)")
    args = ap.parse_args(argv)

    args.outdir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for name in args.products:
        product = PRODUCTS[name]
        try:
            out = build_product(product, args.aquifer_dir, args.aquifer_code,
                                 args.shapefile, args.outdir, args.dst_crs,
                                 args.resolution)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] ERROR: {e}")
            return 1
        if out is not None:
            written.append(out)

    if not written:
        print("Nothing produced (no input rasters found).")
        return 2

    print(f"\nWrote {len(written)} COG(s):")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())