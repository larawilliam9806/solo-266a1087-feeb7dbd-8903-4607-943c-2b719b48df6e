"""主表通道接反更正：读数换回实际测点、校准按实际测点、幂等与重叠约束。"""
from __future__ import annotations

import math


def _reading(number, point, role, start, end, db):
    return {"point": point, "role": role, "start": start, "end": end, "db": db}


def _cal(number, point, role, start, end, offset):
    return {"point": point, "role": role, "start": start, "end": end, "offset": offset}


def _corr(point_a, point_b, start, end):
    return {"point_a": point_a, "point_b": point_b, "start": start, "end": end}


HOUR = "2026-11-21T08:00:00Z"
T15 = "2026-11-21T08:15:00Z"
T30 = "2026-11-21T08:30:00Z"
T45 = "2026-11-21T08:45:00Z"
NEXT = "2026-11-21T09:00:00Z"
HOUR2 = "2026-11-21T10:00:00Z"


def test_swap_moves_primary_readings_to_actual_points(client):
    # 接反时段：A 名下记的是 B 的 80 dB，B 名下记的是 A 的 60 dB。
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 80.0))
    assert abs(client.get(f"/points/A/hours/{HOUR}").json()["leq_db"] - 60.0) < 1e-9
    assert abs(client.get(f"/points/B/hours/{HOUR}").json()["leq_db"] - 80.0) < 1e-9

    resp = client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["number"] == "SW1"
    assert body["start"] == HOUR and body["end"] == NEXT

    # 登记更正后：区间内主表读数换回各自测点。
    a = client.get(f"/points/A/hours/{HOUR}").json()
    b = client.get(f"/points/B/hours/{HOUR}").json()
    assert abs(a["leq_db"] - 80.0) < 1e-9
    assert a["primary_seconds"] == 3600
    assert abs(b["leq_db"] - 60.0) < 1e-9
    assert b["primary_seconds"] == 3600


def test_readings_crossing_correction_boundary_are_split(client):
    # 读数跨越更正边界：窗口内换归属，窗口外仍归登记测点。
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, T30, 80.0))
    client.put("/corrections/SW1", json=_corr("A", "B", T15, T45))

    # A：本点 60 dB 扣除 [08:15,08:45)，换回 B 在 [08:15,08:30) 的 80 dB；
    # [08:30,08:45) 对方无读数，该段无覆盖。
    a = client.get(f"/points/A/hours/{HOUR}").json()
    assert a["covered_seconds"] == 2700
    assert a["primary_seconds"] == 2700
    expected_a = 10 * math.log10((1800 * 10 ** 6.0 + 900 * 10 ** 8.0) / 2700)
    assert abs(a["leq_db"] - expected_a) < 1e-9

    # B：本点 [08:00,08:30) 的 80 dB 扣除 [08:15,08:30)，换回 A 在 [08:15,08:45) 的 60 dB。
    b = client.get(f"/points/B/hours/{HOUR}").json()
    assert b["covered_seconds"] == 2700
    expected_b = 10 * math.log10((900 * 10 ** 8.0 + 1800 * 10 ** 6.0) / 2700)
    assert abs(b["leq_db"] - expected_b) < 1e-9


def test_calibration_applies_by_actual_point(client):
    # 换回的读数按实际测点取校准偏移；校准单始终按登记测点核算。
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 50.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 60.0))
    client.put("/calibrations/CA", json=_cal("CA", "A", "primary", HOUR, NEXT, 10.0))
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))

    # B 名下的 60 dB 换回 A，叠加 A 的校准 +10 -> 70 dB。
    a = client.get(f"/points/A/hours/{HOUR}").json()
    assert abs(a["leq_db"] - 70.0) < 1e-9
    # A 名下的 50 dB 换回 B，B 无校准单 -> 50 dB。
    b = client.get(f"/points/B/hours/{HOUR}").json()
    assert abs(b["leq_db"] - 50.0) < 1e-9


def test_backup_readings_stay_with_registered_point(client):
    # 备表读数不参与换归属，仍按登记测点核算。
    client.put("/readings/BA", json=_reading("BA", "A", "backup", HOUR, NEXT, 55.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 80.0))
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))

    # A：换回的主表 80 dB 优先，本点备表 55 dB 不计入。
    a = client.get(f"/points/A/hours/{HOUR}").json()
    assert a["primary_seconds"] == 3600
    assert a["backup_seconds"] == 0
    assert abs(a["leq_db"] - 80.0) < 1e-9
    # B：主表被换走，无任何覆盖 -> 404。
    assert client.get(f"/points/B/hours/{HOUR}").status_code == 404


