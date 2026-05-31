"""Production jewelry detector CLI for OpenCLAW integrations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import jewelry_cluster_benchmark as benchmark
from tools import jewelry_detector_db as db

IMAGE_EXTENSIONS = benchmark.IMAGE_EXTENSIONS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jewelry detector production CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile = subparsers.add_parser("profile", help="profile one image as DB-ready JSON")
    profile.add_argument("--image", required=True, help="source image path")
    profile.add_argument("--image-id", required=True, help="stable caller-owned image id")
    profile.add_argument("--out", required=True, help="output JSON path")
    profile.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    profile.add_argument("--max-image-size", type=int, default=1024, help="max image side sent to AI")
    profile.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    profile.add_argument(
        "--mock-response",
        help="parse a local model-response JSON file instead of calling OpenAI; useful for OpenCLAW plumbing tests",
    )
    profile.set_defaults(func=benchmark.product_profile_command)

    embed = subparsers.add_parser("embed", help="embed one image as crop JSON")
    embed.add_argument("--image", required=True, help="source image path")
    embed.add_argument("--image-id", required=True, help="stable caller-owned image id")
    embed.add_argument("--out", required=True, help="output JSON path")
    embed.add_argument(
        "--provider",
        choices=["fake", "dinov2", "clip", "siglip"],
        default="siglip",
        help="embedding provider; use fake for OpenCLAW plumbing tests and siglip for production",
    )
    embed.add_argument("--model-id", help="provider-specific Hugging Face model id for CLIP/SigLIP providers")
    embed.add_argument("--dinov2-model", default="dinov2_vits14", help="DINOv2 model name")
    embed.add_argument("--device", default="auto", help="embedding/detector device: auto, cpu, mps, or cuda")
    embed.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    embed.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    embed.add_argument("--profile", help="optional product-profile payload or profile JSON object")
    embed.add_argument(
        "--detector",
        choices=["profile", "owlv2"],
        default="profile",
        help="crop detector used when --profile is supplied",
    )
    embed.add_argument("--owlv2-model", default="google/owlv2-base-patch16-ensemble", help="Hugging Face OWLv2 model id")
    embed.add_argument("--owlv2-threshold", type=float, default=0.05, help="raw OWLv2 score threshold")
    embed.set_defaults(func=benchmark.product_embed_command)

    init_db = subparsers.add_parser("init-db", help="create OpenCLAW jewelry tables and seed the default policy")
    init_db.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    init_db.add_argument(
        "--skip-vector-index",
        action="store_true",
        help="create tables/policy but skip the HNSW vector index",
    )
    init_db.set_defaults(func=init_db_command)

    store_profile = subparsers.add_parser("store-profile", help="store a profile JSON payload in Postgres")
    store_profile.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    store_profile.add_argument("--profile-json", required=True, help="JSON output from jewelry_detector.py profile")
    store_profile.add_argument("--source-uri", required=True, help="original image URI/path to store in product_images")
    store_profile.add_argument("--product-id", help="catalog product id; omit for incoming unmatched images")
    store_profile.set_defaults(func=store_profile_command)

    store_embedding = subparsers.add_parser("store-embedding", help="store an embedding JSON payload in Postgres")
    store_embedding.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    store_embedding.add_argument("--embedding-json", required=True, help="JSON output from jewelry_detector.py embed")
    store_embedding.add_argument("--source-uri", required=True, help="original image URI/path to store in product_images")
    store_embedding.add_argument("--product-id", help="catalog product id; omit for incoming unmatched images")
    store_embedding.add_argument(
        "--allow-nonproduction-dim",
        action="store_true",
        help="allow non-768 fake/test embeddings; not compatible with the default vector(768) schema",
    )
    store_embedding.set_defaults(func=store_embedding_command)

    match_embedding = subparsers.add_parser("match-embedding", help="match embedding JSON against catalog embeddings in Postgres")
    match_embedding.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    match_embedding.add_argument("--embedding-json", required=True, help="JSON output from jewelry_detector.py embed")
    match_embedding.add_argument("--out", help="optional match result JSON path")
    match_embedding.add_argument("--policy", help="matching policy name; defaults to the active policy")
    match_embedding.add_argument("--no-persist", action="store_true", help="return a decision without writing match_attempts/candidates")
    match_embedding.set_defaults(func=match_embedding_command)

    index_dir = subparsers.add_parser("index-dir", help="profile, embed, and store catalog images from product folders")
    index_dir.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    index_dir.add_argument("--root", required=True, help="catalog root with one product-id folder per product")
    index_dir.add_argument("--work-dir", required=True, help="directory for intermediate JSON artifacts")
    index_dir.add_argument("--provider", choices=["fake", "dinov2", "clip", "siglip"], default="siglip", help="embedding provider")
    index_dir.add_argument("--model-id", default="google/siglip-base-patch16-224", help="provider-specific model id")
    index_dir.add_argument("--profile-model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    index_dir.add_argument("--device", default="cpu", help="embedding device")
    index_dir.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    index_dir.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    index_dir.add_argument("--mock-response", help="use one local profile response for every image; plumbing tests only")
    index_dir.set_defaults(func=index_dir_command)

    match_image = subparsers.add_parser("match-image", help="profile, embed, store, and match one incoming image")
    match_image.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    match_image.add_argument("--image", required=True, help="incoming image path")
    match_image.add_argument("--image-id", required=True, help="stable caller-owned image id")
    match_image.add_argument("--work-dir", required=True, help="directory for intermediate/result JSON artifacts")
    match_image.add_argument("--out", help="optional match result JSON path")
    match_image.add_argument("--provider", choices=["fake", "dinov2", "clip", "siglip"], default="siglip", help="embedding provider")
    match_image.add_argument("--model-id", default="google/siglip-base-patch16-224", help="provider-specific model id")
    match_image.add_argument("--profile-model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    match_image.add_argument("--device", default="cpu", help="embedding device")
    match_image.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    match_image.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    match_image.add_argument("--mock-response", help="local profile response for plumbing tests")
    match_image.add_argument("--policy", help="matching policy name; defaults to active policy")
    match_image.set_defaults(func=match_image_command)

    match_dir = subparsers.add_parser("match-dir", help="match all incoming images in a directory")
    match_dir.add_argument("--database-url", help="Postgres connection URL; defaults to DATABASE_URL")
    match_dir.add_argument("--input", required=True, help="folder of incoming images")
    match_dir.add_argument("--work-dir", required=True, help="directory for intermediate/result JSON artifacts")
    match_dir.add_argument("--provider", choices=["fake", "dinov2", "clip", "siglip"], default="siglip", help="embedding provider")
    match_dir.add_argument("--model-id", default="google/siglip-base-patch16-224", help="provider-specific model id")
    match_dir.add_argument("--profile-model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    match_dir.add_argument("--device", default="cpu", help="embedding device")
    match_dir.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    match_dir.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    match_dir.add_argument("--mock-response", help="local profile response for plumbing tests")
    match_dir.add_argument("--policy", help="matching policy name; defaults to active policy")
    match_dir.set_defaults(func=match_dir_command)

    return parser


def init_db_command(args: argparse.Namespace) -> int:
    db.init_db(db.database_url(args.database_url), create_vector_index=not args.skip_vector_index)
    print("Initialized OpenCLAW jewelry DB schema")
    return 0


def store_profile_command(args: argparse.Namespace) -> int:
    payload = db.read_json(Path(args.profile_json).resolve())
    db.store_profile(
        db.database_url(args.database_url),
        payload,
        source_uri=args.source_uri,
        product_id=args.product_id,
    )
    print(f"Stored profile: {payload.get('image_id')}")
    return 0


def store_embedding_command(args: argparse.Namespace) -> int:
    payload = db.read_json(Path(args.embedding_json).resolve())
    count = db.store_embedding(
        db.database_url(args.database_url),
        payload,
        source_uri=args.source_uri,
        product_id=args.product_id,
        allow_nonproduction_dim=args.allow_nonproduction_dim,
    )
    print(f"Stored embeddings: {count}")
    return 0


def match_embedding_command(args: argparse.Namespace) -> int:
    payload = db.read_json(Path(args.embedding_json).resolve())
    result = db.match_embedding(
        db.database_url(args.database_url),
        payload,
        policy_name=args.policy,
        persist=not args.no_persist,
    )
    if args.out:
        benchmark.write_json(Path(args.out).resolve(), result)
    print(f"Match status: {result['status']}")
    print(f"Selected product: {result.get('selected_product_id') or ''}")
    print(f"Confidence: {result.get('confidence')}")
    if result.get("match_attempt_id") is not None:
        print(f"Match attempt: {result['match_attempt_id']}")
    return 0


def iter_images(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def image_id_from_path(path: Path, prefix: str = "img") -> str:
    digest = benchmark.stable_name_digest(str(path.resolve()))[:12]
    stem = "".join(ch if ch.isalnum() else "_" for ch in path.stem.lower()).strip("_")[:40] or "image"
    return f"{prefix}_{stem}_{digest}"


def run_profile_artifact(args: argparse.Namespace, image_path: Path, image_id: str, work_dir: Path) -> Path:
    profile_path = work_dir / f"{image_id}.profile.json"
    profile_args = argparse.Namespace(
        image=str(image_path),
        image_id=image_id,
        out=str(profile_path),
        model=args.profile_model,
        max_image_size=1024,
        timeout=90,
        mock_response=getattr(args, "mock_response", None),
    )
    exit_code = benchmark.product_profile_command(profile_args)
    if exit_code != 0:
        msg = f"profile failed for {image_path}"
        raise RuntimeError(msg)
    return profile_path


def run_embed_artifact(args: argparse.Namespace, image_path: Path, image_id: str, profile_path: Path, work_dir: Path) -> Path:
    embedding_path = work_dir / f"{image_id}.embedding.json"
    embed_args = argparse.Namespace(
        image=str(image_path),
        image_id=image_id,
        out=str(embedding_path),
        provider=args.provider,
        model_id=args.model_id,
        dinov2_model="dinov2_vits14",
        device=args.device,
        image_size=args.image_size,
        offline_model_cache=args.offline_model_cache,
        profile=str(profile_path),
        detector="profile",
        owlv2_model="google/owlv2-base-patch16-ensemble",
        owlv2_threshold=0.05,
    )
    exit_code = benchmark.product_embed_command(embed_args)
    if exit_code != 0:
        msg = f"embed failed for {image_path}"
        raise RuntimeError(msg)
    return embedding_path


def index_one_image(args: argparse.Namespace, image_path: Path, image_id: str, product_id: str | None, work_dir: Path) -> None:
    url = db.database_url(args.database_url)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile_path = run_profile_artifact(args, image_path, image_id, work_dir)
    db.store_profile(url, db.read_json(profile_path), source_uri=str(image_path), product_id=product_id)
    embedding_path = run_embed_artifact(args, image_path, image_id, profile_path, work_dir)
    db.store_embedding(url, db.read_json(embedding_path), source_uri=str(image_path), product_id=product_id)


def index_dir_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    work_dir = Path(args.work_dir).resolve()
    count = 0
    for product_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        product_id = product_dir.name
        for image_path in iter_images(product_dir):
            image_id = image_id_from_path(image_path, prefix=f"catalog_{product_id}")
            index_one_image(args, image_path, image_id, product_id, work_dir)
            count += 1
    print(f"Indexed catalog images: {count}")
    return 0


def match_one_image(args: argparse.Namespace, image_path: Path, image_id: str, work_dir: Path) -> db.JsonDict:
    url = db.database_url(args.database_url)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile_path = run_profile_artifact(args, image_path, image_id, work_dir)
    db.store_profile(url, db.read_json(profile_path), source_uri=str(image_path), product_id=None)
    embedding_path = run_embed_artifact(args, image_path, image_id, profile_path, work_dir)
    embedding_payload = db.read_json(embedding_path)
    db.store_embedding(url, embedding_payload, source_uri=str(image_path), product_id=None)
    return db.match_embedding(url, embedding_payload, policy_name=args.policy, persist=True)


def match_image_command(args: argparse.Namespace) -> int:
    result = match_one_image(args, Path(args.image).resolve(), args.image_id, Path(args.work_dir).resolve())
    if args.out:
        benchmark.write_json(Path(args.out).resolve(), result)
    print(f"Match status: {result['status']}")
    print(f"Selected product: {result.get('selected_product_id') or ''}")
    print(f"Confidence: {result.get('confidence')}")
    return 0


def match_dir_command(args: argparse.Namespace) -> int:
    input_dir = Path(args.input).resolve()
    work_dir = Path(args.work_dir).resolve()
    results = []
    for image_path in iter_images(input_dir):
        image_id = image_id_from_path(image_path, prefix="incoming")
        results.append(match_one_image(args, image_path, image_id, work_dir))
    benchmark.write_json(work_dir / "match_results.json", results)
    print(f"Matched images: {len(results)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
