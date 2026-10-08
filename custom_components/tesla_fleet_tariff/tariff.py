"""Pure helpers to build, validate and modify Tesla ``tariff_content_v2`` payloads.

No Home Assistant imports so the logic can be unit tested in isolation.

Conventions (Fleet API docs, Tesla's example tariff and the offline resolver
in ``tesla_fleet_api.tariff``):
* Times are local site time.
* ``fromDayOfWeek`` / ``toDayOfWeek`` are 0..6 with **Monday = 0**.
* A period whose end is <= its start runs into the *next* day, so
  ``23:30 -> 00:00`` on Monday means Monday 23:30 to Tuesday 00:00.
* Every season must cover every minute of the week exactly once, and every
  label with periods needs a price (and vice versa).
* Sell price must be <= buy price at all times; negative prices are clamped.

Internally each season is expanded to a 7 x 1440 minute grid of labels,
edited, and re-emitted as compact periods.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

MINUTES_PER_DAY = 1440
DAYS = 7
# The Tesla app only has a Weekday (Mon-Fri) and a Weekend (Sat-Sun) schedule.
# Periods must use one of these day ranges or the app shows rows from several
# days on top of each other and flags the plan as invalid.
WEEKDAYS = tuple(range(0, 5))
WEEKEND = (5, 6)
APP_DAY_RANGES = {(0, 6), (0, 4), (5, 6)}


SCHEDULE_SINGLE = "single"
SCHEDULE_WEEKDAY_WEEKEND = "weekday_weekend"


def day_group(day: int, schedule: str = SCHEDULE_SINGLE) -> tuple[int, ...]:
    """All days that share a schedule with ``day`` in the Tesla app.

    ``single``: one all-week schedule (the app shows one schedule).
    ``weekday_weekend``: Mon-Fri and Sat-Sun are separate schedules.
    """
    if schedule == SCHEDULE_SINGLE:
        return tuple(range(DAYS))
    return WEEKDAYS if day in WEEKDAYS else WEEKEND
ALL_YEAR = "All Year"
VALID_CURRENCIES = ("GBP", "EUR", "USD")

STANDARD_LABELS = ("SUPER_OFF_PEAK", "OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK")
# Cheapest first. Two-rate tariffs (e.g. Intelligent Go) deliberately leave
# SUPER_OFF_PEAK and ON_PEAK free so events get meaningful labels in the app.
LABELS_BY_COUNT: dict[int, tuple[str, ...]] = {
    1: ("OFF_PEAK",),
    2: ("OFF_PEAK", "PARTIAL_PEAK"),
    3: ("OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK"),
    4: STANDARD_LABELS,
}
EVENT_LABELS_EXPENSIVE = ("ON_PEAK", "PARTIAL_PEAK", "SUPER_OFF_PEAK", "OFF_PEAK")
EVENT_LABELS_CHEAP = ("SUPER_OFF_PEAK", "OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK")
# The API accepts any label string; the Tesla app just won't name custom ones.
CUSTOM_LABEL = "HA_EVENT_{}"

Grid = list[list[str | None]]  # [day][minute] -> label


class TariffError(ValueError):
    """Raised when a tariff cannot be built or modified."""


@dataclass(frozen=True)
class DailyRate:
    """A rate that applies from ``start`` until the next rate's start."""

    start: int  # minute of day
    buy: float
    sell: float
    label: str | None = None  # e.g. "PARTIAL_PEAK"; auto-assigned if omitted


# Friendly names as shown in the Tesla app -> API labels.
LABEL_ALIASES = {
    "super off-peak": "SUPER_OFF_PEAK",
    "super off peak": "SUPER_OFF_PEAK",
    "off-peak": "OFF_PEAK",
    "off peak": "OFF_PEAK",
    "mid-peak": "PARTIAL_PEAK",
    "mid peak": "PARTIAL_PEAK",
    "partial peak": "PARTIAL_PEAK",
    "peak": "ON_PEAK",
    "on-peak": "ON_PEAK",
    "on peak": "ON_PEAK",
}


