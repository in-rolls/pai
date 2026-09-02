#!/usr/bin/env python3
"""Run irreversible-release preflight without creating a tag."""

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

from verify_data_package import verify

ROOT = Path(__file__).parent.parent


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def check(version: str) -> None:
    expected = project_version()
    if version != expected:
        raise AssertionError(f"requested version {version} differs from project version {expected}")
    if git("status", "--porcelain"):
        raise AssertionError("working tree is not clean")
    if git("branch", "--show-current") != "main":
        raise AssertionError("release candidate must be on main")
    tag = f"v{version}"
    if git("tag", "-l", tag):
        raise AssertionError(f"tag {tag} already exists")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = rf"^## v{re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}$"
    if re.search(pattern, changelog, flags=re.MULTILINE) is None:
        raise AssertionError(f"CHANGELOG.md lacks a dated v{version} entry")
    manifest = verify(ROOT / "data" / "release")
    print(
        f"Ready for independent review: v{version}, "
        f"{manifest['files']['pai_gp.parquet']['rows']:,} GP-year rows"
    )
    print(f"After review and green CI: git tag -a {tag} -m 'Release {tag}'")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    check(args.version)


if __name__ == "__main__":
    main()
