#!/usr/bin/env bash
set -euo pipefail

input=""
out=""
exclude_folder=""
provider="siglip"
model_id="google/siglip-base-patch16-224"
device="auto"
candidate_threshold="0.92"
candidate_top_k="5"
offline_model_cache="1"

usage() {
  cat <<'USAGE'
Usage:
  scripts/openclaw_beta_benchmark.sh --input DIR --out DIR [--exclude-folder NAME]

Runs the beta folder-labeled jewelry benchmark:
  input dataset -> normalized manifest -> SigLIP clustering benchmark

Options:
  --input DIR              Folder containing one subfolder per product cluster.
  --out DIR                Output directory for normalized and benchmark files.
  --exclude-folder NAME    Optional direct child folder to exclude, e.g. 8.
  --provider NAME          Embedding provider. Default: siglip.
  --model-id ID            Provider model id. Default: google/siglip-base-patch16-224.
  --device NAME            auto, cpu, mps, or cuda. Default: auto.
  --candidate-threshold N  Candidate threshold. Default: 0.92.
  --candidate-top-k N      Top-K neighbor candidates. Default: 5.
  --allow-network-model    Do not force offline model cache.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)
      input="$2"
      shift 2
      ;;
    --out)
      out="$2"
      shift 2
      ;;
    --exclude-folder)
      exclude_folder="$2"
      shift 2
      ;;
    --provider)
      provider="$2"
      shift 2
      ;;
    --model-id)
      model_id="$2"
      shift 2
      ;;
    --device)
      device="$2"
      shift 2
      ;;
    --candidate-threshold)
      candidate_threshold="$2"
      shift 2
      ;;
    --candidate-top-k)
      candidate_top_k="$2"
      shift 2
      ;;
    --allow-network-model)
      offline_model_cache="0"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$input" || -z "$out" ]]; then
  echo "ERROR: --input and --out are required" >&2
  usage >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
input_abs="$(cd "$repo_root" && python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$input")"
out_abs="$(cd "$repo_root" && python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$out")"
prepared_input="$out_abs/input"
normalized_out="$out_abs/normalized"
benchmark_out="$out_abs/benchmark"

mkdir -p "$out_abs"

if [[ -n "$exclude_folder" ]]; then
  rm -rf "$prepared_input"
  mkdir -p "$prepared_input"
  python3 - "$input_abs" "$prepared_input" "$exclude_folder" <<'PY'
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
excluded = sys.argv[3]
image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}
kept = 0
removed = 0
for child in sorted(source.iterdir()):
    if child.name.startswith("."):
        continue
    if not child.is_dir():
        continue
    image_count = sum(1 for path in child.rglob("*") if path.is_file() and path.suffix.lower() in image_extensions)
    if child.name == excluded:
        removed += image_count
        continue
    shutil.copytree(child, destination / child.name)
    kept += image_count
print(f"Prepared input: {destination}")
print(f"Kept images: {kept}")
print(f"Excluded folder {excluded}: {removed} images")
PY
else
  prepared_input="$input_abs"
fi

cd "$repo_root"

python3 tools/jewelry_cluster_benchmark.py normalize \
  --reference "$prepared_input" \
  --out "$normalized_out"

cluster_args=(
  tools/jewelry_cluster_benchmark.py cluster
  --manifest "$normalized_out/manifest.csv"
  --out "$benchmark_out"
  --provider "$provider"
  --model-id "$model_id"
  --device "$device"
  --candidate-threshold "$candidate_threshold"
  --candidate-top-k "$candidate_top_k"
)

if [[ "$offline_model_cache" == "1" ]]; then
  cluster_args+=(--offline-model-cache)
fi

python3 "${cluster_args[@]}"

echo "Benchmark report: $benchmark_out/benchmark_report.md"
echo "Review sheets: $benchmark_out/review_sheets"
