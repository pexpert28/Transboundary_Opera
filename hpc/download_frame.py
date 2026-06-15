#!/usr/bin/env python3
"""
download_frame.py — Download OPERA DISP-S1 .nc files for a single frame.

Uses opera_utils.disp._download.run_download() — the same method as
get_opera_data.py which has proven to work. This replaces run1 for the
download step, which fails to find v1.1 products via S3 listing.

Downloads are clipped to the aquifer bbox to reduce file sizes.
Output goes to: output_dir/subset-ncs/*.nc
(process_frame.py expects .nc files in a subdirectory via out_path.glob("*/*.nc"))

Usage:
    python hpc/download_frame.py \
        --aquifer  N015 \
        --frame    14877 \
        --output-dir /work/data/N015/14877 \
        --shapefile  /repo/raw_data/TBA_full.shp \
        --start    20220101 \
        --end      20220401 \
        --workers  8
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd


def create_parser():
    p = argparse.ArgumentParser(
        description="Download OPERA DISP-S1 .nc files for one frame"
    )
    p.add_argument("--aquifer",     required=True, help="Aquifer CODE_2021 (e.g. N015)")
    p.add_argument("--frame",       required=True, type=int, help="Sentinel-1 frame ID")
    p.add_argument("--output-dir",  required=True, type=Path,
                   help="Frame root directory (subset-ncs/ created inside it)")
    p.add_argument("--shapefile",   required=True, type=Path,
                   help="Path to TBA_full.shp")
    p.add_argument("--start",       required=True,
                   help="Start date YYYYMMDD")
    p.add_argument("--end",         required=True,
                   help="End date YYYYMMDD")
    p.add_argument("--workers",     default=8, type=int,
                   help="Parallel download workers (default: 8)")
    return p


def main():
    args = create_parser().parse_args()

    from opera_utils.disp import _download
    from transboundary_opera import displacement_tools as dt

    # ── Load shapefile, filter to aquifer ─────────────────────
    gdf = gpd.read_file(args.shapefile)
    gdf_aq = gdf[gdf["CODE_2021"] == args.aquifer]
    if gdf_aq.empty:
        print(f"ERROR: aquifer {args.aquifer} not found in {args.shapefile}")
        sys.exit(1)

    aquifer_geom = gdf_aq.geometry.iloc[0]

    # ── Get frame geometry to compute clipped bbox ────────────
    print(f"Fetching geometry for frame {args.frame}...")
    geom_frames = dt.get_frame_geometries(
        [args.frame],
        gdf_bounds=aquifer_geom.bounds
    )

    if geom_frames.empty:
        print(f"WARNING: no geometry found for frame {args.frame} — skipping")
        sys.exit(0)

    # Clip aquifer polygon to this frame's footprint
    frame_geom = geom_frames[geom_frames["frame_id"] == args.frame]
    clipped = gpd.clip(
        gpd.GeoSeries([aquifer_geom], crs=gdf.crs),
        frame_geom
    )

    if clipped.empty:
        print(f"No spatial overlap between {args.aquifer} and frame {args.frame} — skipping")
        sys.exit(0)

    clipped_bbox = clipped.geometry.iloc[0].bounds
    print(f"Clipped bbox (minx, miny, maxx, maxy): {[round(x,4) for x in clipped_bbox]}")

    # ── Set up output paths ────────────────────────────────────
    output_dir  = args.output_dir
    subset_dir  = output_dir / "subset-ncs"
    done_marker = output_dir / ".download_complete"

    if done_marker.exists():
        nc_count = len(list(subset_dir.glob("*.nc")))
        print(f"Already downloaded ({nc_count} files) — skipping")
        sys.exit(0)

    # Clean up any partial previous download
    if subset_dir.exists():
        print(f"Cleaning up partial download in {subset_dir}")
        shutil.rmtree(subset_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Parse dates ───────────────────────────────────────────
    start_dt = datetime.strptime(args.start, "%Y%m%d")
    end_dt   = datetime.strptime(args.end,   "%Y%m%d")

    # ── Download ──────────────────────────────────────────────
    print(f"Downloading frame {args.frame}  {args.start} → {args.end} ...")
    print(f"Output: {subset_dir}")

    _download.run_download(
        args.frame,
        start_datetime=start_dt,
        end_datetime=end_dt,
        num_workers=args.workers,
        output_dir=subset_dir,
        bbox=clipped_bbox,
    )

    # ── Verify and mark complete ───────────────────────────────
    nc_files = list(subset_dir.glob("*.nc"))
    print(f"Downloaded {len(nc_files)} .nc files")

    if not nc_files:
        print("WARNING: no .nc files downloaded — frame may have no data in this date range")
        sys.exit(1)

    done_marker.touch()
    print(f"Done. Marker written: {done_marker}")


if __name__ == "__main__":
    main()