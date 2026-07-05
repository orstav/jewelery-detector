#!/usr/bin/env python3
"""Generate jewelry image profiles with Gemini through Google Generative Language API.

Read-only helper for detector experiments. It reads a local image manifest and writes
profile/cache JSON files; it does not mutate the detector DB or call Shopify/Drive.
Authentication supports either GEMINI_API_KEY/GOOGLE_API_KEY or a Google service
account key with the generative-language OAuth scope.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from json import JSONDecodeError
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from tools.jewelry_cluster_benchmark import (
    EVIDENCE_PROFILE_PROMPT_VERSION,
    image_profile_prompt,
    load_ai_decision_cache,
    load_image_manifest,
    parse_image_profile_text,
    resize_image_to_jpeg,
    stable_name_digest,
    write_json,
)

Json = dict[str, Any]


def resized_image_b64(path: Path, tmpdir: Path, max_size: int) -> tuple[str, str]:
    out = tmpdir / f"{stable_name_digest(str(path))}-{max_size}.jpg"
    if not resize_image_to_jpeg(path, out, max_size):
        msg = f"failed to resize image: {path}"
        raise RuntimeError(msg)
    return base64.b64encode(out.read_bytes()).decode("ascii"), "image/jpeg"


def bearer_token_from_service_account(credentials_file: Path) -> str:
    creds = service_account.Credentials.from_service_account_file(
        str(credentials_file), scopes=["https://www.googleapis.com/auth/generative-language"]
    )
    creds.refresh(Request())
    if not creds.token:
        msg = "failed to obtain Google OAuth token"
        raise RuntimeError(msg)
    return creds.token


def call_gemini_profile(
    *,
    model: str,
    image_b64: str,
    mime_type: str,
    prompt: str,
    timeout: int,
    api_key: str | None,
    bearer_token: str | None,
) -> Json:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    params: dict[str, str] = {}
    if api_key:
        params["key"] = api_key
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    else:  # pragma: no cover - guarded by caller
        msg = "Gemini API key or bearer token required"
        raise RuntimeError(msg)

    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    response = requests.post(url, params=params, headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def response_text(payload: Json) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(str(part.get("text", "")) for part in parts)
    except Exception as exc:  # noqa: BLE001
        msg = f"unexpected Gemini response shape: {payload.keys()}"
        raise RuntimeError(msg) from exc


def cache_key(record: Json, model: str, max_image_size: int) -> str:
    return "|".join(
        [str(record.get("sha256") or record.get("image_id")), model, EVIDENCE_PROFILE_PROMPT_VERSION, str(max_image_size)]
    )


def profile_images(args: argparse.Namespace) -> int:
    manifest = load_image_manifest(Path(args.manifest).resolve())
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "gemini_image_profile_cache.json"
    cache = load_ai_decision_cache(cache_path)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    bearer_token: str | None = None
    if not args.from_cache and not api_key:
        credentials_file = Path(args.google_credentials).expanduser().resolve()
        if not credentials_file.exists():
            print("ERROR: Gemini API key not set and Google credentials file missing", file=sys.stderr)
            return 2
        bearer_token = bearer_token_from_service_account(credentials_file)

    profiles: list[Json] = []
    with tempfile.TemporaryDirectory(prefix="jewelry-gemini-profile-") as tmp:
        tmpdir = Path(tmp)
        ready_records = [record for record in manifest if record.get("status") == "ready"]
        for index, record in enumerate(ready_records, start=1):
            key = cache_key(record, args.model, args.max_image_size)
            if key not in cache:
                if args.from_cache:
                    continue
                source = Path(str(record["source_path"]))
                print(f"Gemini profiling {index}/{len(ready_records)} {record['image_id']}: {source.name}", flush=True)
                image_b64, mime_type = resized_image_b64(source, tmpdir, args.max_image_size)
                raw = call_gemini_profile(
                    model=args.model,
                    image_b64=image_b64,
                    mime_type=mime_type,
                    prompt=image_profile_prompt(int(record.get("width") or 1), int(record.get("height") or 1)),
                    timeout=args.timeout,
                    api_key=api_key,
                    bearer_token=bearer_token,
                )
                text = response_text(raw)
                try:
                    profile = parse_image_profile_text(
                        text,
                        str(record["image_id"]),
                        int(record.get("width") or 1),
                        int(record.get("height") or 1),
                    )
                except JSONDecodeError:
                    # Gemini can occasionally return valid JSON followed by a duplicated
                    # JSON object or stray text despite responseMimeType=json. Keep the
                    # first complete JSON object so bounded batches can resume safely.
                    decoder = json.JSONDecoder()
                    first_payload, _ = decoder.raw_decode(text.strip())
                    profile = parse_image_profile_text(
                        json.dumps(first_payload, ensure_ascii=False),
                        str(record["image_id"]),
                        int(record.get("width") or 1),
                        int(record.get("height") or 1),
                    )
                cache[key] = {
                    "model": args.model,
                    "prompt_version": EVIDENCE_PROFILE_PROMPT_VERSION,
                    "max_image_size": args.max_image_size,
                    "image_sha256": record.get("sha256"),
                    "profile": profile,
                    "raw_response": raw,
                }
                write_json(cache_path, cache)
            profiles.append(cache[key]["profile"])

    write_json(cache_path, cache)
    write_json(out_dir / "image_profiles.json", profiles)
    summary = {
        "model": args.model,
        "profiles": len(profiles),
        "manifest_records": len(manifest),
        "auth_mode": "api_key" if api_key else "service_account_oauth",
        "output": str(out_dir),
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile local jewelry images with Gemini")
    parser.add_argument("--manifest", required=True, help="image_manifest JSON")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--model", default="gemini-2.5-flash-lite", help="Gemini model")
    parser.add_argument("--max-image-size", type=int, default=768, help="max image side sent to Gemini")
    parser.add_argument("--timeout", type=int, default=120, help="request timeout seconds")
    parser.add_argument("--from-cache", action="store_true", help="rebuild profiles from cache without API calls")
    parser.add_argument(
        "--google-credentials",
        default="/home/server/.openclaw/workspace/stav-drive-key.json",
        help="service account JSON for Google OAuth if GEMINI_API_KEY/GOOGLE_API_KEY is unset",
    )
    return parser


def main() -> int:
    return profile_images(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
