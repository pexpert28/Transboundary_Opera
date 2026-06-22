#!/usr/bin/env python3
"""
check_allas_frame.py -- pull a frame's timeseries.h5 from Allas and check it.

Downloads <bucket>/<aquifer>/<frame>/mintpy/timeseries.h5 with s3cmd into a temp
dir and runs the ts_integrity check on it, so you can verify what is actually
stored in Allas without submitting a SLURM job.

A BROKEN result means the stored file is a virtual link whose displacement
source was never uploaded -- reads return NaN/0 on any other node.

Usage
-----
    pixi run -e operaapp python hpc/check_allas_frame.py \
        --aquifer N015 --frames 40297 40298

    # check every frame, keep the downloaded files, and list what's in Allas
    pixi run -e operaapp python hpc/check_allas_frame.py \
        --aquifer N015 --frames 14878 22665 22666 40296 40297 40298 42264 42265 \
        --keep --list

Exit code is non-zero if any checked frame is broken.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

# --- locate inspect_timeseries, whether or not the package is importable ------
try:
    from transboundary_opera.ts_integrity import inspect_timeseries
except Exception:  # noqa: BLE001
    # Fallback: add <repo>/src to the path (this file lives in <repo>/hpc/).
    repo_src = Path(__file__).resolve().parent.parent / "src"
    sys.path.insert(0, str(repo_src))
    try:
        from transboundary_opera.ts_integrity import inspect_timeseries
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot import ts_integrity ({exc}).")
        print("Make sure src/transboundary_opera/ts_integrity.py exists.")
        sys.exit(2)

DEFAULT_BUCKET = "transboundry-opera-bucket"


def s3_get(key: str, dest: Path) -> bool:
    """s3cmd get s3://<key> -> dest. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["s3cmd", "get", "--force", f"s3://{key}", str(dest)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        tail = msg[-1] if msg else "unknown error"
        print(f"  s3cmd get failed: {tail}")
        return False
    return True


def s3_list(prefix: str) -> None:
    """List what is actually stored under a frame prefix in Allas."""
    r = subprocess.run(
        ["s3cmd", "ls", "--recursive", f"s3://{prefix}"],
        capture_output=True, text=True,
    )
    out = (r.stdout or "").strip()
    if out:
        print("  Allas contents:")
        for line in out.splitlines():
            print(f"    {line}")
    else:
        print(f"  (nothing found under s3://{prefix})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aquifer", required=True)
    ap.add_argument("--frames", required=True, nargs="+",
                    help="one or more frame IDs")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--keep", action="store_true",
                    help="keep downloaded files instead of using a temp dir")
    ap.add_argument("--list", action="store_true",
                    help="also list what is stored under each frame in Allas")
    args = ap.parse_args(argv)

    workdir = Path("allas_check") if args.keep else Path(tempfile.mkdtemp(prefix="allas_check_"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"Working dir: {workdir}\n")

    broken, missing, ok = [], [], []

    for frame in args.frames:
        prefix = f"{args.bucket}/{args.aquifer}/{frame}"
        key = f"{prefix}/mintpy/timeseries.h5"
        dest = workdir / args.aquifer / str(frame) / "timeseries.h5"
        print(f"=== {args.aquifer}/{frame} ===")

        if not s3_get(key, dest):
            print("  -> not retrievable (skipped during processing, or not uploaded)\n")
            missing.append(frame)
            if args.list:
                s3_list(prefix + "/")
                print()
            continue

        rep = inspect_timeseries(dest)
        print("  " + rep.one_line())
        for rec, _cand, exists in rep.sources:
            if not exists:
                print(f"    -> virtual source NOT bundled with the file: {rec}")
        if rep.reasons:
            print(f"    reasons: {'; '.join(rep.reasons)}")

        (ok if rep.ok else broken).append(frame)

        if args.list:
            s3_list(prefix + "/")
        print()

    # Summary
    print("=" * 50)
    print(f"ok:      {len(ok)}  {ok}")
    print(f"broken:  {len(broken)}  {broken}")
    print(f"missing: {len(missing)}  {missing}")
    if broken:
        print("\nBroken frames hold a virtual timeseries.h5 without its source.")
        print("Reprocess them with the materialized (virtual=False) code so the")
        print("data travels with the file. See ts_integrity.py / process_frame.py.")
    if not args.keep:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())