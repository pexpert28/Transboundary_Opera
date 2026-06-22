#!/usr/bin/env python3
"""
reset_aquifer.py -- wipe ALL stored objects for an aquifer from Allas.

Deletes everything under <bucket>/<aquifer>/ (every frame, every mintpy output)
so the next pipeline run regenerates all h5 files from scratch with the
materialized (virtual=False) code. Optionally also clears the pipeline state
JSON so nothing is marked "done".

Safety:
  * Dry-run by default: lists and counts what is there, deletes nothing.
  * Deletes only after you pass --confirm.

Usage
-----
    # preview (no changes)
    pixi run -e operaapp python hpc/reset_aquifer.py --aquifer N015

    # actually wipe everything for N015, including its state JSON
    pixi run -e operaapp python hpc/reset_aquifer.py --aquifer N015 --state --confirm

After it finishes, re-run:
    pixi run -e operaapp python hpc/pipeline.py --aquifer N015 --start 20220101 --end 20231231
"""

from __future__ import annotations

import argparse
import subprocess
import sys

DEFAULT_BUCKET = "transboundry-opera-bucket"


def s3_listing(prefix: str):
    """Return (lines, n_objects, total_bytes) for everything under a prefix."""
    r = subprocess.run(["s3cmd", "ls", "--recursive", f"s3://{prefix}"],
                       capture_output=True, text=True)
    lines, total = [], 0
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        # s3cmd ls format: DATE TIME  SIZE  s3://key
        if len(parts) >= 4 and parts[2].isdigit():
            total += int(parts[2])
        lines.append(line)
    return lines, len(lines), total


def s3_del(prefix: str) -> bool:
    r = subprocess.run(["s3cmd", "del", "--recursive", "--force", f"s3://{prefix}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  del failed: {(r.stderr or '').strip()}")
        return False
    n = len([l for l in (r.stdout or '').splitlines() if l.startswith("delete:")])
    print(f"  deleted {n} object(s) under s3://{prefix}")
    return True


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aquifer", required=True)
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--state", action="store_true",
                    help="also delete pipeline_state/<aquifer>_state.json")
    ap.add_argument("--confirm", action="store_true",
                    help="actually delete (omit for a dry run)")
    args = ap.parse_args(argv)

    data_prefix = f"{args.bucket}/{args.aquifer}/"
    state_key = f"{args.bucket}/pipeline_state/{args.aquifer}_state.json"

    print(f"Scanning s3://{data_prefix} ...\n")
    lines, n_obj, total = s3_listing(data_prefix)

    if n_obj == 0:
        print("Nothing stored under that prefix. Nothing to do.")
        return 0

    for line in lines:
        print(f"  {line}")
    print(f"\n{n_obj} object(s), {human(total)} total under s3://{data_prefix}")
    if args.state:
        print(f"Plus state JSON: s3://{state_key}")

    if not args.confirm:
        print("\nDRY RUN -- nothing deleted. Re-run with --confirm to wipe the above.")
        return 0

    print("\nDeleting all objects for the aquifer...")
    ok = s3_del(data_prefix)
    if args.state:
        print("Deleting state JSON...")
        # ignore failure here -- state JSON may not exist
        subprocess.run(["s3cmd", "del", "--force", f"s3://{state_key}"],
                       capture_output=True, text=True)
        print(f"  cleared s3://{state_key} (if it existed)")

    print("\nDone. Re-run the pipeline to regenerate all h5 files:")
    print(f"  pixi run -e operaapp python hpc/pipeline.py "
          f"--aquifer {args.aquifer} --start 20220101 --end 20231231")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())