#!/usr/bin/env python3
"""
run_all_batched.py -- submit every aquifer in waves that respect SLURM limits.

`pipeline.py --all-aquifers` submits every job at once, which exceeds this
account's MaxSubmit (200 queued). This driver submits a few aquifers at a time,
waits for the queue to drain below a threshold, then continues.

Safe to re-run: aquifers whose outputs already exist in Allas short-circuit via
the frame job's skip-guard, so an interrupted run resumes cheaply.

Usage:
    # see the plan without submitting anything
    pixi run -e operaapp python hpc/run_all_batched.py \
        --start 20160101 --end 20260722 --dry-run

    # submit for real
    pixi run -e operaapp python hpc/run_all_batched.py \
        --start 20160101 --end 20260722

    # resume, skipping aquifers already done
    pixi run -e operaapp python hpc/run_all_batched.py \
        --start 20160101 --end 20260722 --skip N013 N015
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / "hpc" / "pipeline.py"
CONFIG = REPO_ROOT / "hpc" / "config.yaml"


def queued_jobs() -> int:
    """Number of jobs currently queued or running for this user."""
    r = subprocess.run(["squeue", "--me", "-h", "-o", "%i"],
                       capture_output=True, text=True)
    return len([l for l in r.stdout.splitlines() if l.strip()])


def aquifer_codes() -> list:
    import geopandas as gpd
    import yaml
    cfg = yaml.safe_load(open(CONFIG))
    gdf = gpd.read_file(cfg["shapefile"])
    return sorted(gdf["CODE_2021"].unique())


def submit_aquifer(code: str, start: str, end: str, dry_run: bool) -> bool:
    cmd = [sys.executable, str(PIPELINE), "--aquifer", code,
           "--start", start, "--end", end]
    if dry_run:
        cmd.append("--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print(f"  !! {code} FAILED to submit")
        if r.stderr.strip():
            print("     " + r.stderr.strip().splitlines()[-1])
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--max-queued", type=int, default=150,
                    help="pause when the queue reaches this many jobs (cap is 200)")
    ap.add_argument("--poll", type=int, default=300,
                    help="seconds between queue checks while waiting")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="aquifer codes to skip (e.g. already complete)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="only submit these codes")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    codes = aquifer_codes()
    if args.only:
        codes = [c for c in codes if c in args.only]
    codes = [c for c in codes if c not in args.skip]

    print(f"{len(codes)} aquifer(s) to submit: {', '.join(codes)}")
    print(f"Queue threshold: {args.max_queued} (pause above this)\n")

    submitted, failed = [], []

    for i, code in enumerate(codes, 1):
        # Wait for room in the queue before submitting the next aquifer.
        if not args.dry_run:
            while True:
                n = queued_jobs()
                if n < args.max_queued:
                    break
                print(f"  queue at {n} jobs (>= {args.max_queued}); "
                      f"waiting {args.poll}s...")
                time.sleep(args.poll)

        print(f"\n[{i}/{len(codes)}] === {code} ===")
        if submit_aquifer(code, args.start, args.end, args.dry_run):
            submitted.append(code)
        else:
            failed.append(code)

    print("\n" + "=" * 60)
    print(f"submitted: {len(submitted)}")
    if failed:
        print(f"FAILED   : {len(failed)}  {failed}")
        print("Re-run with --only to retry just those.")
    print("Monitor: squeue --me")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())