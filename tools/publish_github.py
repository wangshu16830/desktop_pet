"""Publish this project to GitHub using only the REST API and GITHUB_TOKEN."""

import base64
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


OWNER = "wangshu16830"
REPO = "desktop_pet"
BRANCH = "main"
TAG = "v1.0.0"
ROOT = Path(__file__).resolve().parent.parent
API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPO}"

SOURCE_FILES = [
    "desktop_pet.py",
    "pet.json",
    "pet.example.json",
    "README.md",
    "requirements.txt",
    "build.ps1",
    ".gitignore",
    "LICENSE",
    "docs/abyssinian-desktop-pet-preview.png",
    "tools/publish_github.py",
]


def request(method, url, payload=None, headers=None, timeout=120):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set.")
    body = None
    request_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "desktop-pet-publisher",
    }
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=request_headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url}: HTTP {exc.code}: {detail}") from exc


def create_blob(path):
    data = path.read_bytes()
    if len(data) > 100 * 1024 * 1024:
        raise RuntimeError(f"Repository file exceeds GitHub's 100 MB API limit: {path}")
    payload = {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    }
    result = request("POST", f"{API_ROOT}/git/blobs", payload)
    return result["sha"]


def bootstrap_readme(readme):
    payload = {
        "message": "Initialize public repository",
        "branch": BRANCH,
        "content": base64.b64encode(readme.read_bytes()).decode("ascii"),
    }
    return request("PUT", f"{API_ROOT}/contents/README.md", payload)


def current_commit():
    try:
        return request("GET", f"{API_ROOT}/git/ref/heads/{BRANCH}")["object"]["sha"]
    except RuntimeError as exc:
        if "HTTP 409" in str(exc) or "HTTP 404" in str(exc):
            return None
        raise


def upload_release_asset(upload_url, archive):
    token = os.environ["GITHUB_TOKEN"]
    target = upload_url.split("{")[0] + "?" + urllib.parse.urlencode(
        {"name": archive.name}
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "desktop-pet-publisher",
        "Content-Type": "application/zip",
        "Content-Length": str(archive.stat().st_size),
    }
    req = urllib.request.Request(target, data=archive.read_bytes(), headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Release asset upload failed: HTTP {exc.code}: {detail}") from exc


def get_release_by_tag():
    try:
        return request("GET", f"{API_ROOT}/releases/tags/{TAG}")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--release-only", action="store_true")
    args = parser.parse_args()
    if args.source_only and args.release_only:
        raise RuntimeError("Choose only one of --source-only or --release-only.")

    if not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("GITHUB_TOKEN is not set.")

    archive = ROOT / "release" / "DesktopPet-clean-v1.0.0.zip"
    if not archive.is_file():
        raise RuntimeError(f"Release archive is missing: {archive}")

    paths = [ROOT / item for item in SOURCE_FILES]
    paths.extend(sorted(ROOT.glob("*.mp4")))
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError("Missing upload files: " + ", ".join(missing))

    if not args.release_only:
        parent_sha = current_commit()
        if parent_sha is None:
            print("Initializing empty repository...", flush=True)
            bootstrap_readme(ROOT / "README.md")
            parent_sha = current_commit()
        else:
            existing_tree = request("GET", f"{API_ROOT}/git/trees/{BRANCH}?recursive=1")
            existing_paths = {entry["path"] for entry in existing_tree.get("tree", [])
                              if entry["type"] == "blob"}
            if existing_paths != {"README.md"}:
                raise RuntimeError(
                    "The main branch contains files other than the bootstrap README. "
                    "Refusing to overwrite existing repository contents."
                )
            print("Resuming after bootstrap README commit...", flush=True)
        parent = request("GET", f"{API_ROOT}/git/commits/{parent_sha}")

        entries = []
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            if relative == "README.md":
                continue
            print(f"Uploading source file: {relative}", flush=True)
            entries.append({
                "path": relative,
                "mode": "100644",
                "type": "blob",
                "sha": create_blob(path),
            })

        tree = request("POST", f"{API_ROOT}/git/trees", {
            "base_tree": parent["tree"]["sha"],
            "tree": entries,
        })
        commit = request("POST", f"{API_ROOT}/git/commits", {
            "message": "Publish desktop pet source and media",
            "tree": tree["sha"],
            "parents": [parent_sha],
        })
        request("PATCH", f"{API_ROOT}/git/refs/heads/{BRANCH}", {
            "sha": commit["sha"],
            "force": False,
        })
        if args.source_only:
            print("Source upload complete.", flush=True)
            return

    release = get_release_by_tag()
    if release is None:
        print("Creating release v1.0.0...", flush=True)
        release = request("POST", f"{API_ROOT}/releases", {
            "tag_name": TAG,
            "target_commitish": BRANCH,
            "name": "Desktop Pet v1.0.0",
            "body": (
                "Windows x64 release. Download and fully extract "
                "DesktopPet-clean-v1.0.0.zip, then run DesktopPet.exe."
            ),
            "draft": False,
            "prerelease": False,
        })
    existing_asset = next(
        (item for item in release.get("assets", []) if item["name"] == archive.name), None
    )
    if existing_asset is None:
        print(f"Uploading release asset: {archive.name}", flush=True)
        asset = upload_release_asset(release["upload_url"], archive)
    else:
        asset = existing_asset
        print(f"Release asset already exists: {archive.name}", flush=True)

    print("Repository URL:", f"https://github.com/{OWNER}/{REPO}", flush=True)
    print("Release URL:", release["html_url"], flush=True)
    print("Release asset URL:", asset["browser_download_url"], flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Publish failed: {exc}", file=sys.stderr)
        sys.exit(1)
