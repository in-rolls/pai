"""The committed data package must be small, typed, and keyed to its universe."""

import json

import build_data_package as package
import pai_common as c
import pai_contracts
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import verify_data_package


@pytest.fixture(autouse=True)
def _small_release_contract(monkeypatch):
    monkeypatch.setattr(package, "REQUIRED_RELEASE_YEARS", ("2023-2024",))
    monkeypatch.setattr(
        package,
        "OFFICIAL_FINAL_GP_COUNTS",
        {"2023-2024": {"Uttar Pradesh": 1, "__india__": 1}},
    )
    monkeypatch.setattr(package, "load_score_value_exceptions", lambda: {})


def _derived(
    root, *, universe_code="100", score_code="100", score_value=50.0, extra_unscored=False
):
    root.mkdir()
    score_row = {
        "year": "2023-2024",
        "state": "Uttar Pradesh",
        "state_value": "9",
        "district": "D",
        "district_value": "1",
        "block": "B",
        "block_value": "2",
        "gp_name": "GP",
        "gp_code": score_code,
        "scorecard_url": "/PS/Public/SC.aspx?gp_id=MTAw",
        **{field: score_value for field in package.SCORE_FIELDS},
    }
    score_schema = pa.schema(
        [
            *[pa.field(field, pa.string()) for field in package.SCORE_ID_FIELDS],
            *[pa.field(field, pa.float64()) for field in package.SCORE_FIELDS],
        ]
    )
    pq.write_table(
        pa.Table.from_pylist([score_row], schema=score_schema),
        root / "gp_scores_wide.parquet",
    )
    universe_rows = []
    for code in [universe_code, "101"] if extra_unscored else [universe_code]:
        universe_rows.append(
            {
                "year": "2023-2024",
                "state": "Uttar Pradesh",
                "state_value": "9",
                "district": "D",
                "district_value": "1",
                "block": "B",
                "block_value": "2",
                "gp_code": code,
                "gp_name": "GP" if code == universe_code else "Unscored GP",
                "source_url": "https://pai.gov.in/handler",
                "retrieved_utc": "2026-09-01T00:00:00+00:00",
                "source_sha256": "a" * 64,
            }
        )
    pq.write_table(
        pa.Table.from_pylist(
            universe_rows, schema=pai_contracts.typed_schema(c.GP_UNIVERSE_FIELDS, "universe")
        ),
        root / "gp_universe.parquet",
    )
    derived = {}
    for filename in ("gp_scores_wide.parquet", "gp_universe.parquet"):
        path = root / filename
        derived[filename] = {
            "rows": pq.read_metadata(path).num_rows,
            "sha256": package.sha256_file(path),
            "schema": str(pq.read_schema(path)),
        }
    (root / "collection_manifest.json").write_text(json.dumps({"derived": derived}))


def test_data_package_builds_one_universe_left_parquet(tmp_path):
    derived = tmp_path / "derived"
    _derived(derived)
    out = tmp_path / "release"
    manifest = package.build(derived, out)

    assert sorted(path.name for path in out.iterdir()) == [
        "MANIFEST.json",
        "pai_gp.parquet",
    ]
    assert pq.read_table(out / "pai_gp.parquet").column_names == package.PUBLIC_FIELDS
    assert manifest["files"]["pai_gp.parquet"]["rows"] == 1
    assert manifest["key"] == ["year", "gp_code"]
    assert verify_data_package.verify(out)["version"] == package.package_version()

    (out / "unexpected.txt").write_text("not part of the release", encoding="utf-8")
    with pytest.raises(AssertionError, match="members differ"):
        verify_data_package.verify(out)


@pytest.mark.parametrize(
    ("universe_code", "score_code", "message"),
    [
        ("101", "100", "scores contain GPs outside the universe"),
        ("100", "", "identity field gp_code contains blanks"),
    ],
)
def test_data_package_refuses_key_defects(tmp_path, universe_code, score_code, message):
    derived = tmp_path / "derived"
    _derived(derived, universe_code=universe_code, score_code=score_code)
    with pytest.raises(AssertionError, match=message):
        package.build(derived, tmp_path / "release")


def test_data_package_refuses_missing_vintage(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    _derived(derived)
    monkeypatch.setattr(package, "REQUIRED_RELEASE_YEARS", ("2022-2023", "2023-2024"))
    with pytest.raises(AssertionError, match="release vintages differ"):
        package.build(derived, tmp_path / "release")


def test_data_package_refuses_unmanifested_source_change(tmp_path):
    derived = tmp_path / "derived"
    _derived(derived)
    path = derived / "collection_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["derived"]["gp_scores_wide.parquet"]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match="derived collection manifest differs"):
        package.build(derived, tmp_path / "release")


@pytest.mark.parametrize("score_value", [None, float("nan"), 101.0])
def test_data_package_refuses_unreviewed_or_invalid_scores(tmp_path, score_value):
    derived = tmp_path / "derived"
    _derived(derived, score_value=score_value)
    with pytest.raises(AssertionError, match="score"):
        package.build(derived, tmp_path / "release")


def test_verifier_rechecks_manifested_controls(tmp_path):
    derived = tmp_path / "derived"
    _derived(derived)
    out = tmp_path / "release"
    package.build(derived, out)
    path = out / "MANIFEST.json"
    manifest = json.loads(path.read_text())
    manifest["official_counts_checked"] = {}
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match="official-count checks"):
        verify_data_package.verify(out)


def test_data_package_refuses_git_warning_sized_files(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    _derived(derived)
    monkeypatch.setattr(package, "MAX_GIT_FILE_BYTES", 1)
    with pytest.raises(AssertionError, match="50 MiB Git warning threshold"):
        package.build(derived, tmp_path / "release")


def test_unscored_universe_gp_is_retained_with_null_scores(tmp_path):
    derived = tmp_path / "derived"
    _derived(derived, extra_unscored=True)
    out = tmp_path / "release"
    manifest = package.build(derived, out)
    rows = pq.read_table(out / "pai_gp.parquet").to_pylist()
    assert len(rows) == 2
    unscored = next(row for row in rows if row["gp_code"] == "101")
    assert unscored["score_available"] is False
    assert unscored["scorecard_url"] is None
    assert all(unscored[field] is None for field in package.SCORE_FIELDS)
    assert manifest["coverage"]["2023-2024"] == {
        "universe_rows": 2,
        "scored_rows": 1,
        "unscored_rows": 1,
    }
    assert manifest["coverage_by_state"]["2023-2024:9"] == {
        "state": "Uttar Pradesh",
        "universe_rows": 2,
        "scored_rows": 1,
        "unscored_rows": 1,
    }
