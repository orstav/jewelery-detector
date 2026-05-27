# Local Embedding Setup

The clustering tool is local-first. It can run the full benchmark plumbing with
the fake provider, but real product clustering needs a local embedding model.

## Recommended Development Setup

Use a Python virtual environment instead of installing into macOS system Python.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

The first real DINOv2 run downloads model weights and caches them locally. Later
runs reuse both the model cache and this tool's embedding cache.

## First Real Local Run

Start with the small DINOv2 model:

```bash
python tools/jewelry_cluster_benchmark.py cluster \
  --manifest data/normalized/manifest.csv \
  --assets data/normalized/visual_assets.json \
  --out results/clustering_dinov2_small \
  --provider dinov2 \
  --dinov2-model dinov2_vits14 \
  --device auto \
  --offline-model-cache
```

If quality is not good enough and the MacBook has enough memory, try base:

```bash
python tools/jewelry_cluster_benchmark.py cluster \
  --manifest data/normalized/manifest.csv \
  --assets data/normalized/visual_assets.json \
  --out results/clustering_dinov2_base \
  --provider dinov2 \
  --dinov2-model dinov2_vitb14 \
  --device auto \
  --offline-model-cache
```

The current best local provider is SigLIP:

```bash
python tools/jewelry_cluster_benchmark.py cluster \
  --manifest data/normalized/manifest.csv \
  --assets data/normalized/visual_assets.json \
  --out results/clustering_siglip_base \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device auto \
  --candidate-threshold 0.92 \
  --offline-model-cache
```

## Production Posture

On the Intel N150 production box, run embeddings in a background job and rely on
the cache. Do not block the WhatsApp/OpenClaw interaction on uncached batch
embedding.

If the N150 is too slow, add an online embedding provider behind the same
`EmbeddingProvider` interface. Keep the cache key as:

```text
provider id + model id + image hash + view
```
