"""读数登记、重叠约束、原样重传与小时计算基础规则。"""
from __future__ import annotations


def _reading(number: str, point: str, role: str, start: str, end: str, db: float) -> dict:
    return {
        "point": point,
        "role": role,
        "start": start,
        "end": end,
        "db": db,
    }


def test_register_and_query_hour_leq(client):
    # 主表覆盖整小时，恒定 60 dB -> Leq = 60 dB，主表计入 3600 秒。
    payload = _reading(
        "R1", "P1", "primary",
        "2026-11-21T08:00:00Z", "2026-11-21T09:00:00Z", 60.0,
    )
    resp = client.put("/readings/R1", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["number"] == "R1"
    assert body["start"] == "2026-11-21T08:00:00Z"
    assert body["db"] == 60.0

    resp = client.get("/points/P1/hours/2026-11-21T08:00:00Z")
    assert resp.status_code == 200
    result = resp.json()
    assert result["covered_seconds"] == 3600
    assert result["primary_seconds"] == 3600
    assert result["backup_seconds"] == 0
    assert abs(result["leq_db"] - 60.0) < 1e-9


def test_partial_hour_and_no_data(client):
    # 只覆盖半小时 50 dB。
    client.put(
        "/readings/R1",
        json=_reading(
            "R1", "P1", "primary",
            "2026-11-21T08:00:00Z", "2026-11-21T08:30:00Z", 50.0,
        ),
    )
    result = client.get("/points/P1/hours/2026-11-21T08:00:00Z").json()
    assert result["covered_seconds"] == 1800
    assert abs(result["leq_db"] - 50.0) < 1e-9

    # 无覆盖的小时报无数据（404）。
    resp = client.get("/points/P1/hours/2026-11-21T09:00:00Z")
    assert resp.status_code == 404
    assert "无数据" in resp.json()["detail"]


def test_retransmission_same_payload_is_idempotent(client):
    payload = _reading(
        "R1", "P1", "primary",
        "2026-11-21T08:00:00Z", "2026-11-21T08:10:00Z", 55.0,
    )
    first = client.put("/readings/R1", json=payload)
    second = client.put("/readings/R1", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    # 同号不同内容 -> 409，原数据保持不变。
    changed = dict(payload)
    changed["db"] = 70.0
    resp = client.put("/readings/R1", json=changed)
    assert resp.status_code == 409
    rows = client.get("/readings").json()
    assert len(rows) == 1
    assert rows[0]["db"] == 55.0


def test_overlapping_readings_same_instrument_rejected(client):
    client.put(
        "/readings/R1",
        json=_reading(
            "R1", "P1", "primary",
            "2026-11-21T08:00:00Z", "2026-11-21T08:10:00Z", 55.0,
        ),
    )
    # 同仪器相邻区间（左闭右开）允许。
    ok = client.put(
        "/readings/R2",
        json=_reading(
            "R2", "P1", "primary",
            "2026-11-21T08:10:00Z", "2026-11-21T08:20:00Z", 56.0,
        ),
    )
    assert ok.status_code == 200

    # 重叠一秒即拒绝。
    bad = client.put(
        "/readings/R3",
        json=_reading(
            "R3", "P1", "primary",
            "2026-11-21T08:19:59Z", "2026-11-21T08:25:00Z", 57.0,
        ),
    )
    assert bad.status_code == 409

    # 不同角色（备表）与主表区间重叠是允许的。
    backup = client.put(
        "/readings/B1",
        json=_reading(
            "B1", "P1", "backup",
            "2026-11-21T08:00:00Z", "2026-11-21T08:10:00Z", 54.0,
        ),
    )
    assert backup.status_code == 200


def test_invalid_intervals_and_timezone(client):
    # end <= start
    resp = client.put(
        "/readings/BAD1",
        json=_reading(
            "BAD1", "P1", "primary",
            "2026-11-21T08:10:00Z", "2026-11-21T08:10:00Z", 50.0,
        ),
    )
    assert resp.status_code == 400

    # 无时区
    resp = client.put(
        "/readings/BAD2",
        json=_reading(
            "BAD2", "P1", "primary",
            "2026-11-21T08:00:00", "2026-11-21T08:10:00", 50.0,
        ),
    )
    assert resp.status_code == 422  # Pydantic 校验阶段拒绝

    # 非整点 hour 查询
    resp = client.get("/points/P1/hours/2026-11-21T08:30:00Z")
    assert resp.status_code == 400


def test_backfill_allowed(client):
    # 补交任意历史时刻的读数都接受。
    resp = client.put(
        "/readings/OLD1",
        json=_reading(
            "OLD1", "P1", "primary",
            "2000-01-01T00:00:00Z", "2000-01-01T01:00:00Z", 42.0,
        ),
    )
    assert resp.status_code == 200
    result = client.get("/points/P1/hours/2000-01-01T00:00:00Z").json()
    assert abs(result["leq_db"] - 42.0) < 1e-9
