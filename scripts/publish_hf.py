#!/usr/bin/env python3
"""Publish the PAI release table and provenance archives to a Hugging Face dataset.

Uploads, then verifies every file against the Hub by size and SHA-256 (LFS OID),
so a truncated upload cannot pass silently.

  release/pai_gp.parquet, release/MANIFEST.json   the versioned analysis table
  archives/blocks_<year>.tar.zst                   per-block typed Parquet cache
  archives/html_<year>.tar.zst                     rendered page captures
  archives/compact_<year>.json                     member checksums `make expand` verifies
  universe/gp_universe.parquet, universe/collection_manifest.json, archives/universe_source.tar.zst
  logs/block_manifest.parquet, logs/dropdown_inventory.parquet, logs/block_count_audit.csv
  README.md                                        dataset card (from --card)

Usage:
  uv run scripts/publish_hf.py --repo soodoku/pai --version 0.2.0 [--private] [--dry-run]
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import YEARS  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    """The Hub reports small (non-LFS) files by their git blob id; compute ours the same way."""
    content = path.read_bytes()
    digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content)  # noqa: S324
    return digest.hexdigest()


def check_package_version(release_dir: Path, version: str) -> None:
    """The tag must name the package that is actually being uploaded."""
    manifest = json.loads((release_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    if str(manifest.get("version")) != version:
        raise SystemExit(
            f"--version {version} does not match MANIFEST.json version {manifest.get('version')}"
        )


def tag_plan(existing_target: str | None, head: str, retag: bool) -> str:
    """What to do with the release tag once the uploads are on `main`.

    A tag that already points at the verified head needs nothing; one that points
    elsewhere is a stale release label and is moved only when asked for explicitly.
    """
    if existing_target is None:
        return "create"
    if existing_target == head:
        return "unchanged"
    return "move" if retag else "refuse"


def uploads_allowed(existing_target: str | None, pending: bool, retag: bool) -> bool:
    """A tagged version's files may change only when retagging was asked for.

    Checked before the first upload: otherwise `main` would already hold the new
    files by the time the tag refusal fires, and the tag would no longer describe
    what the dataset serves.
    """
    return existing_target is None or not pending or retag


def universe_source_archive(universe_dir: Path) -> Path:
    """Solid zstd tar of the raw hierarchy handler responses, rebuilt when stale."""
    import tarfile
    from compression.zstd import ZstdFile

    from pai_stores import zstd_read_options, zstd_write_options

    source = universe_dir / "source"
    dst = universe_dir / "universe_source.tar.zst"
    members = sorted(p for p in source.rglob("*") if p.is_file())
    if not members:
        raise SystemExit(f"{source}: no hierarchy handler responses to archive")
    newest = max(p.stat().st_mtime for p in members)

    def complete(path: Path) -> bool:
        try:
            with ZstdFile(path, "rb", options=zstd_read_options()) as zf:
                with tarfile.open(fileobj=zf, mode="r|") as tar:
                    return sum(1 for member in tar if member.isfile()) == len(members)
        except Exception:  # any unreadable archive is incomplete for our purposes
            return False

    if not dst.exists() or dst.stat().st_mtime < newest or not complete(dst):
        # Write beside the target and promote only after the archive reads back
        # complete, so an interrupted run can never leave a truncated archive
        # with a fresh mtime that the next run would trust.
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
        try:
            with ZstdFile(tmp, "wb", options=zstd_write_options()) as zf:
                with tarfile.open(fileobj=zf, mode="w|") as tar:
                    for path in members:
                        tar.add(path, arcname=path.relative_to(universe_dir).as_posix())
            if not complete(tmp):
                raise SystemExit(f"{dst}: archive did not read back with {len(members)} files")
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    return dst


INDICATORS_CSV = Path(__file__).resolve().parents[1] / "docs" / "pai_indicators.csv"


def collect_files(
    data_dir: Path, release_dir: Path, card: Path | None, universe_dir: Path | None = None
) -> dict[str, Path]:
    files = {
        "release/pai_gp.parquet": release_dir / "pai_gp.parquet",
        "release/MANIFEST.json": release_dir / "MANIFEST.json",
    }
    if universe_dir is not None:
        # The release denominator: the rebuild refuses national controls without it.
        files["universe/gp_universe.parquet"] = universe_dir / "gp_universe.parquet"
        files["universe/collection_manifest.json"] = universe_dir / "collection_manifest.json"
        files["archives/universe_source.tar.zst"] = universe_source_archive(universe_dir)
    for year in YEARS:
        for stem in ("blocks", "html"):
            files[f"archives/{stem}_{year}.tar.zst"] = data_dir / f"{stem}_{year}.tar.zst"
        # `make expand` verifies every restored member against this manifest.
        files[f"archives/compact_{year}.json"] = data_dir / f"compact_{year}.json"
    # The append-only logs are not reproducible from the block tree.
    for stem in ("block_manifest", "dropdown_inventory"):
        files[f"logs/{stem}.parquet"] = data_dir / f"{stem}.parquet"
    files["logs/block_count_audit.csv"] = data_dir / "block_count_audit.csv"
    files["docs/pai_indicators.csv"] = INDICATORS_CSV
    if card is not None:
        files["README.md"] = card
    missing = [remote for remote, local in files.items() if not local.exists()]
    if missing:
        raise SystemExit(f"missing local files: {missing}")
    return files


def remote_mismatches(api: HfApi, repo: str, files: dict[str, Path]) -> dict[str, str]:
    """Files whose Hub size or LFS sha256 differs from the local copy (or are absent)."""
    problems: dict[str, str] = {}
    infos = {
        info.path: info
        for info in api.get_paths_info(repo, list(files), repo_type="dataset", expand=True)
    }
    for remote, local in files.items():
        info = infos.get(remote)
        if info is None:
            problems[remote] = "not on the Hub"
            continue
        size = local.stat().st_size
        if info.size != size:
            problems[remote] = f"size {info.size} != local {size}"
        elif info.lfs is not None:
            if info.lfs.sha256 != sha256_file(local):
                problems[remote] = "sha256 differs from local"
        elif info.blob_id != git_blob_sha1(local):
            problems[remote] = "content differs from local"
    return problems


def upload_with_retries(api: HfApi, repo: str, remote: str, local: Path, message: str) -> None:
    """One commit per file so a dropped connection costs one file, not the release."""
    for attempt in range(1, 4):
        try:
            api.upload_file(
                path_or_fileobj=str(local),
                path_in_repo=remote,
                repo_id=repo,
                repo_type="dataset",
                commit_message=message,
            )
            return
        except Exception as exc:  # network drops mid-transfer are the expected failure
            print(f"  {remote}: attempt {attempt} failed: {type(exc).__name__}: {exc}"[:200])
            time.sleep(30 * attempt)
    raise SystemExit(f"{remote}: upload failed after 3 attempts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="Hub dataset id, e.g. soodoku/pai")
    parser.add_argument("--version", required=True, help="Release version, used as the commit tag")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--release-dir", default="data/release")
    parser.add_argument("--card", help="Dataset card markdown to upload as README.md")
    parser.add_argument(
        "--universe-dir",
        default="runs/pai_universe",
        help="Independent universe crawl to publish alongside the release",
    )
    parser.add_argument("--private", action="store_true", help="Create the repo private")
    parser.add_argument(
        "--retag",
        action="store_true",
        help="Move an existing v<version> tag to the verified head (unreleased versions only)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List what would be uploaded")
    args = parser.parse_args()

    check_package_version(Path(args.release_dir), args.version)
    files = collect_files(
        Path(args.data_dir),
        Path(args.release_dir),
        Path(args.card) if args.card else None,
        Path(args.universe_dir) if args.universe_dir else None,
    )
    total = sum(local.stat().st_size for local in files.values())
    for remote, local in files.items():
        print(f"{remote:40s} {local.stat().st_size / 1e6:8.1f} MB  <- {local}")
    print(f"{'total':40s} {total / 1e6:8.1f} MB")
    if args.dry_run:
        return

    api = HfApi()
    api.create_repo(args.repo, repo_type="dataset", private=args.private, exist_ok=True)
    tag = f"v{args.version}"
    refs = api.list_repo_refs(args.repo, repo_type="dataset")
    existing = next((t.target_commit for t in refs.tags if t.name == tag), None)
    pending = remote_mismatches(api, args.repo, files)
    if not uploads_allowed(existing, bool(pending), args.retag):
        raise SystemExit(
            f"tag {tag} already exists and {len(pending)} file(s) differ: "
            + ", ".join(pending)
            + "; pass --retag only if that version was never released"
        )
    for remote in files:
        if remote not in pending:
            print(f"  {remote}: already on the Hub, identical")
            continue
        print(f"  {remote}: uploading ({pending[remote]})", flush=True)
        upload_with_retries(
            api, args.repo, remote, files[remote], f"PAI data release v{args.version}: {remote}"
        )
    problems = remote_mismatches(api, args.repo, files)
    if problems:
        raise SystemExit(
            "verification FAILED:\n  " + "\n  ".join(f"{k}: {v}" for k, v in problems.items())
        )
    refs = api.list_repo_refs(args.repo, repo_type="dataset")
    head = next(branch.target_commit for branch in refs.branches if branch.name == "main")
    existing = next((t.target_commit for t in refs.tags if t.name == tag), None)
    plan = tag_plan(existing, head, args.retag)
    if plan == "refuse":
        raise SystemExit(
            f"tag {tag} already points at {existing[:8]}, not the verified head {head[:8]}; "
            "pass --retag only if that version was never released"
        )
    if plan == "move":
        api.delete_tag(args.repo, tag=tag, repo_type="dataset")
    if plan in ("create", "move"):
        api.create_tag(args.repo, tag=tag, repo_type="dataset", revision=head)
    print(f"tag {tag}: {plan} -> {head[:8]}")
    print(f"verified {len(files)} files on https://huggingface.co/datasets/{args.repo}")


if __name__ == "__main__":
    main()
