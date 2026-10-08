"""Unit tests for the pure tariff logic.

Every payload is cross-checked with Tesla's own offline resolver
(``tesla_fleet_api.tariff.get_tariff_periods``) so day-of-week and
midnight semantics match what the Tesla library believes.
"""

import importlib.util
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_path = pathlib.Path(__file__).parents[1] / "custom_components/tesla_fleet_tariff/tariff.py"
_spec = importlib.util.spec_from_file_location("tft_tariff", _path)
tariff = importlib.util.module_from_spec(_spec)
sys.modules["tft_tariff"] = tariff
_spec.loader.exec_module(tariff)

resolver = pytest.importorskip("tesla_fleet_api.tariff")

DailyRate, EventWindow, TariffError = tariff.DailyRate, tariff.EventWindow, tariff.TariffError

# Most tests below check the weekday/weekend mode; the default (one all-week
# schedule) has its own tests at the end.
import functools  # noqa: E402

apply_events_single = tariff.apply_events
tariff.apply_events = functools.partial(apply_events_single, schedule="weekday_weekend")
LONDON = ZoneInfo("Europe/London")
# 2026-10-05 is a Monday (weekday 0)
MONDAY = 5


def iog(sell_day=0.15, sell_night=0.07):
    return tariff.build_daily_tariff(
        [DailyRate(330, 0.278, sell_day), DailyRate(1410, 0.07, sell_night)],
        daily_charge=0.53,
    )


def tesla(t, day_offset, hhmm):
    """(buy, sell) per Tesla's resolver at Monday+day_offset HH:MM."""
    h, m = map(int, hhmm.split(":"))
    moment = datetime(2026, 10, MONDAY + day_offset, h, m, tzinfo=LONDON)
    r = resolver.get_tariff_periods(t, moment)
    return r.buy.price, r.sell.price


def test_iog_base():
    t = iog()
    assert tariff.validate_tariff(t) == []
    for d in range(7):
        assert tesla(t, d, "00:00") == (0.07, 0.07)
        assert tesla(t, d, "05:29") == (0.07, 0.07)
        assert tesla(t, d, "05:30") == (0.278, 0.15)
        assert tesla(t, d, "23:29") == (0.278, 0.15)
        assert tesla(t, d, "23:30") == (0.07, 0.07)


def test_export_capped_to_import():
    t = iog(sell_night=0.15)
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 0, "02:00") == (0.07, 0.07)


def test_more_than_four_pairs_rejected():
    with pytest.raises(TariffError):
        tariff.build_daily_tariff([DailyRate(h * 60, 0.1 + h / 100, 0) for h in range(5)])


def test_event_applies_to_its_app_day_group():
    base = iog()
    t, labels = tariff.apply_events(base, [EventWindow(2, 18 * 60, 19 * 60, 3.0, 3.0)])
    assert tariff.validate_tariff(t) == []
    assert labels == {(3.0, 3.0): "ON_PEAK"}
    assert tesla(t, 2, "18:00") == (3.0, 3.0)  # Wednesday
    assert tesla(t, 2, "18:59") == (3.0, 3.0)
    assert tesla(t, 2, "19:00") == (0.278, 0.15)
    assert tesla(t, 2, "17:59") == (0.278, 0.15)
    for d in (0, 1, 3, 4):  # Tesla app: Mon-Fri share one schedule
        assert tesla(t, d, "18:30") == (3.0, 3.0)
    for d in (5, 6):        # weekend untouched
        assert tesla(t, d, "18:30") == (0.278, 0.15)
    assert tariff.validate_tariff(base) == []  # base untouched


def test_two_events_same_day_different_prices():
    t, labels = tariff.apply_events(
        iog(),
        [
            EventWindow(3, 17 * 60 + 30, 18 * 60 + 30, 3.0, 3.0),
            EventWindow(3, 20 * 60, 21 * 60, 1.5, 1.0),
        ],
    )
    assert tariff.validate_tariff(t) == []
    assert labels[(3.0, 3.0)] == "ON_PEAK"
    assert tesla(t, 3, "18:00") == (3.0, 3.0)
    assert tesla(t, 3, "19:00") == (0.278, 0.15)
    assert tesla(t, 3, "20:30") == (1.5, 1.0)
    assert tesla(t, 3, "21:00") == (0.278, 0.15)


def test_free_electricity_is_super_off_peak():
    t, labels = tariff.apply_events(iog(), [EventWindow(5, 13 * 60, 14 * 60, 0.0, 0.0)])
    assert labels == {(0.0, 0.0): "SUPER_OFF_PEAK"}
    assert tesla(t, 5, "13:30") == (0.0, 0.0)
    assert tesla(t, 4, "13:30") == (0.278, 0.15)


