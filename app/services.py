"""领域服务：登记、校准、撤回、接反更正与小时等效声级重算。

所有写函数都要求调用方已持有写事务（BEGIN IMMEDIATE），
原始数据变更与小时结果重算在同一事务内提交或一起回滚。
"""
from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .schemas import CalibrationIn, CorrectionIn, ReadingIn, Role
from .time_utils import SECONDS_PER_HOUR, format_epoch_second, hours_touching

# dB 转声能时的参考基准，公共因子在平均后会抵消。
_ENERGY_REF_FACTOR = 0.1


class ServiceError(Exception):
    """服务层业务错误基类。"""


class NotFoundError(ServiceError):
    """编号不存在。"""


class ConflictError(ServiceError):
    """编号冲突 / 区间重叠 / 状态不允许变更。"""


class ValidationError(ServiceError):
    """入参语义不合法。"""


@dataclass(frozen=True)
class Calibration:
    number: str
    point: str
    role: str
    start_ts: int
    end_ts: int
    offset_db: float
    withdrawn: bool


@dataclass(frozen=True)
class Correction:
    """主表通道接反更正：区间内 point_a / point_b 的主表读数互换归属。"""

    number: str
    point_a: str
    point_b: str
    start_ts: int
    end_ts: int


# --------------------------------------------------------------------------- #
# 行序列化
# --------------------------------------------------------------------------- #
def reading_out(row: sqlite3.Row) -> dict:
    return {
        "number": row["number"],
        "point": row["point"],
        "role": Role(row["role"]),
        "start": format_epoch_second(row["start_ts"]),
        "end": format_epoch_second(row["end_ts"]),
        "db": row["db_value"],
    }


def calibration_out(row: sqlite3.Row) -> dict:
    return {
        "number": row["number"],
        "point": row["point"],
        "role": Role(row["role"]),
        "start": format_epoch_second(row["start_ts"]),
        "end": format_epoch_second(row["end_ts"]),
        "offset": row["offset_db"],
        "withdrawn": bool(row["withdrawn"]),
    }


def correction_out(row: sqlite3.Row) -> dict:
    return {
        "number": row["number"],
        "point_a": row["point_a"],
        "point_b": row["point_b"],
        "start": format_epoch_second(row["start_ts"]),
        "end": format_epoch_second(row["end_ts"]),
    }


# --------------------------------------------------------------------------- #
# 通用校验
# --------------------------------------------------------------------------- #
def _validate_interval(start_ts: int, end_ts: int) -> None:
    if start_ts >= end_ts:
        raise ValidationError("区间必须满足 start < end，且为左闭右开的整秒区间")


def _find_reading_overlap(
    conn: sqlite3.Connection,
    point: str,
    role: Role,
    start_ts: int,
    end_ts: int,
    exclude_number: str | None = None,
) -> str | None:
    """返回与给定区间重叠的已有读数编号，没有则返回 None。"""
    sql = (
        "SELECT number FROM readings "
        "WHERE point = ? AND role = ? AND start_ts < ? AND end_ts > ?"
    )
    params: list[object] = [point, role.value, end_ts, start_ts]
    if exclude_number is not None:
        sql += " AND number != ?"
        params.append(exclude_number)
    row = conn.execute(sql, params).fetchone()
    return row["number"] if row else None


def _find_calibration_overlap(
    conn: sqlite3.Connection,
    point: str,
    role: Role,
    start_ts: int,
    end_ts: int,
    exclude_number: str | None = None,
) -> str | None:
    """返回与给定区间重叠的有效（未撤回）校准单编号。"""
    sql = (
        "SELECT number FROM calibrations "
        "WHERE point = ? AND role = ? AND withdrawn = 0 "
        "AND start_ts < ? AND end_ts > ?"
    )
    params: list[object] = [point, role.value, end_ts, start_ts]
    if exclude_number is not None:
        sql += " AND number != ?"
        params.append(exclude_number)
    row = conn.execute(sql, params).fetchone()
    return row["number"] if row else None


