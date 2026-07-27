"""Normalize public property records from sources that agree on nothing.

Every county publishes the same facts in a different shape: one has an HTML table,
one exports CSV with its own column names, one still emits fixed-width text from a
mainframe. The job is to turn all of them into one schema without a change to one
county's website breaking the other ninety-nine.

Three decisions worth reading the code for:

- Per-source adapters. Adding a county means adding one class, not editing a
  growing if/elif chain that every other county also runs through.
- Failure isolation. A source that throws is logged and skipped; the run still
  produces the records that did parse. Partial data beats no data.
- Archive instead of delete. If a source returns fewer records than last time, the
  missing ones are archived with a timestamp, never dropped. A county site that is
  briefly empty must not erase real history.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

FIXTURES = Path(__file__).parent / "fixtures"
LOG = logging.getLogger("scraper")

SCHEMA_FIELDS = ("parcel_id", "situs_address", "mailing_address", "legal_description", "notes")


@dataclass(frozen=True)
class Record:
    """One property record, in the shape every downstream tool expects."""

    parcel_id: str
    situs_address: str = ""
    mailing_address: str = ""
    legal_description: str = ""
    notes: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class RateLimiter:
    """Minimum delay between requests. Being impolite to a county server gets you blocked."""

    def __init__(self, min_interval: float, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def with_retry(
    fn: Callable[[], Any],
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> Any:
    """Retry with full jitter.

    Jitter is not decoration: without it every worker that hit a rate limit retries
    at the same instant and trips the limit again.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - transport errors are the expected case here
            last = exc
            if attempt < attempts - 1:
                delay = rand(0, 2 ** attempt)
                LOG.warning("attempt %d/%d failed (%s); retrying in %.2fs",
                            attempt + 1, attempts, exc, delay)
                sleep(delay)
    raise RuntimeError(f"all {attempts} attempts failed: {last}")


class SourceAdapter(ABC):
    """One county, one format. Subclasses only care about parsing."""

    name: str = "unnamed"

    @abstractmethod
    def fetch(self) -> str:
        """Return the raw payload. Real adapters would make an HTTP call here."""

    @abstractmethod
    def parse(self, raw: str) -> list[Record]:
        """Turn the raw payload into records."""

    def load(self, limiter: RateLimiter | None = None,
             sleep: Callable[[float], None] = time.sleep) -> list[Record]:
        if limiter:
            limiter.wait()
        raw = with_retry(self.fetch, sleep=sleep)
        records = self.parse(raw)
        LOG.info("%s: parsed %d records", self.name, len(records))
        return records


