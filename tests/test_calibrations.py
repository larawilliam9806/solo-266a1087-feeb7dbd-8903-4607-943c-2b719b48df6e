"""校准单：偏移叠加、修改、撤回、重叠约束与受影响小时重算。"""
from __future__ import annotations


def _reading(number, point, role, start, end, db):
    return {"point": point, "role": role, "start": start, "end": end, "db": db}


def _cal(number, point, role, start, end, offset):
    return {"point": point, "role": role, "start": start, "end": end, "offset": offset}


HOUR = "2026-11-21T08:00:00Z"
NEXT = "2026-11-21T09:00:00Z"


def test_calibration_offset_applied_to_overlap(client):
    # 读数 60 dB 覆盖整小时；校准单在该小时前半小时生效，偏移 +3 dB。
    client.put("/readings/R1", json=_reading("R1", "P1", "primary", HOUR, NEXT, 60.0))
    resp = client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, "2026-11-21T08:30:00Z", 3.0),
    )
    assert resp.status_code == 200
    result = client.get(f"/points/P1/hours/{HOUR}").json()
    assert result["covered_seconds"] == 3600
    # Leq = 10*lg((1800*10^6.3 + 1800*10^6.0)/3600)
    expected = 10 * __import__("math").log10((10 ** 6.3 + 10 ** 6.0) / 2)
    assert abs(result["leq_db"] - expected) < 1e-9


def test_calibration_update_recomputes_affected_hours(client):
    client.put("/readings/R1", json=_reading("R1", "P1", "primary", HOUR, NEXT, 60.0))
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, "2026-11-21T08:30:00Z", 3.0),
    )
    before = client.get(f"/points/P1/hours/{HOUR}").json()["leq_db"]

    # 修改偏移为 -10 dB，且缩到 15 分钟。
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, "2026-11-21T08:15:00Z", -10.0),
    )
    after = client.get(f"/points/P1/hours/{HOUR}").json()
    assert after["leq_db"] != before
    expected = 10 * __import__("math").log10(
        (900 * 10 ** 5.0 + 2700 * 10 ** 6.0) / 3600
    )
    assert abs(after["leq_db"] - expected) < 1e-9


def test_calibration_withdraw_restores_unadjusted_values(client):
    client.put("/readings/R1", json=_reading("R1", "P1", "primary", HOUR, NEXT, 60.0))
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, NEXT, 5.0),
    )
    adjusted = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(adjusted["leq_db"] - 65.0) < 1e-9

    resp = client.post("/calibrations/C1/withdraw")
    assert resp.status_code == 200
    assert resp.json()["withdrawn"] is True

    restored = client.get(f"/points/P1/hours/{HOUR}").json()
    assert abs(restored["leq_db"] - 60.0) < 1e-9

    # 撤回幂等：重复撤回不报错。
    again = client.post("/calibrations/C1/withdraw")
    assert again.status_code == 200

    # 已撤回单据不能修改或复活。
    conflict = client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, NEXT, 1.0),
    )
    assert conflict.status_code == 409

    # 撤回后原小时结果仍是 60 dB，未被失败的修改破坏。
    assert abs(client.get(f"/points/P1/hours/{HOUR}").json()["leq_db"] - 60.0) < 1e-9


def test_calibration_overlap_rejected_and_moved_window(client):
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, "2026-11-21T08:30:00Z", 1.0),
    )
    # 同仪器重叠 -> 409
    resp = client.put(
        "/calibrations/C2",
        json=_cal("C2", "P1", "primary", "2026-11-21T08:20:00Z", NEXT, 2.0),
    )
    assert resp.status_code == 409

    # 不同角色允许重叠
    resp = client.put(
        "/calibrations/C3",
        json=_cal("C3", "P1", "backup", "2026-11-21T08:20:00Z", NEXT, 2.0),
    )
    assert resp.status_code == 200

    # 撤回后同编号区间不再参与重叠检查，新单据可登记在该窗口。
    client.post("/calibrations/C1/withdraw")
    resp = client.put(
        "/calibrations/C4",
        json=_cal("C4", "P1", "primary", HOUR, "2026-11-21T08:30:00Z", 4.0),
    )
    assert resp.status_code == 200


def test_update_calibration_window_touching_other_hours(client):
    # 读数跨两个小时，校准从第一小时挪到第二小时，两个小时都要重算。
    client.put(
        "/readings/R1",
        json=_reading("R1", "P1", "primary", HOUR, "2026-11-21T10:00:00Z", 60.0),
    )
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, NEXT, 10.0),
    )
    h1 = client.get(f"/points/P1/hours/{HOUR}").json()
    h2 = client.get(f"/points/P1/hours/{NEXT}").json()
    assert abs(h1["leq_db"] - 70.0) < 1e-9
    assert abs(h2["leq_db"] - 60.0) < 1e-9

    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", NEXT, "2026-11-21T10:00:00Z", 10.0),
    )
    h1 = client.get(f"/points/P1/hours/{HOUR}").json()
    h2 = client.get(f"/points/P1/hours/{NEXT}").json()
    assert abs(h1["leq_db"] - 60.0) < 1e-9
    assert abs(h2["leq_db"] - 70.0) < 1e-9


def test_withdraw_unknown_calibration_404(client):
    resp = client.post("/calibrations/NOPE/withdraw")
    assert resp.status_code == 404


def test_calibration_point_and_role_are_immutable(client):
    client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, NEXT, 1.0),
    )
    # 改测点 -> 409，原数据不变
    resp = client.put(
        "/calibrations/C1",
        json=_cal("C1", "P2", "primary", HOUR, NEXT, 1.0),
    )
    assert resp.status_code == 409
    # 改角色 -> 409
    resp = client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "backup", HOUR, NEXT, 1.0),
    )
    assert resp.status_code == 409
    rows = client.get("/calibrations").json()
    assert len(rows) == 1
    assert rows[0]["point"] == "P1"
    assert rows[0]["role"] == "primary"
    # 起止与偏移仍可正常修改
    ok = client.put(
        "/calibrations/C1",
        json=_cal("C1", "P1", "primary", HOUR, "2026-11-21T08:30:00Z", 2.0),
    )
    assert ok.status_code == 200
    assert ok.json()["offset"] == 2.0
