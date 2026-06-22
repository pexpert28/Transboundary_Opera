#!/usr/bin/env python3
"""
ts_integrity.py -- detect the "broken virtual link / empty timeseries" failure.

Why this exists
---------------
process_frame.py writes mintpy/timeseries.h5 with ``virtual=True``, so the file
is an HDF5 Virtual Dataset (VDS): it does NOT hold the displacement arrays
itself, it only points at the underlying source file(s). On a single machine the
source sits next to it and reads resolve to real data (this is why Clay's local
run works). In the HPC pipeline timeseries.h5 is uploaded to Allas and pulled
onto the decomposition node *without* its source file, so the VDS dangles and
HDF5 silently returns the fill value (NaN or 0). The reference-point attributes
(REF_X/REF_Y/REF_LAT/REF_LON) survive because they live on the file itself, so
the file looks complete while the data is empty -- exactly the symptom in Clay's
report that he could not reproduce locally.

This module turns that diagnosis into an automated check. It reports, for a
timeseries.h5:
  * whether the timeseries dataset is a virtual dataset
  * which source file(s) it points at, and whether they are present
  * the fraction of finite / non-zero pixels in a sampled time slice

A file is flagged BROKEN when its virtual source is missing, or when the data is
effectively all-NaN / all-zero.

Usage (standalone)
------------------
    # one or more files
    python ts_integrity.py /work/N015/40297/mintpy/timeseries.h5

    # every frame under an aquifer dir (globs */mintpy/timeseries.h5)
    python ts_integrity.py --aquifer-dir /work/N015

Exit code is non-zero if any file is broken, so it can gate a SLURM step.

Usage (importable)
------------------
    from transboundary_opera.ts_integrity import inspect_timeseries, assert_usable
    rep = inspect_timeseries(path)
    print(rep.one_line())
    assert_usable(path)          # raises RuntimeError if broken

Only depends on numpy + h5py (no mintpy), so it runs anywhere.
"""

from __future__ import annotations

import argparse
import glob as _glob
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

try:
    import h5py
except ImportError:  # pragma: no cover
    h5py = None


# Dataset names we expect a displacement timeseries to live under, in priority order.
_CANDIDATE_DSETS = ("timeseries", "displacement", "dlos")


@dataclass
class TSReport:
    path: Path
    dataset: Optional[str] = None
    is_virtual: bool = False
    # (recorded source name, resolved candidate path, exists?)
    sources: List[Tuple[str, str, bool]] = field(default_factory=list)
    n_sampled: int = 0
    finite_frac: float = 0.0
    nonzero_frac: float = 0.0
    ok: bool = False
    reasons: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def missing_sources(self) -> List[str]:
        return [rec for rec, _cand, exists in self.sources if not exists]

    def one_line(self) -> str:
        name = self.path.parent.parent.name + "/" + self.path.name  # e.g. 40297/timeseries.h5
        if self.error:
            return f"[ERROR ] {name}: {self.error}"
        tag = "ok    " if self.ok else "BROKEN"
        vds = "virtual" if self.is_virtual else "real   "
        miss = f" missing-src={len(self.missing_sources)}" if self.missing_sources else ""
        return (f"[{tag}] {name}  ({vds})  "
                f"finite={self.finite_frac:5.1%} nonzero={self.nonzero_frac:5.1%}{miss}")


def _pick_dataset(f) -> Optional[str]:
    """Return the name of the timeseries-like dataset, or None."""
    for name in _CANDIDATE_DSETS:
        if name in f and isinstance(f[name], h5py.Dataset):
            return name
    # Fall back to the largest >=2D float dataset at the top level.
    best, best_size = None, -1
    for name, obj in f.items():
        if isinstance(obj, h5py.Dataset) and obj.ndim >= 2 and obj.size > best_size \
                and np.issubdtype(obj.dtype, np.floating):
            best, best_size = name, obj.size
    return best


def _resolve_source(h5_path: Path, recorded: str) -> Tuple[str, bool]:
    """Given a VDS source filename, return (candidate_path, exists)."""
    p = Path(recorded)
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    # Also try relative to the .h5's own directory and by basename --
    # the source is usually written next to the file that produced it.
    candidates.append(h5_path.parent / recorded)
    candidates.append(h5_path.parent / p.name)
    for c in candidates:
        if c.exists():
            return str(c), True
    return str(candidates[0]), False


