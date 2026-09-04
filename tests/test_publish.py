"""The Hub identifies small files by git blob id; our local hash must agree with git's."""

import subprocess

import publish_hf
import pytest


def test_git_blob_sha1_matches_git_hash_object(tmp_path):
    path = tmp_path / "MANIFEST.json"
    path.write_text('{"version": "0.2.0"}\n', encoding="utf-8")
    expected = subprocess.run(
        ["git", "hash-object", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert publish_hf.git_blob_sha1(path) == expected
    path.write_text('{"version": "0.2.1"}\n', encoding="utf-8")
    assert publish_hf.git_blob_sha1(path) != expected


def test_publisher_refuses_a_version_that_is_not_the_package(tmp_path):
    (tmp_path / "MANIFEST.json").write_text('{"version": "0.2.0"}', encoding="utf-8")
    publish_hf.check_package_version(tmp_path, "0.2.0")
    with pytest.raises(SystemExit, match="does not match MANIFEST.json"):
        publish_hf.check_package_version(tmp_path, "0.3.0")


def test_tag_plan_never_leaves_a_release_tag_on_a_stale_commit():
    assert publish_hf.tag_plan(None, "abc", retag=False) == "create"
    assert publish_hf.tag_plan("abc", "abc", retag=False) == "unchanged"
    assert publish_hf.tag_plan("old", "abc", retag=False) == "refuse"
    assert publish_hf.tag_plan("old", "abc", retag=True) == "move"


def test_files_of_a_tagged_version_change_only_with_retag():
    assert publish_hf.uploads_allowed(None, pending=True, retag=False)
    assert publish_hf.uploads_allowed("old", pending=False, retag=False)
    assert not publish_hf.uploads_allowed("old", pending=True, retag=False)
    assert publish_hf.uploads_allowed("old", pending=True, retag=True)


def test_published_bundle_includes_what_expand_and_rebuild_need(tmp_path):
    data = tmp_path / "data"
    release = data / "release"
    release.mkdir(parents=True)
    for name in ("pai_gp.parquet", "MANIFEST.json"):
        (release / name).write_bytes(b"x")
    for year in ("2022-2023", "2023-2024"):
        for stem in ("blocks", "html"):
            (data / f"{stem}_{year}.tar.zst").write_bytes(b"x")
        (data / f"compact_{year}.json").write_bytes(b"{}")
    for name in ("block_manifest.parquet", "dropdown_inventory.parquet", "block_count_audit.csv"):
        (data / name).write_bytes(b"x")
    files = publish_hf.collect_files(data, release, None)
    assert "archives/compact_2023-2024.json" in files
    assert "logs/block_manifest.parquet" in files
    assert "logs/block_count_audit.csv" in files
    (data / "compact_2023-2024.json").unlink()
    with pytest.raises(SystemExit, match="compact_2023-2024.json"):
        publish_hf.collect_files(data, release, None)


def test_published_bundle_includes_the_independent_universe(tmp_path):
    universe = tmp_path / "universe"
    (universe / "source" / "2023-2024" / "state=9").mkdir(parents=True)
    (universe / "source" / "2023-2024" / "state=9" / "districts.json").write_text("[]")
    (universe / "gp_universe.parquet").write_bytes(b"x")
    (universe / "collection_manifest.json").write_text("{}")
    release = tmp_path / "release"
    release.mkdir()
    for name in ("pai_gp.parquet", "MANIFEST.json"):
        (release / name).write_bytes(b"x")
    data = tmp_path / "data"
    data.mkdir()
    for year in ("2022-2023", "2023-2024"):
        for stem in ("blocks", "html"):
            (data / f"{stem}_{year}.tar.zst").write_bytes(b"x")
        (data / f"compact_{year}.json").write_text("{}")
    for name in ("block_manifest.parquet", "dropdown_inventory.parquet", "block_count_audit.csv"):
        (data / name).write_bytes(b"x")
    files = publish_hf.collect_files(data, release, None, universe)
    assert files["universe/gp_universe.parquet"].exists()
    archive = files["archives/universe_source.tar.zst"]
    assert archive.exists() and archive.stat().st_size > 0


def test_universe_archive_is_written_atomically_and_reads_back_complete(tmp_path):
    universe = tmp_path / "universe"
    for i in range(3):
        d = universe / "source" / "2023-2024" / f"state={i}"
        d.mkdir(parents=True)
        (d / "districts.json").write_text("[]")
    archive = publish_hf.universe_source_archive(universe)
    assert archive.exists()
    assert not [p for p in universe.iterdir() if p.name.startswith(".universe_source")]
    # A truncated leftover with a fresh mtime is rebuilt rather than trusted.
    good = archive.read_bytes()
    archive.write_bytes(good[: len(good) // 2])
    assert publish_hf.universe_source_archive(universe).read_bytes() == good


def test_universe_archive_refuses_an_empty_source(tmp_path):
    (tmp_path / "source").mkdir()
    with pytest.raises(SystemExit, match="no hierarchy handler responses"):
        publish_hf.universe_source_archive(tmp_path)