def test_correction_can_precede_backfill(client):
    # 先登记更正，再补交读数：补交触发双方测点的小时重算。
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 80.0))

    assert abs(client.get(f"/points/A/hours/{HOUR}").json()["leq_db"] - 80.0) < 1e-9
    assert abs(client.get(f"/points/B/hours/{HOUR}").json()["leq_db"] - 60.0) < 1e-9


def test_backfill_crossing_boundary_after_correction(client):
    # 更正先登记，补交的主表读数跨越更正边界时同样分段归属。
    client.put("/corrections/SW1", json=_corr("A", "B", T30, NEXT))
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 80.0))

    # A：前半段本点 60 dB，后半段换回 B 的 80 dB。
    a = client.get(f"/points/A/hours/{HOUR}").json()
    expected_a = 10 * math.log10((1800 * 10 ** 6.0 + 1800 * 10 ** 8.0) / 3600)
    assert abs(a["leq_db"] - expected_a) < 1e-9
    # B：前半段本点 80 dB，后半段换回 A 的 60 dB。
    b = client.get(f"/points/B/hours/{HOUR}").json()
    expected_b = 10 * math.log10((1800 * 10 ** 8.0 + 1800 * 10 ** 6.0) / 3600)
    assert abs(b["leq_db"] - expected_b) < 1e-9


def test_adjacent_corrections_allowed_overlap_rejected(client):
    resp = client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    assert resp.status_code == 200

    # 同一测点相邻时段允许（左闭右开）。
    resp = client.put("/corrections/SW2", json=_corr("A", "C", NEXT, HOUR2))
    assert resp.status_code == 200

    # 经 A 与 SW1 重叠 -> 409。
    resp = client.put("/corrections/SW3", json=_corr("C", "A", T30, HOUR2))
    assert resp.status_code == 409
    # 经 B 与 SW1 重叠（B 在另一条记录中是 point_b）-> 409。
    resp = client.put("/corrections/SW4", json=_corr("B", "D", T30, HOUR2))
    assert resp.status_code == 409
    # 不涉及任何已登记测点的测点对，同时段允许。
    resp = client.put("/corrections/SW5", json=_corr("D", "E", T30, HOUR2))
    assert resp.status_code == 200


def test_correction_idempotent_retransmit(client):
    payload = _corr("A", "B", HOUR, NEXT)
    first = client.put("/corrections/SW1", json=payload)
    assert first.status_code == 200

    # 同号、同一对测点、同一区间 -> 返回原记录。
    second = client.put("/corrections/SW1", json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()

    # 测点对无方向：交换 A/B 顺序仍是同一对测点，幂等返回原记录。
    swapped = client.put("/corrections/SW1", json=_corr("B", "A", HOUR, NEXT))
    assert swapped.status_code == 200
    assert swapped.json() == first.json()

    # 同号内容有变 -> 409，原记录保持不变。
    conflict = client.put("/corrections/SW1", json=_corr("A", "B", HOUR, T30))
    assert conflict.status_code == 409
    conflict = client.put("/corrections/SW1", json=_corr("A", "C", HOUR, NEXT))
    assert conflict.status_code == 409
    rows = client.get("/corrections").json()
    assert rows == [first.json()]


def test_correction_validation(client):
    # 两个测点相同 -> 400。
    resp = client.put("/corrections/BAD1", json=_corr("A", "A", HOUR, NEXT))
    assert resp.status_code == 400
    # start >= end -> 400。
    resp = client.put("/corrections/BAD2", json=_corr("A", "B", NEXT, HOUR))
    assert resp.status_code == 400
    # 非整秒 -> 422（Pydantic 校验阶段拒绝）。
    resp = client.put(
        "/corrections/BAD3",
        json=_corr("A", "B", "2026-11-21T08:00:00.5Z", NEXT),
    )
    assert resp.status_code == 422


def test_raw_readings_and_retransmit_follow_submitted_content(client):
    # 原始读数查询以报送内容为准，不受更正影响。
    payload = _reading("RA", "A", "primary", HOUR, NEXT, 60.0)
    client.put("/readings/RA", json=payload)
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))

    rows = client.get("/readings", params={"point": "A"}).json()
    assert len(rows) == 1
    assert rows[0]["point"] == "A"
    assert rows[0]["db"] == 60.0

    # 同号重传以报送内容为准：原样重传幂等，内容不同 409。
    again = client.put("/readings/RA", json=payload)
    assert again.status_code == 200
    changed = client.put("/readings/RA", json=_reading("RA", "B", "primary", HOUR, NEXT, 60.0))
    assert changed.status_code == 409


