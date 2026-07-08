#!/usr/bin/env python3
"""Pull an OCI image with resumable blob downloads and write an OCI archive.

This is useful for large private registry layers on unreliable links where
`docker pull` restarts a large blob from zero after an unexpected EOF.
Authentication is read from Docker's config.json.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import requests


INDEX_ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_ACCEPT = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


def parse_ref(ref: str) -> tuple[str, str, str]:
    if "/" not in ref:
        raise SystemExit(f"image ref must include a registry: {ref}")
    registry, rest = ref.split("/", 1)
    if "@" in rest:
        repo, reference = rest.split("@", 1)
    else:
        repo, sep, tag = rest.rpartition(":")
        if not sep or "/" in tag:
            repo = rest
            tag = "latest"
        reference = tag
    return registry, repo, reference


def docker_auth(registry: str) -> tuple[str, str] | None:
    docker_config = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))) / "config.json"
    if not docker_config.exists():
        return None
    data = json.loads(docker_config.read_text())
    auths = data.get("auths", {})
    entry = auths.get(registry) or auths.get(f"https://{registry}") or auths.get(f"http://{registry}")
    if not entry:
        return None
    if entry.get("auth"):
        decoded = base64.b64decode(entry["auth"]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username, password
    if entry.get("username") and entry.get("password"):
        return entry["username"], entry["password"]
    return None


def get_token(registry: str, repo: str) -> str:
    auth = docker_auth(registry)
    params = {"service": registry, "scope": f"repository:{repo}:pull"}
    response = requests.get(f"https://{registry}/token", params=params, auth=auth, timeout=30)
    response.raise_for_status()
    token = response.json().get("token") or response.json().get("access_token")
    if not token:
        raise RuntimeError("registry token response did not include a token")
    return token


def get_json(url: str, token: str, accept: str) -> tuple[dict[str, Any], str, str]:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": accept},
        timeout=60,
    )
    response.raise_for_status()
    digest = response.headers.get("Docker-Content-Digest", "")
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
    return response.json(), digest, media_type


def choose_manifest(index: dict[str, Any], os_name: str, arch: str) -> dict[str, Any]:
    for manifest in index.get("manifests", []):
        platform = manifest.get("platform", {})
        if platform.get("os") == os_name and platform.get("architecture") == arch:
            return manifest
    raise RuntimeError(f"no {os_name}/{arch} manifest found")


def digest_path(root: Path, digest: str) -> Path:
    algo, hex_digest = digest.split(":", 1)
    if algo != "sha256":
        raise RuntimeError(f"unsupported digest algorithm: {digest}")
    return root / "blobs" / "sha256" / hex_digest


def verify_digest(path: Path, digest: str) -> None:
    algo, expected = digest.split(":", 1)
    if algo != "sha256":
        raise RuntimeError(f"unsupported digest algorithm: {digest}")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(f"digest mismatch for {path}: got sha256:{actual}, expected {digest}")


def download_blob(registry: str, repo: str, digest: str, token: str, target: Path, expected_size: int | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    if target.exists():
        if expected_size is None or target.stat().st_size == expected_size:
            verify_digest(target, digest)
            print(f"exists {digest} ({target.stat().st_size} bytes)", flush=True)
            return
        target.rename(partial)

    while True:
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"
        print(f"download {digest} from byte {offset}", flush=True)
        try:
            with requests.get(
                f"https://{registry}/v2/{repo}/blobs/{digest}",
                headers=headers,
                stream=True,
                timeout=(30, 120),
            ) as response:
                if response.status_code == 416 and expected_size is not None and offset == expected_size:
                    break
                if offset and response.status_code == 200:
                    print("server ignored Range; restarting partial download", flush=True)
                    partial.unlink(missing_ok=True)
                    continue
                response.raise_for_status()
                mode = "ab" if offset and response.status_code == 206 else "wb"
                downloaded = offset if mode == "ab" else 0
                with partial.open(mode) as f:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if expected_size and downloaded // (512 * 1024 * 1024) != (downloaded - len(chunk)) // (512 * 1024 * 1024):
                            print(f"  {downloaded}/{expected_size} bytes", flush=True)
        except requests.RequestException as exc:
            kept = partial.stat().st_size if partial.exists() else 0
            print(f"stream interrupted at byte {kept}: {exc}; resuming", flush=True)
            time.sleep(5)
            continue

        if expected_size is None or partial.stat().st_size >= expected_size:
            break

    if expected_size is not None and partial.stat().st_size != expected_size:
        raise RuntimeError(f"size mismatch for {digest}: got {partial.stat().st_size}, expected {expected_size}")
    verify_digest(partial, digest)
    partial.rename(target)
    print(f"complete {digest} ({target.stat().st_size} bytes)", flush=True)


def safe_name(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", ref)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--platform-os", default="linux")
    parser.add_argument("--platform-arch", default="amd64")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--archive", default="")
    parser.add_argument(
        "--only-digest",
        action="append",
        default=[],
        help="Download only the listed manifest blob digest(s), then exit without writing an OCI archive.",
    )
    args = parser.parse_args()

    registry, repo, reference = parse_ref(args.image)
    work_dir = Path(args.work_dir or f"/tmp/{safe_name(args.image)}-oci")
    archive = Path(args.archive or f"{work_dir}.tar")
    blobs_dir = work_dir / "blobs" / "sha256"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    token = get_token(registry, repo)
    index, index_digest, index_media_type = get_json(
        f"https://{registry}/v2/{repo}/manifests/{reference}",
        token,
        INDEX_ACCEPT,
    )
    if index.get("manifests"):
        descriptor = choose_manifest(index, args.platform_os, args.platform_arch)
        manifest_digest = descriptor["digest"]
        platform = descriptor.get("platform", {"os": args.platform_os, "architecture": args.platform_arch})
        manifest, returned_digest, manifest_media_type = get_json(
            f"https://{registry}/v2/{repo}/manifests/{manifest_digest}",
            token,
            MANIFEST_ACCEPT,
        )
        if returned_digest and returned_digest != manifest_digest:
            raise RuntimeError(f"manifest digest mismatch: {returned_digest} != {manifest_digest}")
    else:
        manifest = index
        manifest_digest = index_digest
        manifest_media_type = index_media_type
        platform = {"os": args.platform_os, "architecture": args.platform_arch}

    if args.only_digest:
        wanted = set(args.only_digest)
        descriptors = [manifest["config"], *manifest.get("layers", [])]
        by_digest = {item["digest"]: item for item in descriptors}
        missing = sorted(wanted - set(by_digest))
        if missing:
            raise RuntimeError(f"digest(s) not present in selected manifest: {', '.join(missing)}")
        for digest in args.only_digest:
            item = by_digest[digest]
            download_blob(registry, repo, digest, token, digest_path(work_dir, digest), item.get("size"))
        return 0

    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=False).encode("utf-8")
    computed_manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if computed_manifest_digest != manifest_digest:
        raise RuntimeError(f"manifest digest mismatch after serialization: {computed_manifest_digest} != {manifest_digest}")
    manifest_path = digest_path(work_dir, manifest_digest)
    manifest_path.write_bytes(manifest_bytes)

    config = manifest["config"]
    download_blob(registry, repo, config["digest"], token, digest_path(work_dir, config["digest"]), config.get("size"))
    for layer in manifest.get("layers", []):
        download_blob(registry, repo, layer["digest"], token, digest_path(work_dir, layer["digest"]), layer.get("size"))

    (work_dir / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}) + "\n")
    index_doc = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": manifest_media_type or manifest.get("mediaType", "application/vnd.oci.image.manifest.v1+json"),
                "digest": manifest_digest,
                "size": manifest_path.stat().st_size,
                "platform": platform,
                "annotations": {"org.opencontainers.image.ref.name": args.image},
            }
        ],
    }
    (work_dir / "index.json").write_text(json.dumps(index_doc, indent=2) + "\n")

    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
    tmp_archive.unlink(missing_ok=True)
    with tarfile.open(tmp_archive, "w") as tar:
        for name in ("oci-layout", "index.json", "blobs"):
            tar.add(work_dir / name, arcname=name)
    tmp_archive.replace(archive)
    print(f"wrote {archive}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
