# public-records-scraper

Normalizes public property records from sources that agree on nothing, an HTML table, a CSV export with its own column names, and a fixed-width mainframe dump, into one clean schema.

```bash
python scraper.py
python -m unittest discover -s tests
```

Runs entirely against bundled fixtures. No network, no dependencies.

## Why this exists

This is a sanitized version of a system I run in production across a fleet of Texas county record sources. The real code touches actual people's records and isn't public, so this demonstrates the same engineering on invented data.

The interesting problem isn't parsing any one source. It's that every county publishes the same facts differently, several of them break in a given week, and the whole thing has to run unattended on a schedule without a human noticing.

```
$ python scraper.py
INFO    sample_county_html: parsed 3 records
INFO    sample_county_csv: parsed 4 records
INFO    sample_county_txt: parsed 4 records
11 records from 3/3 sources -> records.json
```

## Three decisions worth explaining

**Per-source adapters, not a branching parser.** Each county is a class implementing `fetch()` and `parse()`. Adding a county means adding one class. It doesn't touch, or risk, the ones already working. The alternative, one function with a growing `if county == ...` chain, means every county shares a blast radius.

**Failure isolation.** `collect()` catches per-source exceptions, logs them, records which sources failed, and returns everything that did parse. A county site being down on a Tuesday should cost you that county's records, not the run. Partial data with a known gap beats no data.

**Archive instead of delete, the one that matters most.** When a record disappears upstream, it moves to `archive.json` with a timestamp. It is never deleted.

This looks like over-engineering until the first time a county site returns an empty result page for an afternoon. A naive "replace the dataset with what we just fetched" pipeline silently erases thousands of real records, and you find out weeks later when you go looking for history that no longer exists. Archiving makes that failure recoverable and, more importantly, visible: `merge_with_archive` returns what it archived, so an abnormally large archive event is something you can alert on.

I learned this one the expensive way.

## Also in here

**Retry with full jitter.** Three attempts with randomized backoff. The jitter isn't decoration. Without it, every worker that hits a rate limit retries at the same moment and trips it again.

**A polite rate limiter.** Minimum interval between requests, with an injectable clock and sleep so the tests verify the timing logic without actually sleeping.

**Defensive parsing.** Malformed HTML rows, CSV rows missing a parcel ID, and comment or blank lines in the fixed-width export are logged and skipped rather than crashing the run or, worse, producing a silently mangled record.

## What this demonstrates

Adapter pattern, schema normalization across heterogeneous sources, retry with jitter, rate limiting, failure isolation in batch jobs, non-destructive update strategy, dependency injection for testable time, and defensive parsing.

## Layout

```
scraper.py                 # adapters, retry, rate limiter, archive merge
fixtures/
  sample_county.html       # HTML table, including one malformed row
  sample_county.csv        # different column names, one row missing an ID
  sample_county.txt        # fixed-width, with comment and blank lines
tests/test_scraper.py      # 16 tests
```

All data is invented. Any resemblance to a real parcel, person, or address is coincidental.

## License

MIT
