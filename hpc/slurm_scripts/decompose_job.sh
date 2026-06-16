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

# ── Skip if already done ──────────────────────────────────────
DECOMP_COUNT=$(s3cmd ls "s3://$BUCKET/$AQUIFER/" 2>/dev/null | grep -cE "_dhorz\.h5|_dvert\.h5" || true)
if [ "$DECOMP_COUNT" -gt 0 ]; then
    echo "Already done ($DECOMP_COUNT files) — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
mkdir -p "$SCRATCH"

s3cmd get "s3://$BUCKET/container/transboundary_opera.sif" \
    "$SCRATCH/transboundary_opera.sif"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Pull per-frame mintpy outputs ─────────────────────────────
echo ""
echo "--- Pulling per-frame H5 files ---"
PULLED=0
for FRAME in "${FRAMES[@]}"; do
    # Some frames are skipped (no spatial overlap) — check Allas first
    if ! s3cmd ls "s3://$BUCKET/$AQUIFER/$FRAME/mintpy/timeseries.h5" \
            2>/dev/null | grep -q "timeseries.h5"; then
        echo "  Frame $FRAME: not in Allas (skipped during processing) — skipping"
        continue
    fi

    mkdir -p "$SCRATCH/$AQUIFER/$FRAME/mintpy"
    s3cmd get --recursive \
        "s3://$BUCKET/$AQUIFER/$FRAME/mintpy/" \
        "$SCRATCH/$AQUIFER/$FRAME/mintpy/"
    echo "  ✓ Frame $FRAME"
    PULLED=$((PULLED + 1))
done

echo "Pulled $PULLED frames for decomposition"

if [ "$PULLED" -eq 0 ]; then
    echo "ERROR: no frames available for decomposition"
    exit 1
fi

# ── Run decomposition ─────────────────────────────────────────
echo ""
echo "--- Running decomposition ---"
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    --bind "$REPO/src/transboundary_opera:/transboundary_opera/src/transboundary_opera:ro" \
    --env XDG_CACHE_HOME=/work/cache \
    --env MPLCONFIGDIR=/work/cache/matplotlib \
    "$SIF" \
    $PYTHON /repo/src/transboundary_opera/run_decomposition.py \
        --aquifer-dir "/work/$AQUIFER"

# ── Upload outputs ────────────────────────────────────────────
DEFO_FILES=$(find "$SCRATCH/$AQUIFER" -maxdepth 1 -name "*.h5" ! -path "*/mintpy/*" | sort)
if [ -n "$DEFO_FILES" ]; then
    echo ""
    echo "--- Uploading decomposition outputs ---"
    for f in $DEFO_FILES; do
        FNAME=$(basename "$f")
        s3cmd put "$f" "s3://$BUCKET/$AQUIFER/$FNAME"
        echo "  Uploaded: $FNAME"
    done
else
    echo "No decomposition output files found (single-pass or no overlapping pairs)"
fi

rm -rf "$SCRATCH"
echo ""
echo "============================================================"
echo "Decomposition for $AQUIFER complete: $(date -u)"
echo "============================================================"