# --------------------------------------------------------------------------- #
# 读数登记
# --------------------------------------------------------------------------- #
def register_reading(conn: sqlite3.Connection, number: str, payload: ReadingIn) -> tuple[dict, bool]:
    """登记读数。

    返回 (响应体, 是否新建)。同号原样重传视为幂等成功；同号内容不同则冲突。
    读数可补交（任意历史时刻），没有时效限制。
    """
    _validate_interval(payload.start, payload.end)
    existing = conn.execute(
        "SELECT * FROM readings WHERE number = ?", (number,)
    ).fetchone()
    now = int(time.time())

    if existing is not None:
        same = (
            existing["point"] == payload.point
            and existing["role"] == payload.role.value
            and existing["start_ts"] == payload.start
            and existing["end_ts"] == payload.end
            and float(existing["db_value"]) == float(payload.db)
        )
        if not same:
            raise ConflictError(
                f"读数编号 {number} 已存在且内容不同；同号只接受原样重传"
            )
        return reading_out(existing), False

    overlap = _find_reading_overlap(
        conn, payload.point, payload.role, payload.start, payload.end
    )
    if overlap is not None:
        raise ConflictError(
            f"与同仪器读数 {overlap} 的区间重叠，同一仪器读数区间不得重叠"
        )

    conn.execute(
        "INSERT INTO readings (number, point, role, start_ts, end_ts, db_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            number,
            payload.point,
            payload.role.value,
            payload.start,
            payload.end,
            payload.db,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM readings WHERE number = ?", (number,)).fetchone()
    recompute_affected(conn, _reading_affected_hours(conn, payload))
    return reading_out(row), True


def _reading_affected_hours(
    conn: sqlite3.Connection, payload: ReadingIn
) -> dict[str, set[int]]:
    """读数登记涉及的重算范围：本测点触及的小时，以及（仅主表）接反更正对方测点的小时。

    主表读数落在更正窗口内的部分归属对方测点，因此补交主表读数时，
    对方测点与窗口重叠的小时也要重算；备表读数按登记测点核算，不涉及对方。
    """
    affected: dict[str, set[int]] = {
        payload.point: set(hours_touching(payload.start, payload.end))
    }
    if payload.role == Role.PRIMARY:
        for corr in _corrections_for_point(conn, payload.point):
            lo = max(payload.start, corr.start_ts)
            hi = min(payload.end, corr.end_ts)
            if lo < hi:
                affected.setdefault(_peer_of(corr, payload.point), set()).update(
                    hours_touching(lo, hi)
                )
    return affected


# --------------------------------------------------------------------------- #
# 校准单登记 / 修改 / 撤回
# --------------------------------------------------------------------------- #
def upsert_calibration(
    conn: sqlite3.Connection, number: str, payload: CalibrationIn
) -> tuple[dict, bool]:
    """登记新校准单，或修改已有校准单的起止时刻与偏移。

    已撤回的编号不可复活，需使用新编号。
    """
    _validate_interval(payload.start, payload.end)
    existing = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    now = int(time.time())

    if existing is not None:
        if existing["withdrawn"]:
            raise ConflictError(f"校准单 {number} 已撤回，不能修改或复活，请使用新编号")
        if payload.point != existing["point"] or payload.role.value != existing["role"]:
            raise ConflictError(
                f"校准单 {number} 的测点与主备角色不可修改，只能修改起止时刻和偏移"
            )

        overlap = _find_calibration_overlap(
            conn,
            payload.point,
            payload.role,
            payload.start,
            payload.end,
            exclude_number=number,
        )
        if overlap is not None:
            raise ConflictError(
                f"与有效校准单 {overlap} 的区间重叠，同一仪器校准区间不得重叠"
            )

        conn.execute(
            "UPDATE calibrations SET point = ?, role = ?, start_ts = ?, end_ts = ?, "
            "offset_db = ?, updated_at = ? WHERE number = ?",
            (
                payload.point,
                payload.role.value,
                payload.start,
                payload.end,
                payload.offset,
                now,
                number,
            ),
        )
        affected = set(hours_touching(existing["start_ts"], existing["end_ts"]))
        affected.update(hours_touching(payload.start, payload.end))
        recompute_hours(conn, payload.point, sorted(affected))
        row = conn.execute(
            "SELECT * FROM calibrations WHERE number = ?", (number,)
        ).fetchone()
        return calibration_out(row), False

    overlap = _find_calibration_overlap(
        conn, payload.point, payload.role, payload.start, payload.end
    )
    if overlap is not None:
        raise ConflictError(
            f"与有效校准单 {overlap} 的区间重叠，同一仪器校准区间不得重叠"
        )

    conn.execute(
        "INSERT INTO calibrations (number, point, role, start_ts, end_ts, offset_db, "
        "withdrawn, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            number,
            payload.point,
            payload.role.value,
            payload.start,
            payload.end,
            payload.offset,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    recompute_hours(conn, payload.point, hours_touching(payload.start, payload.end))
    return calibration_out(row), True


def withdraw_calibration(conn: sqlite3.Connection, number: str) -> dict:
    """撤回校准单（软删除）。撤回后其偏移不再参与任何小时计算。"""
    existing = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    if existing is None:
        raise NotFoundError(f"校准单 {number} 不存在")
    if existing["withdrawn"]:
        # 幂等：重复撤回直接返回当前状态。
        return calibration_out(existing)

    now = int(time.time())
    conn.execute(
        "UPDATE calibrations SET withdrawn = 1, updated_at = ? WHERE number = ?",
        (now, number),
    )
    row = conn.execute(
        "SELECT * FROM calibrations WHERE number = ?", (number,)
    ).fetchone()
    recompute_hours(
        conn, existing["point"], hours_touching(existing["start_ts"], existing["end_ts"])
    )
    return calibration_out(row)


# --------------------------------------------------------------------------- #
# 主表通道接反更正
# --------------------------------------------------------------------------- #
def _corrections_for_point(conn: sqlite3.Connection, point: str) -> list[Correction]:
    """该测点作为任一端参与的全部更正，按起点排序（同一测点的时段互不重叠）。"""
    rows = conn.execute(
        "SELECT * FROM corrections WHERE point_a = ? OR point_b = ? ORDER BY start_ts",
        (point, point),
    ).fetchall()
    return [
        Correction(
            number=r["number"],
            point_a=r["point_a"],
            point_b=r["point_b"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
        )
        for r in rows
    ]


def _peer_of(correction: Correction, point: str) -> str:
    """更正中与给定测点配对的另一方。"""
    return correction.point_b if correction.point_a == point else correction.point_a


def register_correction(
    conn: sqlite3.Connection, number: str, payload: CorrectionIn
) -> tuple[dict, bool]:
    """登记主表通道接反更正。

    区间内两个测点的主表读数互换归属，校准偏移按换回后的实际测点叠加；
    备表读数与校准单仍按登记测点核算。更正可在读数补交前登记。
    同一测点的更正时段允许相邻、不得重叠；同号同内容重传幂等，内容不同冲突。
    """
    _validate_interval(payload.start, payload.end)
    if payload.point_a == payload.point_b:
        raise ValidationError("接反更正的两个测点必须不同")
    # 测点对无方向：按字典序归一化后存储与比较。
    point_a, point_b = sorted((payload.point_a, payload.point_b))

    existing = conn.execute(
        "SELECT * FROM corrections WHERE number = ?", (number,)
    ).fetchone()
    if existing is not None:
        same = (
            existing["point_a"] == point_a
            and existing["point_b"] == point_b
            and existing["start_ts"] == payload.start
            and existing["end_ts"] == payload.end
        )
        if not same:
            raise ConflictError(
                f"更正编号 {number} 已存在且内容不同；同号只接受原样重传"
            )
        return correction_out(existing), False

    overlap = conn.execute(
        "SELECT number FROM corrections "
        "WHERE (point_a IN (?, ?) OR point_b IN (?, ?)) "
        "AND start_ts < ? AND end_ts > ?",
        (point_a, point_b, point_a, point_b, payload.end, payload.start),
    ).fetchone()
    if overlap is not None:
        raise ConflictError(
            f"与更正 {overlap['number']} 的时段重叠，同一测点的更正时段不得重叠"
        )

    now = int(time.time())
    conn.execute(
        "INSERT INTO corrections (number, point_a, point_b, start_ts, end_ts, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (number, point_a, point_b, payload.start, payload.end, now),
    )
    row = conn.execute(
        "SELECT * FROM corrections WHERE number = ?", (number,)
    ).fetchone()
    # 更正改变两个测点在窗口内的主表归属，双方触及的小时都要重算。
    hours = set(hours_touching(payload.start, payload.end))
    recompute_affected(conn, {point_a: hours, point_b: set(hours)})
    return correction_out(row), True


# --------------------------------------------------------------------------- #
# 小时等效声级计算
# --------------------------------------------------------------------------- #
def _active_calibrations(
    conn: sqlite3.Connection, point: str, role: str
) -> list[Calibration]:
    rows = conn.execute(
        "SELECT * FROM calibrations WHERE point = ? AND role = ? AND withdrawn = 0 "
        "ORDER BY start_ts",
        (point, role),
    ).fetchall()
    return [
        Calibration(
            number=r["number"],
            point=r["point"],
            role=r["role"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            offset_db=r["offset_db"],
            withdrawn=False,
        )
        for r in rows
    ]


def _apply_calibrations(
    start_ts: int, end_ts: int, db_value: float, calibrations: list[Calibration]
) -> list[tuple[int, int, float]]:
    """把 [start_ts, end_ts) 段按校准区间切成若干 (起点, 终点, 叠加偏移后的分贝) 片段。

    校准单同仪器互不重叠，因此任一秒最多命中一个偏移；未命中校准的片段偏移为 0。
    """
    segments: list[tuple[int, int, float]] = []
    cursor = start_ts
    for cal in calibrations:
        if cal.end_ts <= cursor:
            continue
        if cal.start_ts >= end_ts:
            break
        overlap_start = max(cursor, cal.start_ts)
        overlap_end = min(end_ts, cal.end_ts)
        if overlap_start < overlap_end:
            if cursor < overlap_start:
                segments.append((cursor, overlap_start, db_value))
            segments.append(
                (overlap_start, overlap_end, db_value + cal.offset_db)
            )
            cursor = overlap_end
    if cursor < end_ts:
        segments.append((cursor, end_ts, db_value))
    return segments


def _subtract_windows(
    start_ts: int, end_ts: int, windows: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从 [start_ts, end_ts) 中扣除所有窗口，返回剩余的左闭右开片段。"""
    remaining = [(start_ts, end_ts)]
    for w_start, w_end in windows:
        kept: list[tuple[int, int]] = []
        for seg_start, seg_end in remaining:
            if w_end <= seg_start or w_start >= seg_end:
                kept.append((seg_start, seg_end))
                continue
            if seg_start < w_start:
                kept.append((seg_start, w_start))
            if w_end < seg_end:
                kept.append((w_end, seg_end))
        remaining = kept
    return remaining


def _primary_segments(
    conn: sqlite3.Connection, point: str, hour_start: int, hour_end: int
) -> list[tuple[int, int, float]]:
    """某测点某小时实际归属的主表读数片段 (起点, 终点, 分贝)。

    * 本测点登记的主表读数，扣除落在接反更正窗口内的部分（跨越边界的读数分段归属）；
    * 对方测点登记的主表读数，落在更正窗口内的部分换回本测点。

    同一测点的更正时段互不重叠、同仪器读数互不重叠，因此返回的片段互不重叠。
    """
    corrections = _corrections_for_point(conn, point)
    windows = [(c.start_ts, c.end_ts) for c in corrections]

    segments: list[tuple[int, int, float]] = []
    rows = conn.execute(
        "SELECT * FROM readings WHERE point = ? AND role = ? "
        "AND start_ts < ? AND end_ts > ?",
        (point, Role.PRIMARY.value, hour_end, hour_start),
    ).fetchall()
    for row in rows:
        for seg_start, seg_end in _subtract_windows(
            row["start_ts"], row["end_ts"], windows
        ):
            segments.append((seg_start, seg_end, row["db_value"]))

    for corr in corrections:
        lo = max(corr.start_ts, hour_start)
        hi = min(corr.end_ts, hour_end)
        if lo >= hi:
            continue
        peer_rows = conn.execute(
            "SELECT * FROM readings WHERE point = ? AND role = ? "
            "AND start_ts < ? AND end_ts > ?",
            (_peer_of(corr, point), Role.PRIMARY.value, hi, lo),
        ).fetchall()
        for row in peer_rows:
            seg_start = max(row["start_ts"], corr.start_ts)
            seg_end = min(row["end_ts"], corr.end_ts)
            if seg_start < seg_end:
                segments.append((seg_start, seg_end, row["db_value"]))
    return segments


def _compute_hour(
    conn: sqlite3.Connection, point: str, hour_start: int
) -> dict | None:
    """计算单个测点单个 UTC 小时的结果；无任何覆盖返回 None。"""
    hour_end = hour_start + SECONDS_PER_HOUR

    primary_segments = _primary_segments(conn, point, hour_start, hour_end)
    backup_rows = conn.execute(
        "SELECT * FROM readings WHERE point = ? AND role = ? "
        "AND start_ts < ? AND end_ts > ?",
        (point, Role.BACKUP.value, hour_end, hour_start),
    ).fetchall()
    if not primary_segments and not backup_rows:
        return None

    # 校准偏移按实际归属测点取：换回本测点的主表片段同样叠加本测点的校准单。
    calibrations = {
        Role.PRIMARY.value: _active_calibrations(conn, point, Role.PRIMARY.value),
        Role.BACKUP.value: _active_calibrations(conn, point, Role.BACKUP.value),
    }

    # chosen_energy[秒在小时内的偏移] = 该秒计入的声能；-1 表示尚未覆盖。
    chosen_energy: list[float] = [-1.0] * SECONDS_PER_HOUR
    chosen_role: list[int] = [0] * SECONDS_PER_HOUR  # 1=primary, 2=backup

    sources: list[tuple[int, str, list[tuple[int, int, float]]]] = [
        (1, Role.PRIMARY.value, primary_segments),
        (
            2,
            Role.BACKUP.value,
            [(r["start_ts"], r["end_ts"], r["db_value"]) for r in backup_rows],
        ),
    ]
    for role_rank, role_key, segments in sources:
        for seg_start, seg_end, db_value in segments:
            for adj_start, adj_end, adjusted_db in _apply_calibrations(
                seg_start, seg_end, db_value, calibrations[role_key]
            ):
                lo = max(adj_start, hour_start)
                hi = min(adj_end, hour_end)
                energy = 10.0 ** (_ENERGY_REF_FACTOR * adjusted_db)
                for sec in range(lo - hour_start, hi - hour_start):
                    # 主表优先：主表已覆盖的秒不再被备表替换；备表只补缺口。
                    if chosen_energy[sec] < 0:
                        chosen_energy[sec] = energy
                        chosen_role[sec] = role_rank
                    elif chosen_role[sec] > role_rank:
                        chosen_energy[sec] = energy
                        chosen_role[sec] = role_rank

    covered = [e for e in chosen_energy if e >= 0.0]
    if not covered:
        return None

    primary_seconds = sum(1 for r in chosen_role if r == 1)
    backup_seconds = sum(1 for r in chosen_role if r == 2)
    mean_energy = sum(covered) / len(covered)
    leq_db = 10.0 * math.log10(mean_energy)

    return {
        "point": point,
        "hour_start": hour_start,
        "leq_db": leq_db,
        "covered_seconds": len(covered),
        "primary_seconds": primary_seconds,
        "backup_seconds": backup_seconds,
    }


def recompute_hours(
    conn: sqlite3.Connection, point: str, hour_starts: list[int] | range
) -> None:
    """重算指定测点受影响的小时，并以新结果替换旧结果（无覆盖则删除旧行）。"""
    now = int(time.time())
    for hour_start in hour_starts:
        conn.execute(
            "DELETE FROM hourly_results WHERE point = ? AND hour_start = ?",
            (point, hour_start),
        )
        result = _compute_hour(conn, point, hour_start)
        if result is not None:
            conn.execute(
                "INSERT INTO hourly_results (point, hour_start, leq_db, covered_seconds, "
                "primary_seconds, backup_seconds, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result["point"],
                    result["hour_start"],
                    result["leq_db"],
                    result["covered_seconds"],
                    result["primary_seconds"],
                    result["backup_seconds"],
                    now,
                ),
            )


def recompute_affected(
    conn: sqlite3.Connection, affected: dict[str, Iterable[int]]
) -> None:
    """按「测点 -> 小时集合」重算所有受影响的测点与小时，其余保持原状。"""
    for point, hours in affected.items():
        recompute_hours(conn, point, sorted(set(hours)))


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def get_hour_result(
    conn: sqlite3.Connection, point: str, hour_start: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM hourly_results WHERE point = ? AND hour_start = ?",
        (point, hour_start),
    ).fetchone()
    if row is None:
        return None
    return {
        "point": row["point"],
        "hour_start": row["hour_start"],
        "leq_db": row["leq_db"],
        "covered_seconds": row["covered_seconds"],
        "primary_seconds": row["primary_seconds"],
        "backup_seconds": row["backup_seconds"],
    }


def list_hour_results(
    conn: sqlite3.Connection, point: str, start_hour: int, end_hour: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM hourly_results WHERE point = ? AND hour_start >= ? AND hour_start < ? "
        "ORDER BY hour_start",
        (point, start_hour, end_hour),
    ).fetchall()
    return [
        {
            "point": r["point"],
            "hour_start": r["hour_start"],
            "leq_db": r["leq_db"],
            "covered_seconds": r["covered_seconds"],
            "primary_seconds": r["primary_seconds"],
            "backup_seconds": r["backup_seconds"],
        }
        for r in rows
    ]


def list_readings(conn: sqlite3.Connection, point: str | None) -> list[dict]:
    if point is None:
        rows = conn.execute("SELECT * FROM readings ORDER BY point, start_ts").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings WHERE point = ? ORDER BY start_ts", (point,)
        ).fetchall()
    return [reading_out(r) for r in rows]


def list_calibrations(conn: sqlite3.Connection, point: str | None) -> list[dict]:
    if point is None:
        rows = conn.execute(
            "SELECT * FROM calibrations ORDER BY point, start_ts"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calibrations WHERE point = ? ORDER BY start_ts", (point,)
        ).fetchall()
    return [calibration_out(r) for r in rows]


def list_corrections(conn: sqlite3.Connection, point: str | None) -> list[dict]:
    if point is None:
        rows = conn.execute(
            "SELECT * FROM corrections ORDER BY point_a, point_b, start_ts"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM corrections WHERE point_a = ? OR point_b = ? "
            "ORDER BY start_ts",
            (point, point),
        ).fetchall()
    return [correction_out(r) for r in rows]
