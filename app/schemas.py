"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .time_utils import parse_epoch_second


class Role(str, Enum):
    """仪器主备角色。"""

    PRIMARY = "primary"
    BACKUP = "backup"


class IntervalTimesIn(BaseModel):
    """带左闭右开 UTC 整秒区间的请求基类。"""

    model_config = ConfigDict(extra="forbid")

    start: Annotated[object, Field(description="区间起点，UTC 整秒（闭）")]
    end: Annotated[object, Field(description="区间终点，UTC 整秒（开）")]

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: object) -> int:
        # 校验阶段即转成纪元秒，服务层只处理整数。
        return parse_epoch_second(value)


class IntervalIn(IntervalTimesIn):
    """读数 / 校准单登记的共同字段。"""

    point: Annotated[str, Field(min_length=1, max_length=128, description="测点编号")]
    role: Role = Field(description="主表 primary / 备表 backup")


class ReadingIn(IntervalIn):
    """读数登记请求；区间内声级恒定。"""

    db: Annotated[float, Field(description="区间内恒定分贝值 dB(A)")]


class CalibrationIn(IntervalIn):
    """校准单登记 / 修改请求。"""

    offset: Annotated[float, Field(description="校准偏移 dB，叠加到读数分贝上")]


class CorrectionIn(IntervalTimesIn):
    """主表通道接反更正登记请求。

    区间内两个测点的主表读数互换归属；测点对无方向，存储时按字典序归一化。
    """

    point_a: Annotated[str, Field(min_length=1, max_length=128, description="测点 A")]
    point_b: Annotated[str, Field(min_length=1, max_length=128, description="测点 B")]


class ReadingOut(BaseModel):
    number: str
    point: str
    role: Role
    start: str
    end: str
    db: float


class CalibrationOut(BaseModel):
    number: str
    point: str
    role: Role
    start: str
    end: str
    offset: float
    withdrawn: bool


class CorrectionOut(BaseModel):
    number: str
    point_a: str
    point_b: str
    start: str
    end: str


class HourlyResultOut(BaseModel):
    point: str
    hour: str
    leq_db: float
    covered_seconds: int
    primary_seconds: int
    backup_seconds: int


class HourlyRangeOut(BaseModel):
    point: str
    start_hour: str
    end_hour: str
    hours: list[HourlyResultOut]


class MessageOut(BaseModel):
    detail: str
