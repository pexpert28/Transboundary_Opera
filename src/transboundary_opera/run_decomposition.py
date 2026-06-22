#!/usr/bin/env python3
"""
run_decomposition.py — CLI wrapper for ascending/descending InSAR decomposition.

Scans an aquifer directory for overlapping ascending and descending frame pairs,
then runs InSARDecomposer to separate LOS displacement into horizontal and
vertical components.

Usage:
    python run_decomposition.py --aquifer-dir /work/N015

Expected input structure:
    /work/N015/
    ├── 5124/mintpy/timeseries.h5    ← ascending frame
    ├── 5125/mintpy/timeseries.h5    ← ascending frame
    ├── 14879/mintpy/timeseries.h5   ← descending frame
    └── 34478/mintpy/timeseries.h5   ← descending frame

Outputs (written to --aquifer-dir root):
    /work/N015/5124_14879_horizontalDefo.h5
    /work/N015/5124_14879_verticalDefo.h5
    ...one pair per overlapping asc/desc combination

Author: Pooya (infrastructure wrapper)
Science: Clay Caldwell / Simran Sangha
"""

import argparse
import sys
from pathlib import Path


def create_parser():
    parser = argparse.ArgumentParser(
        description='Decompose LOS InSAR timeseries into horizontal and vertical components'
    )
    parser.add_argument(
        '--aquifer-dir',
        required=True,
        type=str,
        help='Path to aquifer directory containing per-frame subdirectories'
    )
    parser.add_argument(
        '--min-overlap-pixels',
        default=100,
        type=int,
        help='Minimum number of overlapping valid pixels required (default: 100)'
    )
    parser.add_argument(
        '--min-common-dates',
        default=2,
        type=int,
        help='Minimum number of common dates between pairs (default: 2)'
    )
    return parser


def main(iargs=None):
    parser = create_parser()
    inps = parser.parse_args(iargs)

    aquifer_dir = Path(inps.aquifer_dir).resolve()

    if not aquifer_dir.exists():
        print(f"ERROR: aquifer directory does not exist: {aquifer_dir}")
        sys.exit(1)

    # Verify at least some mintpy timeseries files exist
    ts_files = list(aquifer_dir.glob('*/mintpy/timeseries.h5'))
    if not ts_files:
        print(f"ERROR: no timeseries.h5 files found under {aquifer_dir}")
        print("Expected structure: <aquifer_dir>/<frame_id>/mintpy/timeseries.h5")
        sys.exit(1)

    print(f"Found {len(ts_files)} timeseries file(s) under {aquifer_dir}")
    for f in sorted(ts_files):
        print(f"  {f}")
    # ── Integrity check: catch broken virtual links after the Allas round-trip ──
    from transboundary_opera.ts_integrity import inspect_timeseries
    print("\nChecking timeseries integrity...")
    broken = []
    for f in sorted(ts_files):
        rep = inspect_timeseries(f)
        print("  " + rep.one_line())
        for rec, _cand, exists in rep.sources:
            if not exists:
                print(f"    -> source NOT present on this node: {rec}")
        if not rep.ok:
            broken.append(rep)
    if broken:
        print(f"\nERROR: {len(broken)}/{len(ts_files)} timeseries file(s) are empty/unusable.")
        print("Signature: timeseries.h5 is a virtual link uploaded without its")
        print("displacement source, so reads return NaN/0. Reprocess those frames")
        print("with a materialized (virtual=False) timeseries. See ts_integrity.py.")
        sys.exit(3)
    # Import after validation to keep error messages clean
    from transboundary_opera.decomposition_tools import get_asc_desc_pairs
    from transboundary_opera.decomposer import InSARDecomposer

    print(f"\nScanning for overlapping ascending/descending pairs...")
    pairs = get_asc_desc_pairs(
        aquifer_dir,
        min_overlap_pixels=inps.min_overlap_pixels,
        min_common_dates=inps.min_common_dates,
    )

    if not pairs:
        print("No overlapping ascending/descending pairs found.")
        print("This is normal if the aquifer is only covered by one pass direction.")
        sys.exit(0)

    print(f"Found {len(pairs)} overlapping pair(s):")
    for p in pairs:
        print(f"  ASC {p['asc_frame']} x DESC {p['desc_frame']} "
              f"— {p['n_overlap_pixels']} overlap pixels, "
              f"{p['n_common_dates']} common dates")

    print(f"\nRunning decomposition...")
    decomposer = InSARDecomposer(pairs)
    decomposer.run()

    if decomposer.failed_pairs:
        print(f"\nWARNING: {len(decomposer.failed_pairs)} pair(s) failed:")
        for p in decomposer.failed_pairs:
            print(f"  {p}")

    if decomposer.successful_pairs:
        print(f"\nDecomposition complete. {len(decomposer.successful_pairs)} pair(s) succeeded.")
        print(f"Outputs written to: {aquifer_dir}")
    else:
        print("\nERROR: all pairs failed decomposition")
        sys.exit(1)


if __name__ == '__main__':
    main()