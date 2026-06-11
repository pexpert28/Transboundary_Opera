#!/usr/bin/env python3
"""
pipeline.py — Transboundary Opera Pipeline Orchestrator for Mahti

Reads TBA_full.shp + config.yaml, discovers Sentinel-1 frame IDs for the
target aquifer(s), generates SLURM job scripts, and submits them with
automatic dependency chains:

    frame jobs  (run1 download + process_frame.py per frame)
        └── decomposition job  (after ALL frame jobs succeed)
                └── cog mosaic job  (after decomposition succeeds)

Usage:
    # Dry run first — always do this before submitting real jobs
    pixi run -e operaapp python hpc/pipeline.py \
        --aquifer N015 --start 20200101 --end 20231231 --dry-run

    # Single aquifer
    pixi run -e operaapp python hpc/pipeline.py \
        --aquifer N015 --start 20200101 --end 20231231

    # All aquifers in shapefile
    pixi run -e operaapp python hpc/pipeline.py \
        --all-aquifers --start 20200101 --end 20231231

Requirements:
    - Run from the repo root on the Mahti login node
    - config.yaml must be filled in (CSC project, bucket, repo path)
    - Allas must be activated: module load allas && allas-conf
    - Apptainer .sif must be uploaded to Allas already
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import yaml

# ── Paths ──────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "hpc" / "config.yaml"
TEMPLATES   = REPO_ROOT / "hpc" / "slurm_scripts"
LOGS_DIR    = REPO_ROOT / "hpc" / "logs"


# ── Config ─────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    required = ["csc_project", "allas_bucket", "repo_path", "shapefile"]
    missing = [k for k in required if not cfg.get(k) or "XXXXXXX" in str(cfg.get(k, ""))]
    if missing:
        print(f"ERROR: fill in these config values first: {missing}")
        sys.exit(1)
    return cfg


# ── Frame discovery ────────────────────────────────────────────

def discover_frames(shapefile: str, aquifer_code: str) -> list:
    gdf = gpd.read_file(shapefile)
    gdf_aq = gdf[gdf["CODE_2021"] == aquifer_code]
    if gdf_aq.empty:
        print(f"ERROR: aquifer {aquifer_code} not found in {shapefile}")
        sys.exit(1)

    print(f"Aquifer: {gdf_aq['AQ_NAME'].iloc[0]} ({aquifer_code})")
    print("Querying ASF for Sentinel-1 frames...")

    from transboundary_opera.displacement_tools import get_unique_frame_ids
    frame_ids = get_unique_frame_ids(gdf_aq, track_per_row=False)

    if not frame_ids:
        print(f"ERROR: no frames found for {aquifer_code}")
        sys.exit(1)

    print(f"Found {len(frame_ids)} frame(s): {frame_ids}")
    return frame_ids


# ── State tracking ─────────────────────────────────────────────

def load_state(cfg: dict, aquifer_code: str) -> dict:
    """Pull state JSON from Allas. Returns empty state if not found or unavailable."""
    empty = {"aquifer": aquifer_code, "frames": {},
             "decomposition": {"status": "pending"},
             "cog": {"status": "pending"}}
    try:
        bucket = cfg["allas_bucket"]
        key = f"pipeline_state/{aquifer_code}_state.json"
        result = subprocess.run(
            ["a-check", f"{bucket}/{key}"], capture_output=True
        )
        if result.returncode != 0:
            return empty
        tmp = tempfile.mktemp(suffix=".json")
        subprocess.run(
            ["a-get", f"{bucket}/{key}", "-C", str(Path(tmp).parent)],
            check=True, capture_output=True
        )
        with open(tmp) as f:
            return json.load(f)
    except FileNotFoundError:
        # a-check/a-get not in PATH — Allas module not loaded, return empty state
        return empty


def save_state(cfg: dict, aquifer_code: str, state: dict):
    """Push state JSON to Allas. Silently skips if Allas not available."""
    try:
        tmp = tempfile.mktemp(suffix=".json")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        subprocess.run(
            ["a-put", tmp, "-b",
             f"{cfg['allas_bucket']}/pipeline_state",
             "--object", f"{aquifer_code}_state.json"],
            check=True, capture_output=True
        )
        Path(tmp).unlink(missing_ok=True)
    except FileNotFoundError:
        pass  # Allas not available, skip state saving


# ── SLURM script generation ────────────────────────────────────

def render_template(template_path: Path, variables: dict) -> str:
    text = template_path.read_text()
    for key, val in variables.items():
        text = text.replace(f"{{{key}}}", str(val))
    # Catch any unfilled placeholders
    import re
    unfilled = re.findall(r'\{[A-Z_]+\}', text)
    if unfilled:
        print(f"WARNING: unfilled template variables: {unfilled}")
    return text


def write_job_script(content: str, name: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"{name}.sh"
    path.write_text(content)
    path.chmod(0o755)
    return path


def submit_job(script_path: Path, dependency: str = None,
               dry_run: bool = False) -> str:
    cmd = ["sbatch", "--parsable"]
    if dependency:
        cmd += [f"--dependency=afterok:{dependency}"]
    cmd.append(str(script_path))

    if dry_run:
        dep_str = f" (after {dependency})" if dependency else ""
        print(f"  [DRY RUN] {script_path.name}{dep_str}")
        return "DRY_RUN"

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


# ── Main aquifer run ───────────────────────────────────────────

def run_aquifer(aquifer_code: str, cfg: dict, start_date: str,
                end_date: str, dry_run: bool = False):

    print(f"\n{'='*60}")
    print(f"AQUIFER: {aquifer_code}  |  {start_date} → {end_date}")
    print(f"{'='*60}")

    frame_ids = discover_frames(cfg["shapefile"], aquifer_code)
    state     = load_state(cfg, aquifer_code)
    slurm_cfg = cfg["slurm"]

    # ── Frame jobs ────────────────────────────────────────────
    frame_job_ids = []

    for frame_id in frame_ids:
        frame_key   = str(frame_id)
        frame_state = state["frames"].get(frame_key, {})

        if frame_state.get("status") == "done":
            print(f"  Frame {frame_id}: already done — skipping")
            frame_job_ids.append(f"DONE_{frame_id}")
            continue

        variables = {
            "AQUIFER":       aquifer_code,
            "FRAME_ID":      frame_id,
            "START_DATE":    start_date,
            "END_DATE":      end_date,
            "OPERA_VERSION": cfg.get("opera_version", 1.1),
            "DL_WORKERS":    cfg.get("download_workers", 5),
            "REPO":          cfg["repo_path"],
            "BUCKET":        cfg["allas_bucket"],
            "CSC_PROJECT":   cfg["csc_project"],
            "PARTITION":     slurm_cfg["frame_job"]["partition"],
            "CPUS":          slurm_cfg["frame_job"]["cpus"],
            "MEM":           slurm_cfg["frame_job"]["mem"],
            "TIME":          slurm_cfg["frame_job"]["time"],
            "NVME_GB":       slurm_cfg["frame_job"]["nvme_gb"],
            "LOGS_DIR":      str(LOGS_DIR),
        }

        script = render_template(TEMPLATES / "frame_job.sh", variables)
        path   = write_job_script(script, f"{aquifer_code}_{frame_id}")
        job_id = submit_job(path, dry_run=dry_run)
        print(f"  Frame {frame_id}: job {job_id}")

        frame_job_ids.append(job_id)
        state["frames"][frame_key] = {
            "status": "submitted", "slurm_job": job_id,
            "submitted_at": datetime.utcnow().isoformat()
        }

    # ── Decomposition job ─────────────────────────────────────
    real_ids = [j for j in frame_job_ids if not j.startswith("DONE_")]
    dep_str  = ":".join(real_ids) if real_ids else None

    variables = {
        "AQUIFER":     aquifer_code,
        "FRAMES":      " ".join(str(f) for f in frame_ids),
        "REPO":        cfg["repo_path"],
        "BUCKET":      cfg["allas_bucket"],
        "CSC_PROJECT": cfg["csc_project"],
        "PARTITION":   slurm_cfg["decompose_job"]["partition"],
        "CPUS":        slurm_cfg["decompose_job"]["cpus"],
        "MEM":         slurm_cfg["decompose_job"]["mem"],
        "TIME":        slurm_cfg["decompose_job"]["time"],
        "NVME_GB":     slurm_cfg["decompose_job"]["nvme_gb"],
        "LOGS_DIR":    str(LOGS_DIR),
    }

    script       = render_template(TEMPLATES / "decompose_job.sh", variables)
    path         = write_job_script(script, f"{aquifer_code}_decompose")
    decomp_job   = submit_job(path, dependency=dep_str, dry_run=dry_run)
    print(f"\n  Decomposition: job {decomp_job}")
    state["decomposition"] = {"status": "submitted", "slurm_job": decomp_job,
                               "submitted_at": datetime.utcnow().isoformat()}

    # ── COG job ───────────────────────────────────────────────
    variables = {
        "AQUIFER":     aquifer_code,
        "FRAMES":      " ".join(str(f) for f in frame_ids),
        "REPO":        cfg["repo_path"],
        "BUCKET":      cfg["allas_bucket"],
        "CSC_PROJECT": cfg["csc_project"],
        "PARTITION":   slurm_cfg["cog_job"]["partition"],
        "CPUS":        slurm_cfg["cog_job"]["cpus"],
        "MEM":         slurm_cfg["cog_job"]["mem"],
        "TIME":        slurm_cfg["cog_job"]["time"],
        "NVME_GB":     slurm_cfg["cog_job"]["nvme_gb"],
        "LOGS_DIR":    str(LOGS_DIR),
    }

    script   = render_template(TEMPLATES / "cog_job.sh", variables)
    path     = write_job_script(script, f"{aquifer_code}_cog")
    dep      = decomp_job if decomp_job != "DRY_RUN" else None
    cog_job  = submit_job(path, dependency=dep, dry_run=dry_run)
    print(f"  COG mosaic:    job {cog_job}")
    state["cog"] = {"status": "submitted", "slurm_job": cog_job,
                    "submitted_at": datetime.utcnow().isoformat()}

    if not dry_run:
        save_state(cfg, aquifer_code, state)

    print(f"\n  All jobs submitted for {aquifer_code}.")
    print(f"  Monitor: squeue --me")
    print(f"  Logs:    hpc/logs/")


# ── CLI ────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Transboundary Opera pipeline orchestrator for Mahti"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--aquifer", type=str,
                   help="Single aquifer CODE_2021 (e.g. N015)")
    g.add_argument("--all-aquifers", action="store_true",
                   help="Process all aquifers in shapefile")
    p.add_argument("--start",   required=True, help="Start date YYYYMMDD")
    p.add_argument("--end",     required=True, help="End date YYYYMMDD")
    p.add_argument("--config",  default=str(CONFIG_PATH))
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be submitted without submitting")
    args = p.parse_args()

    cfg = load_config(Path(args.config))

    if args.dry_run:
        print("=== DRY RUN — no jobs will be submitted ===\n")

    if args.all_aquifers:
        gdf    = gpd.read_file(cfg["shapefile"])
        codes  = sorted(gdf["CODE_2021"].unique())
        print(f"Processing all {len(codes)} aquifers")
    else:
        codes = [args.aquifer]

    for code in codes:
        run_aquifer(code, cfg, args.start, args.end, args.dry_run)

    print("\nDone. Monitor with: squeue --me")


if __name__ == "__main__":
    main()