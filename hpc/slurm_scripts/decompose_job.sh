#!/bin/bash
#SBATCH --job-name=opera_{AQUIFER}_decompose
#SBATCH --account={CSC_PROJECT}
#SBATCH --partition={PARTITION}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={CPUS}
#SBATCH --mem={MEM}
#SBATCH --time={TIME}
#SBATCH --gres=nvme:{NVME_GB}
#SBATCH --output={LOGS_DIR}/{AQUIFER}_decompose_%j.out
#SBATCH --error={LOGS_DIR}/{AQUIFER}_decompose_%j.err

set -euo pipefail

AQUIFER="{AQUIFER}"
FRAMES=({FRAMES})
REPO="{REPO}"
BUCKET="{BUCKET}"
PYTHON="/transboundary_opera/.pixi/envs/operaapp/bin/python"

echo "============================================================"
echo "Job: $SLURM_JOB_ID — Decomposition for $AQUIFER"
echo "Node: $(hostname) | Started: $(date -u)"
echo "============================================================"

module load allas
allas-conf --silent

# ── Skip if already done ──────────────────────────────────────
DECOMP_COUNT=$(a-list "$BUCKET/$AQUIFER/" 2>/dev/null | grep -cE "_dhorz\.h5|_dvert\.h5" || true)
if [ "$DECOMP_COUNT" -gt 0 ]; then
    echo "Already done — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
mkdir -p "$SCRATCH"

a-get "$BUCKET/container/transboundary_opera.sif" -C "$SCRATCH/"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Pull per-frame mintpy outputs ─────────────────────────────
for FRAME in "${FRAMES[@]}"; do
    mkdir -p "$SCRATCH/$AQUIFER/$FRAME/mintpy"
    a-get "$BUCKET/$AQUIFER/$FRAME/mintpy/" \
        -C "$SCRATCH/$AQUIFER/$FRAME/mintpy/"
    [ -f "$SCRATCH/$AQUIFER/$FRAME/mintpy/timeseries.h5" ] || \
        { echo "ERROR: timeseries.h5 missing for frame $FRAME"; exit 1; }
    echo "  ✓ Frame $FRAME"
done

# ── Run decomposition ─────────────────────────────────────────
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    $PYTHON /repo/src/transboundary_opera/run_decomposition.py \
        --aquifer-dir "/work/$AQUIFER"

# ── Upload outputs ────────────────────────────────────────────
DEFO_FILES=$(find "$SCRATCH/$AQUIFER" -maxdepth 1 -name "*.h5" ! -path "*/mintpy/*" | sort)
if [ -n "$DEFO_FILES" ]; then
    for f in $DEFO_FILES; do
        FNAME=$(basename "$f")
        a-put "$f" -b "$BUCKET/$AQUIFER" --object "$FNAME" --nc
        echo "  Uploaded: $FNAME"
    done
else
    echo "No decomposition files produced (single-pass aquifer)."
fi

rm -rf "$SCRATCH"
echo "Decomposition for $AQUIFER complete: $(date -u)"