def test_midnight_crossing_event_as_two_windows():
    t, _ = tariff.apply_events(
        iog(),
        [EventWindow(6, 23 * 60, 1440, 0.0, 0.0), EventWindow(0, 0, 60, 0.0, 0.0)],
    )
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 6, "23:15") == (0.0, 0.0)  # Sunday
    assert tesla(t, 7, "00:30") == (0.0, 0.0)  # next Monday
    assert tesla(t, 7, "01:00") == (0.07, 0.07)
    assert tesla(t, 5, "23:15") == (0.0, 0.0)    # weekend group (Sat too)
    assert tesla(t, 1, "00:30") == (0.0, 0.0)    # weekday group (Tue too)
    assert tesla(t, 5, "00:30") == (0.07, 0.07)  # weekend 00:00-01:00 untouched
    assert tesla(t, 1, "23:15") == (0.278, 0.15)  # weekday 23:00-23:30 untouched


def test_labels_run_out_uses_custom():
    base = tariff.build_daily_tariff(
        [DailyRate(0, 0.1, 0.0), DailyRate(360, 0.2, 0.0), DailyRate(720, 0.3, 0.0),
         DailyRate(1080, 0.4, 0.0)]
    )
    t, labels = tariff.apply_events(base, [EventWindow(1, 60, 120, 2.0, 2.0)])
    assert labels[(2.0, 2.0)] == "HA_EVENT_1"
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 1, "01:30") == (2.0, 2.0)


def test_window_swallowing_a_label_everywhere_drops_its_price():
    base = tariff.build_daily_tariff([DailyRate(0, 0.2, 0.1), DailyRate(600, 0.3, 0.1),
                                      DailyRate(660, 0.2, 0.1)])
    windows = [EventWindow(d, 590, 670, 2.0, 1.0) for d in range(7)]
    t, _ = tariff.apply_events(base, windows)
    assert tariff.validate_tariff(t) == []


def test_event_sell_above_buy_rejected():
    with pytest.raises(TariffError):
        tariff.apply_events(iog(), [EventWindow(0, 0, 60, 0.0, 0.5)])


def test_flat_sell_tariff_is_expanded_and_capped():
    base = iog()
    base["sell_tariff"] = {"energy_charges": {"ALL": {"rates": {"ALL": 0.15}}}, "seasons": {}}
    t, _ = tariff.apply_events(base, [EventWindow(0, 17 * 60, 18 * 60, 3.0, 2.0)])
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 0, "17:30") == (3.0, 2.0)
    assert tesla(t, 0, "12:00") == (0.278, 0.15)
    assert tesla(t, 0, "02:00") == (0.07, 0.07)


def _pge_example():
    def season(frm, to, to_day):
        p = lambda fh, th: {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": fh,  # noqa: E731
                            "fromMinute": 0, "toHour": th, "toMinute": 0}
        return {"fromMonth": frm, "fromDay": 1, "toMonth": to, "toDay": to_day,
                "tou_periods": {"PARTIAL_PEAK": {"periods": [p(15, 16), p(21, 0)]},
                                "ON_PEAK": {"periods": [p(16, 21)]},
                                "OFF_PEAK": {"periods": [p(0, 15)]}}}
    rates = {"PARTIAL_PEAK": 0.41, "ON_PEAK": 0.43, "OFF_PEAK": 0.24}
    return {"currency": "USD",
            "energy_charges": {"ALL": {"rates": {"ALL": 0}}, "Summer": {"rates": dict(rates)},
                               "Winter": {"rates": dict(rates)}},
            "seasons": {"Summer": season(10, 5, 31), "Winter": season(6, 9, 30)}}


def test_tesla_example_tariff_validates_and_takes_event():
    base = _pge_example()
    assert tariff.validate_tariff(base) == []
    t, labels = tariff.apply_events(base, [EventWindow(0, 17 * 60, 18 * 60, 1.0, 1.0)])
    assert labels[(1.0, 1.0)] == "SUPER_OFF_PEAK"  # only unused standard label
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 0, "17:30")[0] == 1.0
    assert tesla(t, 5, "17:30")[0] == 0.43


def test_validator_catches_gap_and_overlap():
    t = iog()
    p = t["seasons"]["All Year"]["tou_periods"]["OFF_PEAK"]["periods"][0]
    p["toHour"], p["toMinute"] = 4, 0
    assert any("gap" in e for e in tariff.validate_tariff(t))
    t = iog()
    p = t["seasons"]["All Year"]["tou_periods"]["OFF_PEAK"]["periods"][0]
    p["toHour"], p["toMinute"] = 7, 0
    assert any("overlap" in e for e in tariff.validate_tariff(t))