def test_failed_correction_keeps_hours_unchanged(client):
    # 重叠更正被拒绝时，已有更正与小时结果保持原状。
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    client.put("/readings/RB", json=_reading("RB", "B", "primary", HOUR, NEXT, 80.0))
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    assert abs(client.get(f"/points/A/hours/{HOUR}").json()["leq_db"] - 80.0) < 1e-9

    resp = client.put("/corrections/SW2", json=_corr("A", "C", T30, HOUR2))
    assert resp.status_code == 409
    # 小时结果未被失败的写入破坏。
    assert abs(client.get(f"/points/A/hours/{HOUR}").json()["leq_db"] - 80.0) < 1e-9
    rows = client.get("/corrections").json()
    assert [r["number"] for r in rows] == ["SW1"]


def test_list_corrections_filter_by_point(client):
    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    client.put("/corrections/SW2", json=_corr("B", "C", NEXT, HOUR2))
    client.put("/corrections/SW3", json=_corr("D", "E", HOUR, NEXT))

    all_rows = client.get("/corrections").json()
    assert len(all_rows) == 3
    # 按测点过滤匹配更正任一端。
    b_rows = client.get("/corrections", params={"point": "B"}).json()
    assert sorted(r["number"] for r in b_rows) == ["SW1", "SW2"]
    d_rows = client.get("/corrections", params={"point": "D"}).json()
    assert [r["number"] for r in d_rows] == ["SW3"]


def test_correction_only_recomputes_involved_points(client):
    # 未涉及更正的测点结果不受登记更正影响。
    client.put("/readings/RX", json=_reading("RX", "X", "primary", HOUR, NEXT, 66.0))
    client.put("/readings/RA", json=_reading("RA", "A", "primary", HOUR, NEXT, 60.0))
    before = client.get(f"/points/X/hours/{HOUR}").json()

    client.put("/corrections/SW1", json=_corr("A", "B", HOUR, NEXT))
    after = client.get(f"/points/X/hours/{HOUR}").json()
    assert after == before
    # A 的主表被换到 B，A 该小时无覆盖 -> 404；B 计 60 dB。
    assert client.get(f"/points/A/hours/{HOUR}").status_code == 404
    assert abs(client.get(f"/points/B/hours/{HOUR}").json()["leq_db"] - 60.0) < 1e-9


def test_correction_window_spanning_multiple_hours(client):
    # 更正窗口跨小时：窗口内每个小时都换归属，窗口外的小时保持原归属。
    client.put(
        "/readings/RA",
        json=_reading("RA", "A", "primary", HOUR, "2026-11-21T11:00:00Z", 60.0),
    )
    client.put(
        "/readings/RB",
        json=_reading("RB", "B", "primary", HOUR, "2026-11-21T11:00:00Z", 80.0),
    )
    # 窗口只覆盖 08:30 - 10:00，跨 08、09 两个小时。
    client.put("/corrections/SW1", json=_corr("A", "B", T30, HOUR2))

    # 08 点：双方各半小时本点、半小时换回，能量组成相同。
    expected_h1 = 10 * math.log10((1800 * 10 ** 6.0 + 1800 * 10 ** 8.0) / 3600)
    assert abs(client.get(f"/points/A/hours/{HOUR}").json()["leq_db"] - expected_h1) < 1e-9
    assert abs(client.get(f"/points/B/hours/{HOUR}").json()["leq_db"] - expected_h1) < 1e-9
    # 09 点整小时都在窗口内：完全互换。
    assert abs(client.get(f"/points/A/hours/{NEXT}").json()["leq_db"] - 80.0) < 1e-9
    assert abs(client.get(f"/points/B/hours/{NEXT}").json()["leq_db"] - 60.0) < 1e-9
    # 10 点在窗口外：保持登记测点。
    assert abs(client.get(f"/points/A/hours/{HOUR2}").json()["leq_db"] - 60.0) < 1e-9
    assert abs(client.get(f"/points/B/hours/{HOUR2}").json()["leq_db"] - 80.0) < 1e-9