def normalise_label(value: str) -> str:
    """Accept 'Mid-Peak' / 'mid_peak' / 'PARTIAL_PEAK' etc."""
    key = value.strip().lower().replace("_", " ")
    return LABEL_ALIASES.get(key, LABEL_ALIASES.get(key.replace(" ", "-"), value.strip().upper()))


@dataclass(frozen=True)
class EventWindow:
    """An override on one weekday: ``[start, end)`` minutes, Monday = 0.

    Either ``label`` (reuse an existing label of the base tariff, and so its
    prices and its colour in the Tesla app) or ``buy``/``sell`` (new prices,
    which need a free label).
    """

    day: int
    start: int
    end: int
    buy: float | None = None
    sell: float | None = None
    label: str | None = None
    # Relabel the rest of the event's day first, e.g. (("ON_PEAK", "PARTIAL_PEAK"),)
    # so the event window is the only Peak that day.
    relabel: tuple[tuple[str, str], ...] = ()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def parse_time(value: str | time) -> int:
    """Return minutes since midnight for ``HH:MM[:SS]`` or a ``time``."""
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    try:
        parts = [int(p) for p in str(value).split(":")]
        hours, minutes = parts[0], parts[1] if len(parts) > 1 else 0
    except (ValueError, IndexError) as err:
        raise TariffError(f"Invalid time '{value}', expected HH:MM") from err
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise TariffError(f"Invalid time '{value}', expected HH:MM")
    return hours * 60 + minutes


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _make_period(day_from: int, day_to: int, start: int, end: int) -> dict[str, int]:
    return {
        "fromDayOfWeek": day_from,
        "toDayOfWeek": day_to,
        "fromHour": start // 60,
        "fromMinute": start % 60,
        "toHour": (end // 60) % 24,
        "toMinute": end % 60,
    }


def _expand_days(day_from: int, day_to: int) -> list[int]:
    if day_from <= day_to:
        return list(range(day_from, day_to + 1))
    return list(range(day_from, DAYS)) + list(range(0, day_to + 1))


def _period_minutes(period: dict[str, Any]) -> list[tuple[int, int]]:
    """(day, minute) cells covered by a period, following Tesla's semantics."""
    days = _expand_days(
        int(period.get("fromDayOfWeek", 0)), int(period.get("toDayOfWeek", 6))
    )
    start = int(period.get("fromHour", 0)) * 60 + int(period.get("fromMinute", 0))
    end = int(period.get("toHour", 0)) * 60 + int(period.get("toMinute", 0))
    if end <= start:
        end += MINUTES_PER_DAY
    cells: list[tuple[int, int]] = []
    for day in days:
        for m in range(start, end):
            cells.append(((day + m // MINUTES_PER_DAY) % DAYS, m % MINUTES_PER_DAY))
    return cells


def season_grid(season: dict[str, Any]) -> tuple[Grid, list[str]]:
    """Expand a season into a label grid, reporting overlaps."""
    grid: Grid = [[None] * MINUTES_PER_DAY for _ in range(DAYS)]
    errors: list[str] = []
    for label, spec in (season.get("tou_periods") or {}).items():
        for period in (spec or {}).get("periods", []):
            for day, minute in _period_minutes(period):
                if grid[day][minute] is not None and grid[day][minute] != label:
                    if len(errors) < 3:
                        errors.append(
                            f"overlap on day {day} at {_hhmm(minute)} "
                            f"({grid[day][minute]} / {label})"
                        )
                    continue
                grid[day][minute] = label
    return grid, errors


def season_breaks(season: dict[str, Any]) -> set[int]:
    """Minute-of-day boundaries of a season's existing periods (block edges)."""
    breaks: set[int] = set()
    for spec in (season.get("tou_periods") or {}).values():
        for period in (spec or {}).get("periods", []):
            for h, m in (("fromHour", "fromMinute"), ("toHour", "toMinute")):
                breaks.add((int(period.get(h, 0)) * 60 + int(period.get(m, 0))) % MINUTES_PER_DAY)
    breaks.discard(0)
    return breaks


def grid_to_tou(
    grid: Grid, breaks: frozenset[int] | set[int] = frozenset()
) -> dict[str, dict[str, list[dict[str, int]]]]:
    """Re-emit a complete grid as Tesla periods.

    Each day is run-length encoded, but runs are also split at every minute in
    ``breaks`` so separate blocks of the same label (e.g. Mid-Peak 18:00-19:00
    and 19:00-20:30) stay separate rows in the Tesla app, ready to be
    overridden by hand. Identical (start, end, label) runs on consecutive days
    are merged into one day range.
    """
    runs: dict[tuple[int, int, str], list[int]] = {}
    for day in range(DAYS):
        row = grid[day]
        start = 0
        for minute in range(1, MINUTES_PER_DAY + 1):
            if minute == MINUTES_PER_DAY or row[minute] != row[start] or minute in breaks:
                label = row[start]
                if label is None:
                    raise TariffError(f"gap on day {day} at {_hhmm(start)}")
                runs.setdefault((start, minute, label), []).append(day)
                start = minute
    tou: dict[str, dict[str, list[dict[str, int]]]] = {}
    for (start, end, label), days in sorted(runs.items(), key=lambda kv: (kv[1][0], kv[0])):
        ranges: list[tuple[int, int]] = []
        for day in days:
            if ranges and ranges[-1][1] == day - 1:
                ranges[-1] = (ranges[-1][0], day)
            else:
                ranges.append((day, day))
        periods = tou.setdefault(label, {"periods": []})["periods"]
        periods.extend(_make_period(a, b, start, end) for a, b in ranges)
    for spec in tou.values():
        _join_overnight(spec["periods"])
    return tou


def _join_overnight(periods: list[dict[str, int]]) -> None:
    """Merge 'X-00:00' + '00:00-Y' of the same label into one 'X-Y' row.

    Matches how the Tesla app shows an overnight block (e.g. Super Off-Peak
    23:30-05:30). Only done for all-week rows: a row whose end is before its
    start runs into the next morning, which is only identical to the split
    form when every day has the same night.
    """
    def minutes(p, h, m):
        return p[h] * 60 + p[m]

    evening = next(
        (p for p in periods
         if (p["fromDayOfWeek"], p["toDayOfWeek"]) == (0, 6)
         and minutes(p, "toHour", "toMinute") == 0
         and minutes(p, "fromHour", "fromMinute") > 0),
        None,
    )
    morning = next(
        (p for p in periods
         if (p["fromDayOfWeek"], p["toDayOfWeek"]) == (0, 6)
         and minutes(p, "fromHour", "fromMinute") == 0
         and minutes(p, "toHour", "toMinute") > 0),
        None,
    )
    if evening is None or morning is None or evening is morning:
        return
    evening["toHour"], evening["toMinute"] = morning["toHour"], morning["toMinute"]
    periods.remove(morning)


def _rates(part: dict[str, Any], season: str) -> dict[str, float]:
    charges = part.setdefault("energy_charges", {})
    return charges.setdefault(season, {}).setdefault("rates", {})


def _all_rate(part: dict[str, Any]) -> float:
    return float(
        part.get("energy_charges", {}).get("ALL", {}).get("rates", {}).get("ALL", 0) or 0
    )


def _has_tou(part: dict[str, Any] | None) -> bool:
    if not part:
        return False
    return any((s or {}).get("tou_periods") for s in (part.get("seasons") or {}).values())


def _labels_in_use(tariff: dict[str, Any]) -> set[str]:
    used: set[str] = set()
    for part in (tariff, tariff.get("sell_tariff") or {}):
        for season in (part.get("seasons") or {}).values():
            used.update(((season or {}).get("tou_periods") or {}).keys())
    return used


def _skeleton(*, name, code, utility, currency, daily_charge, seasons, rates):
    season_names = list(seasons)
    return {
        "version": 1,
        "monthly_minimum_bill": 0,
        "min_applicable_demand": 0,
        "max_applicable_demand": 0,
        "monthly_charges": 0,
        "utility": utility,
        "code": code,
        "name": name,
        "currency": currency,
        "daily_charges": [{"name": "Standing charge", "amount": daily_charge}],
        "daily_demand_charges": {},
        "demand_charges": {
            "ALL": {"rates": {"ALL": 0}},
            **{s: {"rates": {}} for s in season_names},
        },
        "energy_charges": {
            "ALL": {"rates": {"ALL": 0}},
            **{s: {"rates": dict(rates)} for s in season_names},
        },
        "seasons": copy.deepcopy(seasons),
    }


# --------------------------------------------------------------------------- #
# Building a simple daily tariff (e.g. Octopus Intelligent Go)
# --------------------------------------------------------------------------- #
def build_daily_tariff(
    rates: list[DailyRate],
    *,
    currency: str = "GBP",
    name: str = "Home Assistant tariff",
    code: str = "HA-DAILY",
    utility: str = "Home Assistant",
    daily_charge: float = 0.0,
) -> dict[str, Any]:
    """Build an all-year tariff where every day of the week is identical.

    Rates may carry their own label (e.g. "Mid-Peak" -> PARTIAL_PEAK) to match
    the Tesla app; otherwise distinct (buy, sell) pairs are labelled
    cheapest first (max 4).
    Export is capped at import for each slot: Tesla requires buy >= sell and
    would otherwise *raise the import price* (e.g. 15p fixed export vs 7p
    overnight import), wrecking overnight charging.
    """
    if not rates:
        raise TariffError("At least one rate is required")
    if currency not in VALID_CURRENCIES:
        raise TariffError(f"Currency must be one of {', '.join(VALID_CURRENCIES)}")
    ordered = sorted(
        (
            DailyRate(r.start, r.buy, min(r.sell, r.buy),
                      normalise_label(r.label) if r.label else None)
            for r in rates
        ),
        key=lambda r: r.start,
    )
    if len({r.start for r in ordered}) != len(ordered):
        raise TariffError("Two rates share the same start time")
    if any(r.buy < 0 or r.sell < 0 for r in ordered):
        raise TariffError("Prices must not be negative (Tesla clamps them to 0)")

    labelled = [r for r in ordered if r.label]
    if labelled and len(labelled) != len(ordered):
        raise TariffError("Give a label for every rate, or for none")
    if labelled:
        prices: dict[str, tuple[float, float]] = {}
        for r in ordered:
            if prices.setdefault(r.label, (r.buy, r.sell)) != (r.buy, r.sell):
                raise TariffError(
                    f"Label '{r.label}' is used with two different prices"
                )
        if len(prices) > 4:
            raise TariffError("Tesla supports at most 4 labels")
        label_of = {r.start: r.label for r in ordered}
        pair_by_label = prices
    else:
        pairs = sorted({(r.buy, r.sell) for r in ordered})
        if len(pairs) > 4:
            raise TariffError(
                f"{len(pairs)} distinct price pairs given; Tesla supports at most 4"
            )
        auto = dict(zip(pairs, LABELS_BY_COUNT[len(pairs)], strict=True))
        label_of = {r.start: auto[(r.buy, r.sell)] for r in ordered}
        pair_by_label = {lbl: p for p, lbl in auto.items()}

    row: list[str | None] = [None] * MINUTES_PER_DAY
    for idx, rate in enumerate(ordered):
        end = ordered[(idx + 1) % len(ordered)].start
        length = (end - rate.start) % MINUTES_PER_DAY or MINUTES_PER_DAY
        for m in range(rate.start, rate.start + length):
            row[m % MINUTES_PER_DAY] = label_of[rate.start]
    tou = grid_to_tou([list(row) for _ in range(DAYS)], {r.start for r in ordered})

    buy_rates = {lbl: p[0] for lbl, p in pair_by_label.items()}
    sell_rates = {lbl: p[1] for lbl, p in pair_by_label.items()}
    seasons = {
        ALL_YEAR: {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31, "tou_periods": tou}
    }
    common = {
        "name": name,
        "code": code,
        "utility": utility,
        "currency": currency,
        "daily_charge": daily_charge,
        "seasons": seasons,
    }
    tariff = _skeleton(rates=buy_rates, **common)
    tariff["sell_tariff"] = _skeleton(rates=sell_rates, **common)
    tariff["sell_tariff"]["daily_charges"] = [{"name": "Charge", "amount": 0}]
    return tariff


# --------------------------------------------------------------------------- #
# Overlaying events (saving sessions / free electricity / anything)
# --------------------------------------------------------------------------- #
def _synthesise_flat_seasons(part: dict[str, Any], label: str = "OFF_PEAK") -> None:
    """Give seasons with no TOU periods a full-week period at the flat rate."""
    flat = _all_rate(part)
    for name, season in (part.get("seasons") or {}).items():
        if season.get("tou_periods"):
            continue
        season["tou_periods"] = {label: {"periods": [_make_period(0, 6, 0, 0)]}}
        _rates(part, name).setdefault(label, flat)
    if flat and part.get("energy_charges", {}).get("ALL"):
        part["energy_charges"]["ALL"] = {"rates": {"ALL": 0}}


def _ensure_sell_seasons(tariff: dict[str, Any]) -> None:
    """Make sure the export side has its own seasons/TOU periods.

    A flat export rate is mirrored onto the import structure (capped at the
    import price per slot) so events can raise export for their window.
    """
    sell = tariff.get("sell_tariff") or {}
    if _has_tou(sell):
        _synthesise_flat_seasons(sell)
        return
    flat = _all_rate(sell)
    new_sell = copy.deepcopy(sell) if sell else {}
    new_sell["seasons"] = copy.deepcopy(tariff["seasons"])
    new_sell["energy_charges"] = {"ALL": {"rates": {"ALL": 0}}}
    for name, season in new_sell["seasons"].items():
        buy_rates = tariff.get("energy_charges", {}).get(name, {}).get("rates", {})
        for label in season.get("tou_periods", {}):
            _rates(new_sell, name)[label] = min(flat, float(buy_rates.get(label, flat)))
    new_sell.setdefault("demand_charges", {"ALL": {"rates": {"ALL": 0}}})
    new_sell.setdefault("daily_charges", [{"name": "Charge", "amount": 0}])
    tariff["sell_tariff"] = new_sell


def _min_buy(tariff: dict[str, Any]) -> float:
    prices = [
        float(v)
        for name, charges in tariff.get("energy_charges", {}).items()
        if name != "ALL"
        for v in charges.get("rates", {}).values()
    ]
    return min(prices) if prices else _all_rate(tariff)


def assign_event_labels(
    tariff: dict[str, Any], pairs: list[tuple[float, float]]
) -> dict[tuple[float, float], str]:
    """Give each distinct (buy, sell) event price its own unused label."""
    free = [label for label in STANDARD_LABELS if label not in _labels_in_use(tariff)]
    cheapest_base = _min_buy(tariff)
    result: dict[tuple[float, float], str] = {}
    custom = 0
    # Most expensive first so the headline saving session gets ON_PEAK.
    for pair in sorted(set(pairs), key=lambda p: -p[0]):
        prefs = EVENT_LABELS_CHEAP if pair[0] < cheapest_base else EVENT_LABELS_EXPENSIVE
        label = next((lbl for lbl in prefs if lbl in free), None)
        if label is None:
            custom += 1
            label = CUSTOM_LABEL.format(custom)
        else:
            free.remove(label)
        result[pair] = label
    return result


def apply_events(
    base: dict[str, Any],
    windows: list[EventWindow],
    schedule: str = SCHEDULE_SINGLE,
) -> tuple[dict[str, Any], dict[tuple[float, float], str]]:
    """Return a copy of ``base`` with each window overridden on its weekday.

    Later windows win where they overlap. Windows are applied to every season;
    the caller keeps only events within the next 7 days and restores the base
    once they have ended, so the weekly repetition never matters.
    """
    if not base.get("seasons"):
        raise TariffError("Base tariff has no seasons")
    existing = _labels_in_use(base)
    for w in windows:
        if not (0 <= w.day < DAYS and 0 <= w.start < w.end <= MINUTES_PER_DAY):
            raise TariffError(f"Invalid event window {w}")
        if w.label is not None:
            if w.label not in existing:
                raise TariffError(
                    f"Label '{w.label}' is not used by the base tariff "
                    f"(available: {', '.join(sorted(existing))})"
                )
        else:
            if w.buy is None or w.sell is None:
                raise TariffError("An event needs either a label or buy/sell prices")
        for frm, to in w.relabel:
            for lbl in (frm, to):
                if lbl not in existing:
                    raise TariffError(
                        f"Relabel label '{lbl}' is not used by the base tariff "
                        f"(available: {', '.join(sorted(existing))})"
                    )
        if w.label is not None:
            continue
        if w.buy is None or w.sell is None:
            raise TariffError("An event needs either a label or buy/sell prices")
        if w.buy < 0 or w.sell < 0:
            raise TariffError("Prices must not be negative (Tesla clamps them to 0)")
        if w.sell > w.buy:
            raise TariffError(
                "Export price is above import price; Tesla requires buy >= sell "
                "and would silently raise the import price to match"
            )

    tariff = copy.deepcopy(base)
    if not windows:
        # Re-emit periods in the current tidy form (e.g. one 23:30-05:30 row
        # for a base stored by an older version), without changing prices.
        for part in (tariff, tariff.get("sell_tariff") or {}):
            for season in (part.get("seasons") or {}).values():
                if not season.get("tou_periods"):
                    continue
                grid, errors = season_grid(season)
                if errors or any(None in row for row in grid):
                    continue  # leave anything odd exactly as given
                season["tou_periods"] = grid_to_tou(grid, season_breaks(season))
        return tariff, {}
    _synthesise_flat_seasons(tariff)
    _ensure_sell_seasons(tariff)
    labels = assign_event_labels(
        tariff, [(w.buy, w.sell) for w in windows if w.label is None]
    )

    for part, side in ((tariff, 0), (tariff["sell_tariff"], 1)):
        for season_name, season in part["seasons"].items():
            grid, errors = season_grid(season)
            if errors:
                raise TariffError(f"Base tariff is invalid: {errors[0]}")
            # 1) day-wide relabels (normal part of the event's day), then
            # 2) the event windows themselves on top.
            # Each window is applied to every day in its app group (Mon-Fri or
            # Sat-Sun) - the app can't show a single-day change. Only the
            # event's own day ever runs with it: the plan is pushed on that
            # day and the normal plan is restored when the event ends.
            for w in windows:
                for day in day_group(w.day, schedule):
                    row = grid[day]
                    for frm, to in w.relabel:
                        for m in range(MINUTES_PER_DAY):
                            if row[m] == frm:
                                row[m] = to
            for w in windows:
                label = w.label or labels[(w.buy, w.sell)]
                for day in day_group(w.day, schedule):
                    for m in range(w.start, w.end):
                        grid[day][m] = label
            season["tou_periods"] = grid_to_tou(grid, season_breaks(season))
            rates = _rates(part, season_name)
            present = set(season["tou_periods"])
            for label in list(rates):
                if label != "ALL" and label not in present:
                    del rates[label]
            for pair, label in labels.items():
                if label in present:
                    rates[label] = pair[side]
    return tariff, labels


# --------------------------------------------------------------------------- #
# Validation (mirrors Tesla's documented checks so we fail before the API)
# --------------------------------------------------------------------------- #
def _season_dates(season: dict[str, Any]) -> set[date]:
    year = 2024  # leap year
    start = date(year, int(season.get("fromMonth", 1)), int(season.get("fromDay", 1)))
    to_month = int(season.get("toMonth", 12))
    to_day = int(season.get("toDay", 0)) or 31
    while True:
        try:
            end = date(year, to_month, to_day)
            break
        except ValueError:
            to_day -= 1
    days: set[date] = set()
    cur = start
    while True:
        days.add(cur)
        if cur == end:
            return days
        cur += timedelta(days=1)
        if cur.year != year:
            cur = date(year, 1, 1)


def _validate_part(part: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    seasons = part.get("seasons") or {}
    if not seasons:
        return [f"{prefix}: at least one season is required"]
    covered: dict[date, str] = {}
    for name, season in seasons.items():
        for day in _season_dates(season):
            if day in covered:
                errors.append(f"{prefix}: seasons '{covered[day]}' and '{name}' overlap")
                break
            covered[day] = name
    if len(covered) != 366:
        errors.append(f"{prefix}: seasons leave gaps in the year")
    for name, season in seasons.items():
        grid, overlaps = season_grid(season)
        errors.extend(f"{prefix}/{name}: {e}" for e in overlaps)
        for day, row in enumerate(grid):
            if None in row:
                errors.append(f"{prefix}/{name}: gap on day {day} at {_hhmm(row.index(None))}")
                break
        labels = set((season.get("tou_periods") or {}).keys())
        rates = part.get("energy_charges", {}).get(name, {}).get("rates", {})
        priced = {k for k in rates if k != "ALL"}
        errors.extend(f"{prefix}/{name}: label '{m}' has no price" for m in sorted(labels - priced))
        errors.extend(f"{prefix}/{name}: price '{x}' has no periods" for x in sorted(priced - labels))
        errors.extend(
            f"{prefix}/{name}: negative price for '{k}'" for k, v in rates.items() if float(v) < 0
        )
    return errors


def _app_day_range_errors(part: dict[str, Any], prefix: str) -> list[str]:
    """Periods must be all week, Mon-Fri or Sat-Sun for the Tesla app."""
    errors = []
    for name, season in (part.get("seasons") or {}).items():
        for label, spec in (season.get("tou_periods") or {}).items():
            for period in (spec or {}).get("periods", []):
                rng = (int(period.get("fromDayOfWeek", 0)), int(period.get("toDayOfWeek", 6)))
                if rng not in APP_DAY_RANGES:
                    errors.append(
                        f"{prefix}/{name}: {label} uses days {rng[0]}-{rng[1]}; the "
                        "Tesla app only supports all week, Mon-Fri or Sat-Sun"
                    )
                    return errors
    return errors


def validate_tariff(tariff: dict[str, Any]) -> list[str]:
    """Return a list of human readable problems (empty list means valid)."""
    errors = _validate_part(tariff, "buy")
    errors.extend(_app_day_range_errors(tariff, "buy"))
    sell = tariff.get("sell_tariff")
    if not _has_tou(sell):
        return errors
    errors.extend(_validate_part(sell, "sell"))
    errors.extend(_app_day_range_errors(sell, "sell"))
    for name, season in tariff["seasons"].items():
        sell_season = sell["seasons"].get(name)
        if not sell_season:
            continue
        buy_grid, _ = season_grid(season)
        sell_grid, _ = season_grid(sell_season)
        buy_rates = tariff.get("energy_charges", {}).get(name, {}).get("rates", {})
        sell_rates = sell.get("energy_charges", {}).get(name, {}).get("rates", {})
        for day in range(DAYS):
            bad = next(
                (
                    m
                    for m in range(MINUTES_PER_DAY)
                    if buy_grid[day][m] is not None
                    and sell_grid[day][m] is not None
                    and float(sell_rates.get(sell_grid[day][m], 0))
                    > float(buy_rates.get(buy_grid[day][m], 0))
                ),
                None,
            )
            if bad is not None:
                errors.append(f"{name}: sell > buy on day {day} at {_hhmm(bad)}")
                break
    return errors


def price_at(part: dict[str, Any], day: int, minute: int) -> float:
    """Price in effect at (weekday, minute) in the first season (for tests/status)."""
    name, season = next(iter(part["seasons"].items()))
    grid, _ = season_grid(season)
    return float(part["energy_charges"][name]["rates"][grid[day][minute]])