# --------------------------------------------------------------------------- #
# Ryan's BAU plan
# --------------------------------------------------------------------------- #
RYAN_BAU = [
    DailyRate(330, 0.35, 0.01, "Mid-Peak"),     # 05:30
    DailyRate(780, 0.35, 0.01, "Mid-Peak"),     # 13:00
    DailyRate(900, 0.35, 0.01, "Mid-Peak"),     # 15:00
    DailyRate(1080, 0.35, 0.01, "Mid-Peak"),    # 18:00
    DailyRate(1140, 0.35, 0.01, "Mid-Peak"),    # 19:00 (to 20:30)
    DailyRate(1230, 0.35, 0.12, "Peak"),        # 20:30
    DailyRate(1410, 0.07, 0.01, "Super Off-Peak"),  # 23:30
]


def ryan():
    return tariff.build_daily_tariff(RYAN_BAU, name="Ryan BAU")


def test_ryan_bau_matches_app_plan():
    t = ryan()
    assert tariff.validate_tariff(t) == []
    season = t["seasons"]["All Year"]["tou_periods"]
    assert set(season) == {"SUPER_OFF_PEAK", "PARTIAL_PEAK", "ON_PEAK"}
    assert t["energy_charges"]["All Year"]["rates"] == {
        "SUPER_OFF_PEAK": 0.07, "PARTIAL_PEAK": 0.35, "ON_PEAK": 0.35}
    assert t["sell_tariff"]["energy_charges"]["All Year"]["rates"] == {
        "SUPER_OFF_PEAK": 0.01, "PARTIAL_PEAK": 0.01, "ON_PEAK": 0.12}
    for d in range(7):
        assert tesla(t, d, "05:29") == (0.07, 0.01)
        assert tesla(t, d, "05:30") == (0.35, 0.01)
        assert tesla(t, d, "20:29") == (0.35, 0.01)
        assert tesla(t, d, "20:30") == (0.35, 0.12)
        assert tesla(t, d, "23:29") == (0.35, 0.12)
        assert tesla(t, d, "23:30") == (0.07, 0.01)


def test_ryan_bau_with_saving_and_free_sessions():
    t, labels = tariff.apply_events(
        ryan(),
        [EventWindow(2, 18 * 60, 19 * 60, 3.0, 3.0), EventWindow(2, 13 * 60, 14 * 60, 0.0, 0.0)],
    )
    assert tariff.validate_tariff(t) == []
    assert labels[(3.0, 3.0)] == "OFF_PEAK"      # the only spare standard label
    assert labels[(0.0, 0.0)] == "HA_EVENT_1"
    assert tesla(t, 2, "18:30") == (3.0, 3.0)
    assert tesla(t, 2, "13:30") == (0.0, 0.0)
    assert tesla(t, 2, "21:00") == (0.35, 0.12)
    assert tesla(t, 5, "18:30") == (0.35, 0.01)  # weekend untouched


def test_labels_must_be_all_or_nothing_and_consistent():
    with pytest.raises(TariffError):
        tariff.build_daily_tariff([DailyRate(0, 0.1, 0, "Peak"), DailyRate(60, 0.2, 0)])
    with pytest.raises(TariffError):
        tariff.build_daily_tariff([DailyRate(0, 0.1, 0, "Peak"), DailyRate(60, 0.2, 0, "Peak")])


def test_ryan_power_down_day_relabel():
    relabel = (("ON_PEAK", "PARTIAL_PEAK"),)
    t, labels = tariff.apply_events(
        ryan(), [EventWindow(2, 18 * 60, 19 * 60, label="ON_PEAK", relabel=relabel)]
    )
    assert labels == {}
    assert tariff.validate_tariff(t) == []
    assert tesla(t, 2, "18:30") == (0.35, 0.12)
    assert tesla(t, 2, "21:00") == (0.35, 0.01)
    assert tesla(t, 2, "23:30") == (0.07, 0.01)
    assert tesla(t, 1, "21:00") == (0.35, 0.01)  # Mon-Fri share one schedule
    assert tesla(t, 5, "21:00") == (0.35, 0.12)  # weekend untouched
    assert tesla(t, 6, "21:00") == (0.35, 0.12)



def _rows(part, label):
    return sorted(
        (p["fromHour"] * 60 + p["fromMinute"], p["toHour"] * 60 + p["toMinute"],
         p["fromDayOfWeek"], p["toDayOfWeek"])
        for p in part["seasons"]["All Year"]["tou_periods"][label]["periods"]
    )


