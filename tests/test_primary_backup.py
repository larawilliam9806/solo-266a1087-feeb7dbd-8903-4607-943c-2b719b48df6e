"""主备表合并：主表优先、备表补缺口、按计入时长平均声能。"""
from __future__ import annotations

import math


def _reading(number, point, role, start, end, db):
    return {"point": point, "role": role, "start": start, "end": end, "db": db}


HOUR = "2026-11-21T08:00:00Z"
T15 = "2026-11-21T08:15:00Z"
T30 = "2026-11-21T08:30:00Z"
T45 = "2026-11-21T08:45:00Z"
NEXT = "2026-11-21T09:00:00Z"


def test_backup_fills_primary_gap(client):
    # 主表覆盖前 15 分钟和后 15 分钟，中间 30 分钟缺口由备表补。
    client.put("/readings/P1", json=_reading("P1", "P", "primary", HOUR, T15, 70.0))
    client.put("/readings/P2", json=_reading("P2", "P", "primary", T45, NEXT, 70.0))
    client.put("/readings/B1", json=_reading("B1", "P", "backup", T15, T45, 50.0))

    result = client.get(f"/points/P/hours/{HOUR}").json()
    assert result["covered_seconds"] == 3600
    assert result["primary_seconds"] == 1800
    assert result["backup_seconds"] == 1800

    expected = 10 * math.log10((1800 * 10 ** 7.0 + 1800 * 10 ** 5.0) / 3600)
    assert abs(result["leq_db"] - expected) < 1e-9


def test_primary_takes_precedence_over_backup(client):
    # 主备同时覆盖整小时：全部秒计主表，备表 0 秒。
    client.put("/readings/P1", json=_reading("P1", "P", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/B1", json=_reading("B1", "P", "backup", HOUR, NEXT, 90.0))

    result = client.get(f"/points/P/hours/{HOUR}").json()
    assert result["primary_seconds"] == 3600
    assert result["backup_seconds"] == 0
    assert abs(result["leq_db"] - 60.0) < 1e-9


def test_backup_only_when_no_primary(client):
    client.put("/readings/B1", json=_reading("B1", "P", "backup", HOUR, T30, 55.0))
    result = client.get(f"/points/P/hours/{HOUR}").json()
    assert result["covered_seconds"] == 1800
    assert result["primary_seconds"] == 0
    assert result["backup_seconds"] == 1800
    assert abs(result["leq_db"] - 55.0) < 1e-9


def test_uncovered_seconds_excluded_from_average(client):
    # 主表 30 分钟 80 dB，备表 15 分钟 60 dB，剩余 15 分钟无覆盖。
    client.put("/readings/P1", json=_reading("P1", "P", "primary", HOUR, T30, 80.0))
    client.put("/readings/B1", json=_reading("B1", "P", "backup", T30, T45, 60.0))

    result = client.get(f"/points/P/hours/{HOUR}").json()
    assert result["covered_seconds"] == 2700
    assert result["primary_seconds"] == 1800
    assert result["backup_seconds"] == 900

    expected = 10 * math.log10((1800 * 10 ** 8.0 + 900 * 10 ** 6.0) / 2700)
    assert abs(result["leq_db"] - expected) < 1e-9

    # 区间查询：无覆盖的相邻小时不返回。
    rng = client.get(
        f"/points/P/hours?start={HOUR}&end=2026-11-21T11:00:00Z"
    ).json()
    assert len(rng["hours"]) == 1


def test_calibration_applies_per_role(client):
    # 主表前半小时 60 dB（校准 +10），备表补后半小时 60 dB（无校准）。
    client.put("/readings/P1", json=_reading("P1", "P", "primary", HOUR, T30, 60.0))
    client.put("/readings/B1", json=_reading("B1", "P", "backup", T30, NEXT, 60.0))
    client.put(
        "/calibrations/C1",
        json={
            "point": "P", "role": "primary",
            "start": HOUR, "end": T30, "offset": 10.0,
        },
    )
    result = client.get(f"/points/P/hours/{HOUR}").json()
    assert result["primary_seconds"] == 1800
    assert result["backup_seconds"] == 1800
    expected = 10 * math.log10((10 ** 7.0 + 10 ** 6.0) / 2)
    assert abs(result["leq_db"] - expected) < 1e-9


def test_points_are_isolated(client):
    client.put("/readings/R1", json=_reading("R1", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/R2", json=_reading("R2", "B", "primary", HOUR, NEXT, 80.0))
    a = client.get(f"/points/A/hours/{HOUR}").json()
    b = client.get(f"/points/B/hours/{HOUR}").json()
    assert abs(a["leq_db"] - 60.0) < 1e-9
    assert abs(b["leq_db"] - 80.0) < 1e-9
