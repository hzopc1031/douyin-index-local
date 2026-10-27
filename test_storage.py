"""Offline cache and persistence behavior tests; no browser or network access."""
import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storage import ResultStore, _atomic_write


def fixture(words=None):
    words = words or ["酵素", "银杏"]
    days = [(date(2026, 10, 19) + timedelta(days=i)).isoformat() for i in range(7)]
    return {"metadata": {"keywords": words, "start_date": days[0], "end_date": days[-1],
                          "complete": True, "fetched_at": datetime.now(timezone.utc).isoformat()},
            "rows": [{"keyword": word, "date": day, "search_index": 100 + i, "composite_index": None}
                     for word in words for i, day in enumerate(days)], "raw": {"response": {"status": 0}}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "results"
        self.store = ResultStore(self.root)

    def lookup(self, words=None):
        return self.store.lookup(words or ["酵素", "银杏"], "2026-10-19", "2026-10-25")

    def test_roundtrip_scope_permissions_and_unique_batches(self):
        data = fixture()
        before = copy.deepcopy(data)
        first = Path(self.store.save(data))
        second = Path(self.store.save(data))
        self.assertNotEqual(first, second)
        self.assertEqual(data, before)
        hit = self.lookup()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["result"]["metadata"]["region"], "全国")
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        for name in ("result.json", "search-index.csv"):
            self.assertEqual((first / name).stat().st_mode & 0o777, 0o600)
        self.assertTrue((first / "search-index.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_query_order_dates_and_scope_are_exact(self):
        folder = Path(self.store.save(fixture()))
        self.assertIsNone(self.lookup(["银杏", "酵素"]))
        self.assertIsNone(self.store.lookup(["酵素", "银杏"], "2026-10-18", "2026-10-24"))
        original = json.loads((folder / "result.json").read_text())
        for key, value in (("region", "上海"), ("platform", "toutiao"), ("metric", "composite_index")):
            data = copy.deepcopy(original)
            data["metadata"][key] = value
            (folder / "result.json").write_text(json.dumps(data))
            self.assertIsNone(self.lookup())

    def test_expired_future_and_naive_timestamps_miss(self):
        folder = Path(self.store.save(fixture()))
        data = json.loads((folder / "result.json").read_text())
        now = datetime.now(timezone.utc)
        for stamp in ((now - timedelta(hours=25)).isoformat(), (now + timedelta(seconds=60)).isoformat(), now.replace(tzinfo=None).isoformat()):
            data["metadata"]["fetched_at"] = stamp
            (folder / "result.json").write_text(json.dumps(data))
            self.assertIsNone(self.lookup())

    def test_missing_null_and_duplicate_rows_never_hit(self):
        folder = Path(self.store.save(fixture()))
        original = json.loads((folder / "result.json").read_text())
        for mutation in ("remove", "null", "duplicate", "wrong_keyword", "bool"):
            data = copy.deepcopy(original)
            if mutation == "remove": data["rows"].pop()
            elif mutation == "null": data["rows"][0]["search_index"] = None
            elif mutation == "duplicate": data["rows"][-1] = data["rows"][0]
            elif mutation == "wrong_keyword": data["rows"][0]["keyword"] = "其他"
            else: data["rows"][0]["search_index"] = True
            (folder / "result.json").write_text(json.dumps(data))
            self.assertIsNone(self.lookup())

    def test_incomplete_batch_is_saved_but_not_cached(self):
        data = fixture()
        data["metadata"]["complete"] = False
        data["rows"][0]["search_index"] = None
        folder = Path(self.store.save(data))
        self.assertTrue(folder.is_dir())
        self.assertIsNone(self.lookup())

    def test_csv_formula_escaped_without_changing_json_keyword(self):
        keyword = '=HYPERLINK("https://invalid.test")'
        folder = Path(self.store.save(fixture([keyword])))
        rows = list(csv.DictReader(io.StringIO((folder / "search-index.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual(rows[0]["keyword"], "'" + keyword)
        data = json.loads((folder / "result.json").read_text())
        self.assertEqual(data["rows"][0]["keyword"], keyword)

    def test_corrupt_json_and_symlinks_are_ignored(self):
        folder = Path(self.store.save(fixture()))
        (folder / "result.json").write_text('{"metadata": {}, "metadata": {}}')
        self.assertIsNone(self.lookup())
        external = Path(self.temp.name) / "outside"
        external.mkdir()
        (external / "result.json").write_text(json.dumps(fixture()))
        (self.root / "linked").symlink_to(external, target_is_directory=True)
        self.assertIsNone(self.lookup())

    def test_comparison_matches_batch_and_updates_metadata(self):
        folder = Path(self.store.save(fixture()))
        data = json.loads((folder / "result.json").read_text())
        report = {"verified": True, "rows": [{"keyword": row["keyword"], "date": row["date"],
                    "api_search_index": row["search_index"], "page_display": str(row["search_index"]),
                    "status": "match"} for row in data["rows"]]}
        self.store.save_comparison(folder, report)
        self.assertEqual(json.loads((folder / "result.json").read_text())["metadata"]["page_comparison"], "verified")
        self.assertTrue(json.loads((folder / "comparison.json").read_text())["verified"])
        wrong = copy.deepcopy(report)
        wrong["rows"][0]["keyword"] = "错误"
        with self.assertRaises(ValueError): self.store.save_comparison(folder, wrong)
        self.store.save_comparison(folder, {"verified": False, "rows": []})
        self.assertEqual(json.loads((folder / "result.json").read_text())["metadata"]["page_comparison"], "incomplete")

    def test_comparison_cannot_escape_root_or_follow_symlinks(self):
        folder = Path(self.store.save(fixture()))
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "link"
        link.symlink_to(folder, target_is_directory=True)
        for target in (outside, self.root, link):
            with self.assertRaises(ValueError): self.store.save_comparison(target, {"verified": False})
        result_file = folder / "comparison.json"
        result_file.symlink_to(outside / "victim")
        with self.assertRaises(ValueError): self.store.save_comparison(folder, {"verified": False})
        self.assertFalse((outside / "victim").exists())

    def test_atomic_failure_preserves_existing_file(self):
        path = Path(self.temp.name) / "artifact.json"
        path.write_bytes(b"original")
        with patch("storage.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError): _atomic_write(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(Path(self.temp.name).glob(".writing-*")), [])

    def test_reject_inconsistent_complete_claim_and_scope(self):
        data = fixture()
        data["rows"].pop()
        with self.assertRaises(ValueError): self.store.save(data)
        data = fixture()
        data["metadata"]["region"] = "上海"
        with self.assertRaises(ValueError): self.store.save(data)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
