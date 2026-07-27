"""Tests for the record scraper. Run: python -m unittest discover -s tests"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import (  # noqa: E402
    CsvAdapter,
    FixedWidthAdapter,
    HtmlTableAdapter,
    RateLimiter,
    Record,
    SourceAdapter,
    collect,
    merge_with_archive,
    with_retry,
)


class BrokenAdapter(SourceAdapter):
    name = "broken_county"

    def fetch(self) -> str:
        raise ConnectionError("county site is down")

    def parse(self, raw: str) -> list[Record]:
        return []


class TestHtmlAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.records = HtmlTableAdapter().parse(HtmlTableAdapter().fetch())

    def test_parses_well_formed_rows(self) -> None:
        self.assertEqual(len(self.records), 3, "the malformed 2-cell row should be skipped")

    def test_strips_inline_markup(self) -> None:
        record = next(r for r in self.records if r.parcel_id == "SMP-000302")
        self.assertEqual(record.situs_address, "77 Testcase Dr, Springfield TX")

    def test_maps_columns_to_schema(self) -> None:
        record = self.records[0]
        self.assertEqual(record.parcel_id, "SMP-000301")
        self.assertIn("code violation", record.notes)
        self.assertEqual(record.source, "sample_county_html")


class TestCsvAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.records = CsvAdapter().parse(CsvAdapter().fetch())

    def test_skips_rows_without_a_parcel_id(self) -> None:
        self.assertEqual(len(self.records), 4)
        self.assertTrue(all(r.parcel_id for r in self.records))

    def test_renames_source_columns(self) -> None:
        record = next(r for r in self.records if r.parcel_id == "SMP-000102")
        self.assertEqual(record.mailing_address, "9 Faraway Rd, Otherville TX")
        self.assertIn("Tax delinquent", record.notes)

    def test_ignores_columns_outside_the_schema(self) -> None:
        self.assertFalse(any("780000" in r.notes for r in self.records))


class TestFixedWidthAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.records = FixedWidthAdapter().parse(FixedWidthAdapter().fetch())

    def test_skips_comments_and_blank_lines(self) -> None:
        self.assertEqual(len(self.records), 4)

    def test_slices_columns_at_the_right_offsets(self) -> None:
        record = self.records[1]
        self.assertEqual(record.parcel_id, "SMP-000202")
        self.assertEqual(record.situs_address, "212 Placeholder Ave, Springfield TX")
        self.assertEqual(record.mailing_address, "9 Faraway Rd, Otherville TX")
        self.assertEqual(record.legal_description, "LOT 11 BLK 7 SAMPLE HEIGHTS")
        self.assertEqual(record.notes, "Mailing address differs from situs.")


class TestRetry(unittest.TestCase):
    def test_succeeds_after_transient_failures(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("boom")
            return "ok"

        self.assertEqual(with_retry(flaky, sleep=lambda _: None), "ok")
        self.assertEqual(calls["n"], 3)

    def test_raises_after_exhausting_attempts(self) -> None:
        with self.assertRaises(RuntimeError):
            with_retry(lambda: (_ for _ in ()).throw(TimeoutError("boom")), sleep=lambda _: None)


class TestRateLimiter(unittest.TestCase):
    def test_waits_only_when_calls_are_too_close(self) -> None:
        slept: list[float] = []
        now = {"t": 0.0}
        limiter = RateLimiter(1.0, sleep=slept.append, clock=lambda: now["t"])

        limiter.wait()          # first call never waits
        now["t"] = 0.25
        limiter.wait()          # 0.75s too early
        now["t"] = 10.0
        limiter.wait()          # plenty of time has passed

        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.75, places=6)


class TestFailureIsolation(unittest.TestCase):
    def test_one_dead_source_does_not_end_the_run(self) -> None:
        records, failed = collect(
            [CsvAdapter(), BrokenAdapter(), FixedWidthAdapter()], sleep=lambda _: None
        )
        self.assertEqual(failed, ["broken_county"])
        self.assertTrue(records, "surviving sources should still return records")
        self.assertTrue(any(r.source == "sample_county_txt" for r in records),
                        "sources after the failing one must still run")


class TestArchive(unittest.TestCase):
    def test_missing_records_are_archived_not_deleted(self) -> None:
        existing = {
            "A-1": {"parcel_id": "A-1", "notes": "still here"},
            "A-2": {"parcel_id": "A-2", "notes": "vanished upstream"},
        }
        incoming = [Record(parcel_id="A-1", notes="still here")]

        live, archived = merge_with_archive(existing, incoming, today="2026-07-25")

        self.assertIn("A-1", live)
        self.assertNotIn("A-2", live)
        self.assertEqual(archived["A-2"]["archived_on"], "2026-07-25")
        self.assertEqual(archived["A-2"]["notes"], "vanished upstream")

    def test_empty_upstream_response_archives_everything_and_loses_nothing(self) -> None:
        existing = {"A-1": {"parcel_id": "A-1"}, "A-2": {"parcel_id": "A-2"}}
        live, archived = merge_with_archive(existing, [], today="2026-07-25")
        self.assertEqual(live, {})
        self.assertEqual(set(archived), {"A-1", "A-2"})

    def test_new_records_are_stamped_with_last_seen(self) -> None:
        live, _ = merge_with_archive({}, [Record(parcel_id="B-1")], today="2026-07-25")
        self.assertEqual(live["B-1"]["last_seen"], "2026-07-25")


if __name__ == "__main__":
    unittest.main()
