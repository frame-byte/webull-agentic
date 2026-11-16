import importlib.util
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).parent.parent / "deploy" / "watchdog.py")
wdm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wdm)

UP = {"ok": True, "mode": "live", "broker": "webull_openapi", "kill_switch": False}
# Monday 2026-11-16 06:00 ET: before the 9:00 heartbeat
T0 = datetime(2026, 11, 16, 6, 0, tzinfo=wdm.ET).timestamp()


def test_down_reminder_and_recovery():
    wd = wdm.Watchdog(fail_threshold=3, remind_min=30)
    assert wd.step(T0, UP) == []
    assert wd.step(T0 + 30, None, "ConnectionRefusedError") == []
    assert wd.step(T0 + 60, None) == []
    down = wd.step(T0 + 90, None, "ConnectionRefusedError")
    assert len(down) == 1 and "DOWN" in down[0]
    assert wd.step(T0 + 120, None) == []                      # no spam
    assert "still DOWN" in wd.step(T0 + 90 + 1800, None)[0]   # reminder after 30 min
    rec = wd.step(T0 + 2000, UP)
    assert len(rec) == 1 and "RECOVERED" in rec[0]
    assert wd.step(T0 + 2030, None) == []                     # counter reset


def test_single_blip_is_not_an_alert():
    wd = wdm.Watchdog(fail_threshold=3)
    wd.step(T0, UP)
    assert wd.step(T0 + 30, None) == [] and wd.step(T0 + 60, UP) == []


def test_kill_switch_and_mode_changes():
    wd = wdm.Watchdog()
    wd.step(T0, UP)
    assert "Kill switch is ON" in wd.step(T0 + 30, {**UP, "kill_switch": True})[0]
    assert "OFF" in wd.step(T0 + 60, UP)[0]
    assert "live -> dryrun" in wd.step(T0 + 90, {**UP, "mode": "dryrun"})[0]


def test_daily_heartbeat_weekdays_once():
    wd = wdm.Watchdog(heartbeat_hour_et=9)
    nine = datetime(2026, 11, 16, 9, 1, tzinfo=wdm.ET).timestamp()
    assert wd.step(T0, UP) == []
    assert "Daily check" in wd.step(nine, UP)[0]
    assert wd.step(nine + 60, UP) == []
    saturday = datetime(2026, 11, 21, 10, 0, tzinfo=wdm.ET).timestamp()
    assert wd.step(saturday, UP) == []