def test_ryan_bau_keeps_separate_mid_peak_rows():
    t = ryan()
    assert _rows(t, "PARTIAL_PEAK") == [
        (330, 780, 0, 6), (780, 900, 0, 6), (900, 1080, 0, 6), (1080, 1140, 0, 6),
        (1140, 1230, 0, 6)]
    assert _rows(t, "ON_PEAK") == [(1230, 1410, 0, 6)]
    assert _rows(t, "SUPER_OFF_PEAK") == [(1410, 330, 0, 6)]  # 23:30-05:30
    assert _rows(t["sell_tariff"], "PARTIAL_PEAK") == _rows(t, "PARTIAL_PEAK")


def test_event_keeps_rows_and_splits_only_its_day_group():
    t, _ = tariff.apply_events(
        ryan(),
        [EventWindow(2, 18 * 60, 19 * 60, label="ON_PEAK", relabel=(("ON_PEAK", "PARTIAL_PEAK"),))],
    )
    assert tariff.validate_tariff(t) == []
    # Mon-Fri (the app's weekday schedule): 18:00-19:00 row is Peak; other
    # Mid-Peak rows intact; 20:30-23:30 flipped to Mid-Peak as its own row.
    wk = {(a, b, d1, d2) for a, b, d1, d2 in _rows(t, "PARTIAL_PEAK") if d1 <= 2 <= d2}
    # Unchanged rows stay all-week; only changed rows split Mon-Fri / Sat-Sun.
    assert wk == {(330, 780, 0, 6), (780, 900, 0, 6), (900, 1080, 0, 6),
                  (1140, 1230, 0, 6), (1230, 1410, 0, 4)}
    assert {(a, b, d1, d2) for a, b, d1, d2 in _rows(t, "ON_PEAK") if d1 <= 2 <= d2} == {
        (1080, 1140, 0, 4)}
    # Weekend schedule unchanged.
    we = {(a, b, d1, d2) for a, b, d1, d2 in _rows(t, "PARTIAL_PEAK") if d1 <= 5 <= d2}
    assert we == {(330, 780, 0, 6), (780, 900, 0, 6), (900, 1080, 0, 6),
                  (1080, 1140, 5, 6), (1140, 1230, 0, 6)}
    assert {(a, b, d1, d2) for a, b, d1, d2 in _rows(t, "ON_PEAK") if d1 <= 5 <= d2} == {
        (1230, 1410, 5, 6)}
    assert tesla(t, 2, "18:30") == (0.35, 0.12)
    assert tesla(t, 5, "18:30") == (0.35, 0.01)


def test_power_up_day_keeps_rows():
    t, _ = tariff.apply_events(ryan(), [EventWindow(5, 13 * 60, 15 * 60, label="SUPER_OFF_PEAK")])
    assert tariff.validate_tariff(t) == []
    sat_sop = {(a, b) for a, b, d1, d2 in _rows(t, "SUPER_OFF_PEAK") if d1 <= 5 <= d2}
    assert (780, 900) in sat_sop
    sat_mid = {(a, b) for a, b, d1, d2 in _rows(t, "PARTIAL_PEAK") if d1 <= 5 <= d2}
    assert sat_mid == {(330, 780), (900, 1080), (1080, 1140), (1140, 1230)}
    assert tesla(t, 5, "14:00") == (0.07, 0.01)
    assert tesla(t, 4, "14:00") == (0.35, 0.01)
    # Overnight Super Off-Peak untouched either side of the Power Up day.
    for day, hhmm in ((4, "23:30"), (5, "00:00"), (5, "05:29"), (5, "23:30"),
                      (6, "00:00"), (6, "05:29")):
        assert tesla(t, day, hhmm) == (0.07, 0.01)
    assert tesla(t, 5, "05:30") == (0.35, 0.01)
    assert tesla(t, 5, "21:00") == (0.35, 0.12)  # Peak kept on Power Up days
    assert (1410, 330) in sat_sop  # one overnight row 23:30-05:30, as in the app



def test_default_single_schedule_applies_all_week():
    t, _ = apply_events_single(
        ryan(), [EventWindow(3, 19 * 60, 20 * 60, label="ON_PEAK",
                             relabel=(("ON_PEAK", "PARTIAL_PEAK"),))]
    )
    assert tariff.validate_tariff(t) == []
    for d in range(7):
        assert tesla(t, d, "19:30") == (0.35, 0.12)
        assert tesla(t, d, "21:00") == (0.35, 0.01)
        assert tesla(t, d, "23:45") == (0.07, 0.01)
    assert {(a, b, d1, d2) for a, b, d1, d2 in _rows(t, "SUPER_OFF_PEAK")} == {(1410, 330, 0, 6)}
    assert all((d1, d2) == (0, 6) for _, _, d1, d2 in
               _rows(t, "PARTIAL_PEAK") + _rows(t, "ON_PEAK"))


def test_brand_images_present():
    """HA 2026.x serves custom-integration icons from <integration>/brand/."""
    brand = _path.parent / "brand"
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        data = (brand / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", name
