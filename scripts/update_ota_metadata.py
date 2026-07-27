#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def newest_artifact(pattern: str) -> Path:
    matches = [Path(item).resolve() for item in glob.glob(pattern)]
    if not matches:
        raise RuntimeError(f"no OTA artifact matches {pattern}")
    return max(matches, key=lambda item: item.stat().st_mtime)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ota_timestamp(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = archive.read("META-INF/com/android/metadata").decode(errors="replace")
        for line in metadata.splitlines():
            if line.startswith("post-timestamp="):
                return int(line.partition("=")[2])
    except (KeyError, ValueError, zipfile.BadZipFile):
        pass
    return int(path.stat().st_mtime or time.time())


def ota_metadata(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            text = archive.read("META-INF/com/android/metadata").decode(errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key and value:
            result[key] = value
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish one Lineage-compatible OTA entry")
    parser.add_argument("--device", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-type", required=True,
                        help="Value of ro.lineage.releasetype, for example UNOFFICIAL or NIGHTLY")
    parser.add_argument("--artifact", required=True, help="Trusted artifact glob")
    parser.add_argument("--family", default="LineageOS")
    parser.add_argument("--flavor", choices=("gms", "vanilla"), required=True)
    parser.add_argument("--repository", default="/home/ubuntu/aosp/lineage-ota")
    args = parser.parse_args()

    if not all(re.fullmatch(r"[A-Za-z0-9._+-]+", part) for part in
               (args.device, args.version, args.release_type, args.family)):
        raise RuntimeError("invalid OTA metadata argument")

    artifact = newest_artifact(args.artifact)
    build_date = datetime.fromtimestamp(ota_timestamp(artifact)).strftime("%Y%m%d")
    filename = artifact.name
    url = (
        "https://sourceforge.net/projects/yuis-release/files/"
        f"{quote(args.family)}/{quote(args.device)}/{build_date}/{quote(filename)}/download"
    )
    metadata = ota_metadata(artifact)
    file_entry: dict[str, str | int] = {
        "filename": filename,
        "sha256": sha256(artifact),
        "size": artifact.stat().st_size,
        "url": url,
    }
    if metadata.get("ota-property-files"):
        file_entry["ota_property_files"] = metadata["ota-property-files"]
    if metadata.get("post-security-patch-level"):
        file_entry["os_patch_level"] = metadata["post-security-patch-level"]
    if metadata.get("post-sdk-level", "").isdigit():
        file_entry["os_sdk_level"] = int(metadata["post-sdk-level"])
    entry = {
        "datetime": ota_timestamp(artifact),
        "files": [file_entry],
        "type": args.release_type.lower(),
        "version": args.version,
        "variant": args.flavor,
    }

    repository = Path(args.repository)
    key = Path(os.environ.get("OTA_DEPLOY_KEY", "/home/ubuntu/.ssh/id_rsa"))
    git_env = os.environ.copy()
    git_env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
        "-o 'ProxyCommand=nc -X connect -x 127.0.0.1:7890 %h %p'"
    )
    if not (repository / ".git").is_dir():
        repository.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "git@github.com:LineageOS-Sado/ota.git", str(repository),
            cwd=repository.parent, env=git_env)
    run("git", "pull", "--ff-only", "origin", "main", cwd=repository, env=git_env)
    run("git", "config", "user.name", "YuisWorkSpace CI", cwd=repository)
    run("git", "config", "user.email", "ci@lineageos-sado.local", cwd=repository)

    output = repository / f"{args.device}.json"
    existing: list[dict[str, object]] = []
    if output.is_file():
        try:
            loaded = json.loads(output.read_text())
            if isinstance(loaded, list):
                existing = [item for item in loaded if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
    payload = [
        item for item in existing
        if item.get("variant") in ("gms", "vanilla") and item.get("variant") != args.flavor
    ]
    payload.append(entry)
    payload.sort(key=lambda item: str(item.get("variant", "")), reverse=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    run("git", "add", output.name, cwd=repository)
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repository, check=False
    ).returncode != 0
    if changed:
        run("git", "commit", "-m",
            f"ota: publish {args.device} {args.flavor} {build_date} {args.release_type}",
            cwd=repository)
        run("git", "push", "origin", "main", cwd=repository, env=git_env)
        print(f"OTA metadata published: {args.device}.json [{args.flavor}] -> {filename}")
    else:
        print(f"OTA metadata unchanged: {args.device}.json")


if __name__ == "__main__":
    main()
