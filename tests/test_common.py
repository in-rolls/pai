"""Unit tests for the shared pai_common helpers."""

from pathlib import Path

import pai_common as c


def test_csv_roundtrip(tmp_path):
    path = tmp_path / "x.csv"
    rows = [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    c.write_csv_rows(path, rows, ["a", "b"])
    assert c.read_csv(path) == rows


def test_read_csv_missing_returns_empty(tmp_path):
    assert c.read_csv(tmp_path / "nope.csv") == []


def test_json_roundtrip_preserves_unicode(tmp_path):
    path = tmp_path / "x.json"
    obj = {"status": "done", "gp_rows": 3, "name": "Thāngaon"}
    c.write_json(path, obj)
    assert c.read_json(path) == obj


def test_append_csv_rows_writes_header_once(tmp_path):
    path = tmp_path / "a.csv"
    c.append_csv_rows(path, [{"a": "1", "b": "2"}], ["a", "b"])
    c.append_csv_rows(path, [{"a": "3", "b": "4"}], ["a", "b"])
    rows = c.read_csv(path)
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_read_block_status_prefers_done(tmp_path):
    bd = tmp_path / "B__1"
    bd.mkdir()
    c.write_json(bd / "FAILED.json", {"status": "failed"})
    c.write_json(bd / "DONE.json", {"status": "done", "gp_rows": 5})
    assert c.read_block_status(bd)["status"] == "done"


def test_read_block_status_none_when_absent(tmp_path):
    assert c.read_block_status(tmp_path) is None


def test_append_csv_rows_header_once_when_another_worker_wrote_while_we_waited(
    tmp_path, monkeypatch
):
    """Worker B finds an empty log, then blocks on the lock while A writes header+rows."""
    path = tmp_path / "log.csv"
    real_lock = c.FileLock

    class LockHeldByOtherWorkerFirst:
        def __init__(self, lock_path):
            self.inner = real_lock(lock_path)

        def __enter__(self):
            self.inner.__enter__()
            if not path.exists():
                path.write_text("a\n1\n", encoding="utf-8")  # A finished during our wait
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    monkeypatch.setattr(c, "FileLock", LockHeldByOtherWorkerFirst)
    c.append_csv_rows(path, [{"a": "2"}], ["a"])
    assert path.read_text(encoding="utf-8").splitlines() == ["a", "1", "2"]


def test_write_json_leaves_no_partial_file_and_replaces_atomically(tmp_path):
    path = tmp_path / "DONE.json"
    c.write_json(path, {"status": "done"})
    c.write_json(path, {"status": "done", "gp_rows": 3})
    assert c.read_json(path) == {"status": "done", "gp_rows": 3}
    assert [p.name for p in tmp_path.iterdir()] == ["DONE.json"]


def test_write_json_goes_through_an_atomic_replace(tmp_path, monkeypatch):
    replaced = []
    real_replace = c.os.replace

    def spy(src, dst):
        replaced.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(c.os, "replace", spy)
    c.write_json(tmp_path / "DONE.json", {"status": "done"})
    assert replaced and replaced[0][1] == "DONE.json" and replaced[0][0] != "DONE.json"
