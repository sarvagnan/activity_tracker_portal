"""Exact arithmetic for schema-validated projection/v1 nested instants.

This module uses the proleptic Gregorian calendar, including year 0000. It has
no imports or lazy initialization and never consults a clock or timezone data.
The caller supplies validated UTC instant text and applied numeric offsets.
"""

_NS_PER_SECOND = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_SECOND
# Days before each month in a non-leap year.
_MONTH_STARTS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
_EPOCH_ORDINAL = 719_163  # 1970-01-01, with 0001-01-01 numbered 1.
_MAX_ORDINAL = 3_652_059  # 9999-12-31.


def parse_instant_ns(text: str) -> int:
    """Parse a validated projection UTC instant without losing any fraction."""
    year = int(text[:4])
    month = int(text[5:7])
    day = int(text[8:10])
    previous_year = year - 1
    # Floor division extends the leap-year count correctly through year 0000.
    ordinal = (
        365 * previous_year + previous_year // 4 - previous_year // 100
        + previous_year // 400 + _MONTH_STARTS[month - 1] + day
    )
    if month > 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        ordinal += 1
    seconds = int(text[11:13]) * 3600 + int(text[14:16]) * 60 + int(text[17:19])
    fraction_ns = int(text[20:-1].ljust(9, "0")) if text[19] == "." else 0
    return (ordinal - _EPOCH_ORDINAL) * _NS_PER_DAY + seconds * _NS_PER_SECOND + fraction_ns


def offset_seconds(text: str) -> int:
    """Parse an applied ±hh:mm[:ss] offset, with its sign on all components."""
    seconds = int(text[1:3]) * 3600 + int(text[4:6]) * 60
    if len(text) == 9:
        seconds += int(text[7:9])
    return -seconds if text[0] == "-" else seconds


def local_date(instant_ns: int, offset: int) -> str | None:
    """Return the shifted YYYY-MM-DD, or None outside years 0001–9999."""
    days = (instant_ns + offset * _NS_PER_SECOND) // _NS_PER_DAY
    ordinal = days + _EPOCH_ORDINAL
    if not 1 <= ordinal <= _MAX_ORDINAL:
        return None

    # March-based Gregorian cycles: leap days end the year. This inverse of
    # civil-date day numbering takes constant work, independent of the year.
    march_days = days + 719_468
    era, day_of_era = divmod(march_days, 146_097)
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    year = era * 400 + year_of_era
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    march_month = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * march_month + 2) // 5 + 1
    month = march_month + (3 if march_month < 10 else -9)
    year += month <= 2
    return f"{year:04d}-{month:02d}-{day:02d}"
