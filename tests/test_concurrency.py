"""并发写入：成功的变更全部计入；失败时原数据与小时结果保持不变。"""
from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import database, services
from app.schemas import CalibrationIn, ReadingIn
from app.time_utils import parse_epoch_second

HOUR = "2026-11-21T08:00:00Z"
NEXT = "2026-11-21T09:00:00Z"
HALF = "2026-11-21T08:30:00Z"


@pytest.fixture()
def db_path() -> str:
    tmpdir = tempfile.mkdtemp(prefix="noise-conc-")
    path = os.path.join(tmpdir, "conc.db")
    database.init_db(path)
    return path


def _reading(point, role, start, end, db) -> ReadingIn:
    return ReadingIn(
        point=point,
        role=role,
        start=parse_epoch_second(start),
        end=parse_epoch_second(end),
        db=db,
    )


def _calibration(point, role, start, end, offset) -> CalibrationIn:
    return CalibrationIn(
        point=point,
        role=role,
        start=parse_epoch_second(start),
        end=parse_epoch_second(end),
        offset=offset,
    )


def test_concurrent_non_overlapping_readings_all_commit(db_path):
    # 10 个线程各写一条互不重叠的读数到同一仪器，全部应成功。
    def write_one(idx: int) -> str:
        conn = database.connect(db_path)
        try:
            start = f"2026-11-21T08:{idx * 5:02d}:00Z"
            end = f"2026-11-21T08:{idx * 5 + 5:02d}:00Z"
            with database.write_transaction(conn):
                services.register_reading(
                    conn, f"R{idx}", _reading("P1", "primary", start, end, 60.0)
                )
            return "ok"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(write_one, range(10)))

    assert outcomes == ["ok"] * 10
    conn = database.connect(db_path)
    try:
        rows = conn.execute("SELECT number FROM readings").fetchall()
        assert len(rows) == 10
        result = services.get_hour_result(
            conn, "P1", parse_epoch_second(HOUR)
        )
        assert result is not None
        assert result["covered_seconds"] == 3000  # 10 * 5 分钟
        assert result["primary_seconds"] == 3000
    finally:
        conn.close()


def test_concurrent_overlapping_writes_one_wins_other_keeps_old(db_path):
    # 先落一条基线读数（小时的前 15 分钟）；两个线程并发登记彼此重叠的新读数，
    # 只能有一个成功（另一个 409），且任何情况下基线与小时结果都不被破坏。
    conn = database.connect(db_path)
    with database.write_transaction(conn):
        services.register_reading(
            conn,
            "BASE",
            _reading("P1", "primary", HOUR, "2026-11-21T08:15:00Z", 60.0),
        )
    conn.close()

    barrier = threading.Barrier(2)

    def write_overlap(number: str) -> tuple[str, str]:
        conn = database.connect(db_path)
        try:
            barrier.wait()
            with database.write_transaction(conn):
                try:
                    if number == "X1":
                        # 与 BASE 相邻不重叠，但与 X2 重叠。
                        payload = _reading("P1", "primary", "2026-11-21T08:15:00Z", HALF, 90.0)
                    else:
                        # 起点 08:20 落入 X1 的 [08:15, 08:30)，二者互斥。
                        payload = _reading("P1", "primary", "2026-11-21T08:20:00Z", NEXT, 80.0)
                    services.register_reading(conn, number, payload)
                    return number, "committed"
                except services.ConflictError:
                    raise
        except services.ConflictError:
            return number, "conflict"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write_overlap, ["X1", "X2"]))

    statuses = sorted(status for _, status in results)
    assert statuses == ["committed", "conflict"]

    conn = database.connect(db_path)
    try:
        rows = conn.execute("SELECT number FROM readings ORDER BY number").fetchall()
        numbers = [r["number"] for r in rows]
        assert "BASE" in numbers
        assert "X1" in numbers or "X2" in numbers
        assert len(numbers) == 2
        # 小时结果仍可查询：BASE 900 秒 + 获胜者区间（X1 900 秒或 X2 2400 秒）。
        result = services.get_hour_result(conn, "P1", parse_epoch_second(HOUR))
        assert result is not None
        assert result["covered_seconds"] in (1800, 3300)
        assert result["primary_seconds"] == result["covered_seconds"]
    finally:
        conn.close()


def test_failed_validation_rolls_back_everything(db_path):
    conn = database.connect(db_path)
    # 先登记一条合法读数。
    with database.write_transaction(conn):
        services.register_reading(
            conn, "R1", _reading("P1", "primary", HOUR, HALF, 60.0)
        )

    # 同一事务里再制造重叠冲突：整体回滚，R1 之外不留任何痕迹。
    with pytest.raises(services.ConflictError):
        with database.write_transaction(conn):
            services.register_reading(
                conn, "R2", _reading("P1", "primary", HOUR, HALF, 70.0)
            )
    conn.close()

    conn = database.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 1
        result = services.get_hour_result(conn, "P1", parse_epoch_second(HOUR))
        assert result is not None
        assert abs(result["leq_db"] - 60.0) < 1e-9
        assert result["covered_seconds"] == 1800
    finally:
        conn.close()


def test_concurrent_calibration_and_reading_both_counted(db_path):
    # 读数与校准单并发写入同一小时，提交后小时结果必须同时反映两者。
    barrier = threading.Barrier(2)

    def write_reading() -> None:
        conn = database.connect(db_path)
        try:
            barrier.wait()
            with database.write_transaction(conn):
                services.register_reading(
                    conn, "R1", _reading("P1", "primary", HOUR, NEXT, 60.0)
                )
        finally:
            conn.close()

    def write_calibration() -> None:
        conn = database.connect(db_path)
        try:
            barrier.wait()
            with database.write_transaction(conn):
                services.upsert_calibration(
                    conn,
                    "C1",
                    _calibration("P1", "primary", HOUR, NEXT, 5.0),
                )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [write_reading, write_calibration]))

    conn = database.connect(db_path)
    try:
        result = services.get_hour_result(conn, "P1", parse_epoch_second(HOUR))
        assert result is not None
        # 两个写事务都成功；后提交者会重算，最终结果含 +5 偏移 = 65 dB。
        assert abs(result["leq_db"] - 65.0) < 1e-9
        assert result["covered_seconds"] == 3600
    finally:
        conn.close()