def inspect_timeseries(path, sample_slices: int = 1,
                       min_finite_frac: float = 1e-3) -> TSReport:
    """Inspect a timeseries.h5 and return a TSReport.

    A file is ``ok`` when it is not missing any virtual source AND its sampled
    data has more than ``min_finite_frac`` finite, non-zero pixels.
    """
    path = Path(path)
    rep = TSReport(path=path)

    if h5py is None:
        rep.error = "h5py not installed"
        return rep
    if not path.exists():
        rep.error = "file does not exist"
        return rep

    try:
        with h5py.File(path, "r") as f:
            name = _pick_dataset(f)
            if name is None:
                rep.error = "no timeseries-like dataset found"
                return rep
            rep.dataset = name
            dset = f[name]
            rep.is_virtual = bool(getattr(dset, "is_virtual", False))

            if rep.is_virtual:
                for vs in dset.virtual_sources():
                    cand, exists = _resolve_source(path, vs.file_name)
                    rep.sources.append((vs.file_name, cand, exists))

            # Sample up to `sample_slices` time slices cheaply. A broken VDS
            # returns the fill value here (NaN or 0) instead of raising.
            if dset.ndim >= 3:
                k = min(sample_slices, dset.shape[0])
                sample = np.asarray(dset[:k])
            else:
                sample = np.asarray(dset[...])
            rep.n_sampled = int(sample.size)
            if sample.size:
                finite = np.isfinite(sample)
                rep.finite_frac = float(finite.mean())
                rep.nonzero_frac = float((finite & (sample != 0)).mean())
    except Exception as e:  # noqa: BLE001 -- want any failure surfaced, not raised
        rep.error = f"{type(e).__name__}: {e}"
        return rep

    # Decide.
    if rep.missing_sources:
        rep.reasons.append(f"{len(rep.missing_sources)} virtual source(s) missing")
    if rep.finite_frac <= min_finite_frac:
        rep.reasons.append(f"finite fraction {rep.finite_frac:.2%} <= {min_finite_frac:.2%}")
    elif rep.nonzero_frac <= min_finite_frac:
        rep.reasons.append(f"non-zero fraction {rep.nonzero_frac:.2%} <= {min_finite_frac:.2%}")

    rep.ok = not rep.reasons
    return rep


def assert_usable(path, **kwargs) -> TSReport:
    """Inspect and raise RuntimeError if the file is broken. Returns the report."""
    rep = inspect_timeseries(path, **kwargs)
    if not rep.ok:
        why = rep.error or "; ".join(rep.reasons)
        raise RuntimeError(
            f"timeseries unusable: {path} ({why}). "
            f"This is the broken-virtual-link signature -- the file was likely "
            f"uploaded without its displacement source. See ts_integrity.py docstring."
        )
    return rep


def materialize_timeseries(src_path, dst_path=None) -> Path:
    """Rewrite a virtual timeseries.h5 as a contiguous (real) copy.

    Run this on the node that produced the file, BEFORE uploading to Allas,
    while the virtual source still resolves. Copies datasets and attributes
    verbatim, replacing virtual datasets with materialized arrays.
    """
    if h5py is None:
        raise RuntimeError("h5py not installed")
    src_path = Path(src_path)
    dst_path = Path(dst_path) if dst_path else src_path.with_suffix(".materialized.h5")

    with h5py.File(src_path, "r") as src, h5py.File(dst_path, "w") as dst:
        for k, v in src.attrs.items():
            dst.attrs[k] = v
        for name, obj in src.items():
            if isinstance(obj, h5py.Dataset):
                data = np.asarray(obj[...])  # forces VDS resolution into memory
                d = dst.create_dataset(name, data=data)
                for k, v in obj.attrs.items():
                    d.attrs[k] = v
    return dst_path


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _gather_paths(args) -> List[Path]:
    paths: List[Path] = []
    if args.aquifer_dir:
        paths += [Path(p) for p in sorted(
            _glob.glob(str(Path(args.aquifer_dir) / "*" / "mintpy" / "timeseries.h5")))]
    for p in args.paths:
        paths += [Path(x) for x in sorted(_glob.glob(p))] or [Path(p)]
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="timeseries.h5 path(s) or globs")
    ap.add_argument("--aquifer-dir",
                    help="glob <dir>/*/mintpy/timeseries.h5 instead of listing files")
    ap.add_argument("--min-finite-frac", type=float, default=1e-3,
                    help="minimum finite/non-zero pixel fraction to count as usable")
    ap.add_argument("--sample-slices", type=int, default=1,
                    help="how many time slices to sample for finite stats")
    args = ap.parse_args(argv)

    paths = _gather_paths(args)
    if not paths:
        print("No timeseries.h5 files found. Pass paths or --aquifer-dir.")
        return 2

    print(f"Checking {len(paths)} timeseries file(s)...\n")
    broken = 0
    for p in paths:
        rep = inspect_timeseries(p, sample_slices=args.sample_slices,
                                 min_finite_frac=args.min_finite_frac)
        print(rep.one_line())
        for rec, cand, exists in rep.sources:
            if not exists:
                print(f"         -> source NOT found on this node: {rec}")
        if not rep.ok:
            broken += 1

    print()
    if broken:
        print(f"{broken}/{len(paths)} file(s) BROKEN (empty / missing virtual source).")
        print("Root cause: timeseries.h5 is a virtual link uploaded without its")
        print("displacement source, so reads return NaN/0 on this node.")
        print("Fix: reprocess with a materialized (virtual=False) timeseries, or run")
        print("materialize_timeseries() before upload. See module docstring.")
        return 1
    print(f"All {len(paths)} file(s) OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())