class HtmlTableAdapter(SourceAdapter):
    """Parses a simple HTML table with a stdlib regex rather than a dependency.

    Deliberate limitation: this handles the flat, well-formed table this county
    publishes. A county with nested tables or inline markup gets its own adapter
    rather than a more clever regex here - clever regex on HTML is how these break.
    """

    name = "sample_county_html"
    _ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
    _CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
    _TAG = re.compile(r"<[^>]+>")

    def __init__(self, path: Path = FIXTURES / "sample_county.html") -> None:
        self.path = path

    def fetch(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def _clean(self, cell: str) -> str:
        return self._TAG.sub("", cell).replace("&amp;", "&").strip()

    def parse(self, raw: str) -> list[Record]:
        rows = self._ROW.findall(raw)
        if not rows:
            return []
        header = [self._clean(c).lower() for c in self._CELL.findall(rows[0])]
        out: list[Record] = []
        for row in rows[1:]:
            cells = [self._clean(c) for c in self._CELL.findall(row)]
            if len(cells) != len(header):
                LOG.warning("%s: skipping malformed row (%d cells, expected %d)",
                            self.name, len(cells), len(header))
                continue
            data = dict(zip(header, cells))
            out.append(Record(
                parcel_id=data.get("parcel", ""),
                situs_address=data.get("property address", ""),
                mailing_address=data.get("owner mailing", ""),
                legal_description=data.get("legal", ""),
                notes=data.get("remarks", ""),
                source=self.name,
            ))
        return out


class CsvAdapter(SourceAdapter):
    """Same facts, different column names. The mapping lives in one place."""

    name = "sample_county_csv"
    COLUMNS = {
        "PARCELNO": "parcel_id",
        "SITUS": "situs_address",
        "MAIL_ADDR": "mailing_address",
        "LEGAL_DESC": "legal_description",
        "COMMENT": "notes",
    }

    def __init__(self, path: Path = FIXTURES / "sample_county.csv") -> None:
        self.path = path

    def fetch(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def parse(self, raw: str) -> list[Record]:
        out: list[Record] = []
        for row in csv.DictReader(io.StringIO(raw)):
            mapped = {dest: (row.get(src) or "").strip() for src, dest in self.COLUMNS.items()}
            if not mapped["parcel_id"]:
                LOG.warning("%s: skipping row with no parcel id", self.name)
                continue
            out.append(Record(source=self.name, **mapped))
        return out


class FixedWidthAdapter(SourceAdapter):
    """Column offsets from a mainframe export. Order and width are the whole contract."""

    name = "sample_county_txt"
    LAYOUT: Sequence[tuple[str, int, int]] = (
        ("parcel_id", 0, 12),
        ("situs_address", 12, 52),
        ("mailing_address", 52, 92),
        ("legal_description", 92, 124),
        ("notes", 124, 999),
    )

    def __init__(self, path: Path = FIXTURES / "sample_county.txt") -> None:
        self.path = path

    def fetch(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def parse(self, raw: str) -> list[Record]:
        out: list[Record] = []
        for line in raw.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            fields = {name: line[start:end].strip() for name, start, end in self.LAYOUT}
            if not fields["parcel_id"]:
                continue
            out.append(Record(source=self.name, **fields))
        return out


def collect(adapters: Iterable[SourceAdapter], limiter: RateLimiter | None = None,
            sleep: Callable[[float], None] = time.sleep) -> tuple[list[Record], list[str]]:
    """Load every source. A source that fails is reported, not fatal."""
    records: list[Record] = []
    failed: list[str] = []
    for adapter in adapters:
        try:
            records.extend(adapter.load(limiter=limiter, sleep=sleep))
        except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
            LOG.error("%s: source failed, continuing without it (%s)", adapter.name, exc)
            failed.append(adapter.name)
    return records, failed


def merge_with_archive(
    existing: dict[str, dict[str, Any]],
    incoming: list[Record],
    today: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Upsert incoming records; archive anything that vanished upstream.

    Never delete. A county site that returns an empty page for an afternoon would
    otherwise wipe records that are still perfectly real - which is a data-loss bug
    you usually discover weeks later, when the history you needed is already gone.
    """
    today = today or date.today().isoformat()
    seen = {r.parcel_id for r in incoming}

    live = {pid: row for pid, row in existing.items() if pid in seen}
    archived = {
        pid: {**row, "archived_on": today}
        for pid, row in existing.items()
        if pid not in seen
    }
    for record in incoming:
        live[record.parcel_id] = {**record.as_dict(), "last_seen": today}
    return live, archived


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    adapters: list[SourceAdapter] = [HtmlTableAdapter(), CsvAdapter(), FixedWidthAdapter()]
    limiter = RateLimiter(min_interval=0.25)

    records, failed = collect(adapters, limiter=limiter)

    out_path = Path("records.json")
    archive_path = Path("archive.json")
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    prior_archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {}

    live, newly_archived = merge_with_archive(existing, records)
    out_path.write_text(json.dumps(live, indent=2), encoding="utf-8")
    if newly_archived:
        archive_path.write_text(json.dumps({**prior_archive, **newly_archived}, indent=2), encoding="utf-8")

    print(f"{len(records)} records from {len(adapters) - len(failed)}/{len(adapters)} sources -> {out_path}")
    if newly_archived:
        print(f"  {len(newly_archived)} record(s) no longer upstream -> {archive_path}")
    if failed:
        print(f"  sources that failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
