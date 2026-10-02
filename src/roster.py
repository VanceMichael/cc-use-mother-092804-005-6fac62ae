"""赛事服务中心的排班、签到、替班与表彰记录链。

报名、语言能力、岗位培训、班次需求、签到、替班和奖励都追加在
同一条记录链上；系统恢复后重放记录链即可继续处理未审核的替班
和即将开始的班次，并回答一份表彰为何包含或排除了某段服务。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

DEFAULT_REST_INTERVAL = timedelta(hours=8)

# 班次状态
SHIFT_SCHEDULED = "scheduled"
SHIFT_STARTED = "started"
SHIFT_CLOSED = "closed"
SHIFT_CANCELLED = "cancelled"

# 名额状态
ASSIGNMENT_ACTIVE = "active"
ASSIGNMENT_RELEASED = "released"
ASSIGNMENT_SUBSTITUTED = "substituted"

# 替班申请状态
SUBSTITUTION_PENDING = "pending"
SUBSTITUTION_APPLIED = "applied"
SUBSTITUTION_REJECTED = "rejected"

# 表彰排除原因
REASON_SHIFT_CANCELLED = "SHIFT_CANCELLED"
REASON_ASSIGNMENT_RELEASED = "ASSIGNMENT_RELEASED"
REASON_SUBSTITUTED = "SUBSTITUTED"
REASON_NO_CHECKIN = "NO_CHECKIN"
REASON_SHIFT_NOT_CLOSED = "SHIFT_NOT_CLOSED"
REASON_OUTSIDE_PERIOD = "OUTSIDE_PERIOD"

REASON_TEXT = {
    REASON_SHIFT_CANCELLED: "班次已取消，名额已释放",
    REASON_ASSIGNMENT_RELEASED: "名额已释放",
    REASON_SUBSTITUTED: "已由他人替班",
    REASON_NO_CHECKIN: "缺少签到记录",
    REASON_SHIFT_NOT_CLOSED: "班次尚未完成",
    REASON_OUTSIDE_PERIOD: "不在表彰周期内",
}


class RosterError(ValueError):
    """违反排班、替班或表彰约束。"""


@dataclass(frozen=True)
class TimeWindow:
    """班次或表彰的起止时段。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("时段结束必须晚于开始")

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: TimeWindow) -> TimeWindow | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end <= start:
            return None
        return TimeWindow(start, end)

    def gap_to(self, other: TimeWindow) -> timedelta:
        if self.overlaps(other):
            return timedelta(0)
        if self.end <= other.start:
            return other.start - self.end
        return self.start - other.end

    def to_record(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_record(cls, data: Mapping[str, str]) -> TimeWindow:
        return cls(datetime.fromisoformat(data["start"]), datetime.fromisoformat(data["end"]))


@dataclass
class Volunteer:
    volunteer_id: str
    name: str
    languages: dict[str, str] = field(default_factory=dict)
    qualifications: set[str] = field(default_factory=set)


@dataclass
class Position:
    position_id: str
    name: str
    venue: str
    capacity: int
    required_qualification: str
    required_languages: frozenset[str] = frozenset()


@dataclass
class CheckIn:
    volunteer_id: str
    at: datetime


@dataclass
class ShiftLogEntry:
    kind: str  # "anomaly" 异常 | "handover" 交接
    author: str
    detail: str
    at: datetime


@dataclass
class Shift:
    shift_id: str
    position_id: str
    window: TimeWindow
    arrangement_id: str | None = None
    state: str = SHIFT_SCHEDULED
    checkins: dict[str, CheckIn] = field(default_factory=dict)
    log: list[ShiftLogEntry] = field(default_factory=list)


@dataclass
class Assignment:
    shift_id: str
    volunteer_id: str
    source: str = "normal"  # "normal" 排班 | "substitute" 替班
    state: str = ASSIGNMENT_ACTIVE


@dataclass
class Substitution:
    request_id: str
    shift_id: str
    original_id: str
    substitute_id: str
    reason: str
    requested_at: datetime
    state: str = SUBSTITUTION_PENDING
    owner_confirmed_at: datetime | None = None
    supervisor_confirmed_by: str | None = None
    supervisor_confirmed_at: datetime | None = None
    decided_reason: str | None = None


@dataclass(frozen=True)
class ServiceSegment:
    """一段计入表彰的服务。"""

    shift_id: str
    position: str
    window: TimeWindow

    @property
    def hours(self) -> float:
        return (self.window.end - self.window.start).total_seconds() / 3600

    def to_record(self) -> dict[str, Any]:
        return {"shift_id": self.shift_id, "position": self.position, "window": self.window.to_record()}

    @classmethod
    def from_record(cls, data: Mapping[str, Any]) -> ServiceSegment:
        return cls(data["shift_id"], data["position"], TimeWindow.from_record(data["window"]))


@dataclass(frozen=True)
class Exclusion:
    """一段被排除的服务及其原因。"""

    shift_id: str
    position: str
    reason: str

    @property
    def detail(self) -> str:
        return REASON_TEXT[self.reason]

    def to_record(self) -> dict[str, str]:
        return {"shift_id": self.shift_id, "position": self.position, "reason": self.reason}

    @classmethod
    def from_record(cls, data: Mapping[str, str]) -> Exclusion:
        return cls(data["shift_id"], data["position"], data["reason"])


@dataclass
class ServiceAssessment:
    volunteer_id: str
    period: TimeWindow
    included: list[ServiceSegment]
    excluded: list[Exclusion]

    @property
    def total_hours(self) -> float:
        return sum(segment.hours for segment in self.included)


@dataclass
class Commendation:
    commendation_id: str
    volunteer_id: str
    period: TimeWindow
    approved_by: str
    granted_at: datetime
    included: list[ServiceSegment]
    excluded: list[Exclusion]


class Roster:
    """赛事服务中心的记录链：所有事实只追加，可重放恢复。"""

    def __init__(self, rest_interval: timedelta = DEFAULT_REST_INTERVAL) -> None:
        if rest_interval < timedelta(0):
            raise ValueError("休息间隔不能为负")
        self.rest_interval = rest_interval
        self.records: list[dict[str, Any]] = []
        self.volunteers: dict[str, Volunteer] = {}
        self.positions: dict[str, Position] = {}
        self.shifts: dict[str, Shift] = {}
        self.assignments: dict[tuple[str, str], Assignment] = {}
        self.substitutions: dict[str, Substitution] = {}
        self.commendations: dict[str, Commendation] = {}
        self.supervisors: set[str] = set()
        self.reviewers: set[str] = set()

    # ---------- 报名、语言能力与岗位培训 ----------

    def register_volunteer(
        self,
        volunteer_id: str,
        name: str,
        *,
        languages: Mapping[str, str] | None = None,
        at: datetime,
    ) -> None:
        if volunteer_id in self.volunteers:
            raise RosterError("志愿者已报名")
        self._append(
            "volunteer_registered",
            at,
            {"volunteer_id": volunteer_id, "name": name, "languages": dict(languages or {})},
        )

    def certify_language(self, volunteer_id: str, language: str, level: str, *, at: datetime) -> None:
        self._volunteer(volunteer_id)
        if not language.strip() or not level.strip():
            raise RosterError("语言能力与等级不能为空")
        self._append(
            "language_certified",
            at,
            {"volunteer_id": volunteer_id, "language": language, "level": level},
        )

    def record_training(self, volunteer_id: str, qualification: str, *, at: datetime) -> None:
        self._volunteer(volunteer_id)
        if not qualification.strip():
            raise RosterError("岗位资质不能为空")
        self._append("training_recorded", at, {"volunteer_id": volunteer_id, "qualification": qualification})

    def register_supervisor(self, supervisor_id: str, name: str, *, at: datetime) -> None:
        if supervisor_id in self.supervisors:
            raise RosterError("主管已登记")
        self._append("supervisor_registered", at, {"supervisor_id": supervisor_id, "name": name})

    def register_reviewer(self, reviewer_id: str, name: str, *, at: datetime) -> None:
        if reviewer_id in self.reviewers:
            raise RosterError("表彰审核人员已登记")
        self._append("reviewer_registered", at, {"reviewer_id": reviewer_id, "name": name})

    # ---------- 岗位与班次需求 ----------

    def register_position(
        self,
        position_id: str,
        name: str,
        venue: str,
        capacity: int,
        required_qualification: str,
        required_languages: Iterable[str] = (),
        *,
        at: datetime,
    ) -> None:
        if position_id in self.positions:
            raise RosterError("岗位已存在")
        if capacity < 1:
            raise RosterError("场地人数必须至少为一")
        self._append(
            "position_registered",
            at,
            {
                "position_id": position_id,
                "name": name,
                "venue": venue,
                "capacity": capacity,
                "required_qualification": required_qualification,
                "required_languages": sorted(required_languages),
            },
        )

    def open_shift(
        self,
        shift_id: str,
        position_id: str,
        window: TimeWindow,
        arrangement_id: str | None = None,
        *,
        at: datetime,
    ) -> None:
        if position_id not in self.positions:
            raise RosterError("岗位不存在")
        if shift_id in self.shifts:
            raise RosterError("班次已存在")
        self._append(
            "shift_opened",
            at,
            {
                "shift_id": shift_id,
                "position_id": position_id,
                "window": window.to_record(),
                "arrangement_id": arrangement_id,
            },
        )

    # ---------- 排班 ----------

    def assign(self, shift_id: str, volunteer_id: str, *, at: datetime) -> None:
        shift = self._shift(shift_id)
        if shift.state != SHIFT_SCHEDULED:
            raise RosterError("班次不在可排班状态")
        self._validate_eligibility(shift, volunteer_id, freeing=None)
        self._append(
            "assignment_created",
            at,
            {"shift_id": shift_id, "volunteer_id": volunteer_id, "source": "normal"},
        )

    # ---------- 签到、异常与交接 ----------

    def record_checkin(self, shift_id: str, volunteer_id: str, *, at: datetime) -> None:
        shift = self._shift(shift_id)
        if shift.state == SHIFT_CANCELLED:
            raise RosterError("班次已取消")
        if shift.state != SHIFT_SCHEDULED:
            raise RosterError("已开始的班次不能回写签到事实")
        assignment = self.assignments.get((shift_id, volunteer_id))
        if assignment is None or assignment.state != ASSIGNMENT_ACTIVE:
            raise RosterError("志愿者不在该班次")
        if volunteer_id in shift.checkins:
            raise RosterError("签到记录已存在，不可改写")
        self._append(
            "checkin_recorded",
            at,
            {"shift_id": shift_id, "volunteer_id": volunteer_id, "checked_in_at": at.isoformat()},
        )

    def start_shift(self, shift_id: str, *, at: datetime) -> None:
        shift = self._shift(shift_id)
        if shift.state != SHIFT_SCHEDULED:
            raise RosterError("班次状态不允许开始")
        self._append("shift_started", at, {"shift_id": shift_id})

    def append_anomaly(self, shift_id: str, author: str, detail: str, *, at: datetime) -> None:
        self._append_log("anomaly_appended", shift_id, author, detail, at)

    def append_handover(self, shift_id: str, author: str, detail: str, *, at: datetime) -> None:
        self._append_log("handover_appended", shift_id, author, detail, at)

    def close_shift(self, shift_id: str, *, at: datetime) -> None:
        shift = self._shift(shift_id)
        if shift.state != SHIFT_STARTED:
            raise RosterError("班次状态不允许结束")
        self._append("shift_closed", at, {"shift_id": shift_id})

    # ---------- 替班 ----------

    def request_substitution(
        self,
        request_id: str,
        shift_id: str,
        original_id: str,
        substitute_id: str,
        reason: str,
        *,
        at: datetime,
    ) -> None:
        if request_id in self.substitutions:
            raise RosterError("替班申请编号重复")
        shift = self._shift(shift_id)
        if shift.state == SHIFT_STARTED:
            raise RosterError("已开始的班次只能追加异常和交接")
        if shift.state != SHIFT_SCHEDULED:
            raise RosterError("班次状态不允许替班")
        if original_id == substitute_id:
            raise RosterError("替班双方不能是同一人")
        assignment = self.assignments.get((shift_id, original_id))
        if assignment is None or assignment.state != ASSIGNMENT_ACTIVE:
            raise RosterError("原负责人不在该班次")
        self._validate_eligibility(shift, substitute_id, freeing=assignment)
        self._append(
            "substitution_requested",
            at,
            {
                "request_id": request_id,
                "shift_id": shift_id,
                "original_id": original_id,
                "substitute_id": substitute_id,
                "reason": reason,
                "requested_at": at.isoformat(),
            },
        )

    def confirm_substitution_by_owner(self, request_id: str, volunteer_id: str, *, at: datetime) -> None:
        sub = self._substitution(request_id)
        if sub.state != SUBSTITUTION_PENDING:
            raise RosterError("替班申请已处理")
        if volunteer_id != sub.original_id:
            raise RosterError("须由原负责人确认")
        if sub.owner_confirmed_at is not None:
            raise RosterError("原负责人已确认")
        self._append("substitution_owner_confirmed", at, {"request_id": request_id, "by": volunteer_id, "at": at.isoformat()})
        self._maybe_complete_substitution(request_id, at)

    def confirm_substitution_by_supervisor(self, request_id: str, supervisor_id: str, *, at: datetime) -> None:
        sub = self._substitution(request_id)
        if sub.state != SUBSTITUTION_PENDING:
            raise RosterError("替班申请已处理")
        if supervisor_id not in self.supervisors:
            raise RosterError("主管未登记")
        if supervisor_id in (sub.original_id, sub.substitute_id):
            raise RosterError("主管确认须独立于替班双方")
        if sub.supervisor_confirmed_at is not None:
            raise RosterError("主管已确认")
        self._append(
            "substitution_supervisor_confirmed",
            at,
            {"request_id": request_id, "by": supervisor_id, "at": at.isoformat()},
        )
        self._maybe_complete_substitution(request_id, at)

    # ---------- 安排取消 ----------

    def cancel_arrangement(self, arrangement_id: str, reason: str, *, at: datetime) -> None:
        """取消机场接送或酒店安排：只释放相关班次的名额，其他岗位保持不变。"""
        if not arrangement_id:
            raise RosterError("安排编号不能为空")
        targets = [
            shift
            for shift in self.shifts.values()
            if shift.arrangement_id == arrangement_id and shift.state == SHIFT_SCHEDULED
        ]
        if not targets:
            raise RosterError("未找到可取消的相关安排")
        for shift in targets:
            for assignment in list(self.assignments.values()):
                if assignment.shift_id == shift.shift_id and assignment.state == ASSIGNMENT_ACTIVE:
                    self._append(
                        "assignment_released",
                        at,
                        {
                            "shift_id": assignment.shift_id,
                            "volunteer_id": assignment.volunteer_id,
                            "state": ASSIGNMENT_RELEASED,
                            "reason": reason,
                        },
                    )
            self._append(
                "shift_cancelled",
                at,
                {"shift_id": shift.shift_id, "arrangement_id": arrangement_id, "reason": reason},
            )

    # ---------- 时长与表彰 ----------

    def assess_service(self, volunteer_id: str, period: TimeWindow) -> ServiceAssessment:
        """按记录链核算表彰周期内的服务：包含的时段与被排除的时段及原因。"""
        self._volunteer(volunteer_id)
        included: list[ServiceSegment] = []
        excluded: list[Exclusion] = []
        ordered = sorted(
            (a for a in self.assignments.values() if a.volunteer_id == volunteer_id),
            key=lambda a: (self.shifts[a.shift_id].window.start, a.shift_id),
        )
        for assignment in ordered:
            shift = self.shifts[assignment.shift_id]
            position = self.positions[shift.position_id]
            if shift.window.intersection(period) is None:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_OUTSIDE_PERIOD))
                continue
            if shift.state == SHIFT_CANCELLED:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_SHIFT_CANCELLED))
                continue
            if assignment.state == ASSIGNMENT_RELEASED:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_ASSIGNMENT_RELEASED))
                continue
            if assignment.state == ASSIGNMENT_SUBSTITUTED:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_SUBSTITUTED))
                continue
            if shift.state != SHIFT_CLOSED:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_SHIFT_NOT_CLOSED))
                continue
            checkin = shift.checkins.get(volunteer_id)
            served_from = shift.window.start if checkin is None else max(checkin.at, shift.window.start)
            if checkin is None or served_from >= shift.window.end:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_NO_CHECKIN))
                continue
            served = TimeWindow(served_from, shift.window.end).intersection(period)
            if served is None:
                excluded.append(Exclusion(shift.shift_id, position.name, REASON_OUTSIDE_PERIOD))
                continue
            included.append(ServiceSegment(shift.shift_id, position.name, served))
        return ServiceAssessment(volunteer_id, period, included, excluded)

    def grant_commendation(
        self,
        commendation_id: str,
        volunteer_id: str,
        period: TimeWindow,
        approved_by: str,
        *,
        at: datetime,
    ) -> None:
        if commendation_id in self.commendations:
            raise RosterError("表彰编号重复")
        if approved_by == volunteer_id:
            raise RosterError("任何人不能批准自己的奖励")
        if approved_by not in self.reviewers:
            raise RosterError("表彰审核人员未登记")
        self._volunteer(volunteer_id)
        assessment = self.assess_service(volunteer_id, period)
        self._append(
            "commendation_granted",
            at,
            {
                "commendation_id": commendation_id,
                "volunteer_id": volunteer_id,
                "approved_by": approved_by,
                "period": period.to_record(),
                "granted_at": at.isoformat(),
                "included": [segment.to_record() for segment in assessment.included],
                "excluded": [exclusion.to_record() for exclusion in assessment.excluded],
            },
        )

    def explain_commendation(self, commendation_id: str) -> str:
        """回答一份表彰为何包含或排除了某段服务。"""
        commendation = self.commendations.get(commendation_id)
        if commendation is None:
            raise RosterError("表彰不存在")
        period = commendation.period
        lines = [
            f"表彰 {commendation.commendation_id}：志愿者 {commendation.volunteer_id}，"
            f"审核 {commendation.approved_by}，"
            f"周期 {period.start:%Y-%m-%d %H:%M} 至 {period.end:%Y-%m-%d %H:%M}"
        ]
        for segment in commendation.included:
            lines.append(
                f"包含：{segment.position} 班次 {segment.shift_id}"
                f"（{segment.window.start:%Y-%m-%d %H:%M} 至 {segment.window.end:%Y-%m-%d %H:%M}，"
                f"{segment.hours:g} 小时）"
            )
        if not commendation.included:
            lines.append("没有可计入的服务时段")
        for exclusion in commendation.excluded:
            lines.append(f"排除：{exclusion.position} 班次 {exclusion.shift_id}，原因：{exclusion.detail}")
        total = sum(segment.hours for segment in commendation.included)
        lines.append(f"合计：{total:g} 小时")
        return "\n".join(lines)

    # ---------- 恢复与续办 ----------

    @classmethod
    def recover(
        cls,
        records: Iterable[Mapping[str, Any]],
        *,
        rest_interval: timedelta = DEFAULT_REST_INTERVAL,
    ) -> Roster:
        """系统恢复：重放记录链，之后可继续处理未审核的替班和即将开始的班次。"""
        roster = cls(rest_interval=rest_interval)
        for record in records:
            roster._apply(record)
            roster.records.append(dict(record))
        return roster

    def pending_substitutions(self) -> list[Substitution]:
        """尚未完成审核的替班申请。"""
        pending = [sub for sub in self.substitutions.values() if sub.state == SUBSTITUTION_PENDING]
        return sorted(pending, key=lambda sub: (sub.requested_at, sub.request_id))

    def upcoming_shifts(self, now: datetime) -> list[Shift]:
        """即将开始、仍可排班与签到的班次。"""
        upcoming = [
            shift
            for shift in self.shifts.values()
            if shift.state == SHIFT_SCHEDULED and shift.window.end > now
        ]
        return sorted(upcoming, key=lambda shift: (shift.window.start, shift.shift_id))

    # ---------- 记录链内部 ----------

    def _append(self, kind: str, at: datetime, data: dict[str, Any]) -> None:
        record = {"seq": len(self.records) + 1, "kind": kind, "at": at.isoformat(), "data": data}
        self._apply(record)
        self.records.append(record)

    def _apply(self, record: Mapping[str, Any]) -> None:
        handler = getattr(self, f"_apply_{record['kind']}", None)
        if handler is None:
            raise RosterError(f"未知记录类型：{record['kind']}")
        handler(record["data"])

    def _apply_volunteer_registered(self, data: Mapping[str, Any]) -> None:
        self.volunteers[data["volunteer_id"]] = Volunteer(
            data["volunteer_id"], data["name"], dict(data.get("languages", {}))
        )

    def _apply_language_certified(self, data: Mapping[str, Any]) -> None:
        self.volunteers[data["volunteer_id"]].languages[data["language"]] = data["level"]

    def _apply_training_recorded(self, data: Mapping[str, Any]) -> None:
        self.volunteers[data["volunteer_id"]].qualifications.add(data["qualification"])

    def _apply_supervisor_registered(self, data: Mapping[str, Any]) -> None:
        self.supervisors.add(data["supervisor_id"])

    def _apply_reviewer_registered(self, data: Mapping[str, Any]) -> None:
        self.reviewers.add(data["reviewer_id"])

    def _apply_position_registered(self, data: Mapping[str, Any]) -> None:
        self.positions[data["position_id"]] = Position(
            data["position_id"],
            data["name"],
            data["venue"],
            data["capacity"],
            data["required_qualification"],
            frozenset(data["required_languages"]),
        )

    def _apply_shift_opened(self, data: Mapping[str, Any]) -> None:
        self.shifts[data["shift_id"]] = Shift(
            data["shift_id"],
            data["position_id"],
            TimeWindow.from_record(data["window"]),
            data.get("arrangement_id"),
        )

    def _apply_assignment_created(self, data: Mapping[str, Any]) -> None:
        assignment = Assignment(data["shift_id"], data["volunteer_id"], data["source"])
        self.assignments[(assignment.shift_id, assignment.volunteer_id)] = assignment

    def _apply_assignment_released(self, data: Mapping[str, Any]) -> None:
        self.assignments[(data["shift_id"], data["volunteer_id"])].state = data["state"]

    def _apply_checkin_recorded(self, data: Mapping[str, Any]) -> None:
        checkin = CheckIn(data["volunteer_id"], datetime.fromisoformat(data["checked_in_at"]))
        self.shifts[data["shift_id"]].checkins[checkin.volunteer_id] = checkin

    def _apply_shift_started(self, data: Mapping[str, Any]) -> None:
        self.shifts[data["shift_id"]].state = SHIFT_STARTED

    def _apply_anomaly_appended(self, data: Mapping[str, Any]) -> None:
        self._apply_log(data, "anomaly")

    def _apply_handover_appended(self, data: Mapping[str, Any]) -> None:
        self._apply_log(data, "handover")

    def _apply_log(self, data: Mapping[str, Any], kind: str) -> None:
        entry = ShiftLogEntry(kind, data["author"], data["detail"], datetime.fromisoformat(data["at"]))
        self.shifts[data["shift_id"]].log.append(entry)

    def _apply_shift_closed(self, data: Mapping[str, Any]) -> None:
        self.shifts[data["shift_id"]].state = SHIFT_CLOSED

    def _apply_shift_cancelled(self, data: Mapping[str, Any]) -> None:
        self.shifts[data["shift_id"]].state = SHIFT_CANCELLED

    def _apply_substitution_requested(self, data: Mapping[str, Any]) -> None:
        self.substitutions[data["request_id"]] = Substitution(
            data["request_id"],
            data["shift_id"],
            data["original_id"],
            data["substitute_id"],
            data["reason"],
            datetime.fromisoformat(data["requested_at"]),
        )

    def _apply_substitution_owner_confirmed(self, data: Mapping[str, Any]) -> None:
        self.substitutions[data["request_id"]].owner_confirmed_at = datetime.fromisoformat(data["at"])

    def _apply_substitution_supervisor_confirmed(self, data: Mapping[str, Any]) -> None:
        sub = self.substitutions[data["request_id"]]
        sub.supervisor_confirmed_by = data["by"]
        sub.supervisor_confirmed_at = datetime.fromisoformat(data["at"])

    def _apply_substitution_applied(self, data: Mapping[str, Any]) -> None:
        self.substitutions[data["request_id"]].state = SUBSTITUTION_APPLIED

    def _apply_substitution_rejected(self, data: Mapping[str, Any]) -> None:
        sub = self.substitutions[data["request_id"]]
        sub.state = SUBSTITUTION_REJECTED
        sub.decided_reason = data["reason"]

    def _apply_commendation_granted(self, data: Mapping[str, Any]) -> None:
        self.commendations[data["commendation_id"]] = Commendation(
            data["commendation_id"],
            data["volunteer_id"],
            TimeWindow.from_record(data["period"]),
            data["approved_by"],
            datetime.fromisoformat(data["granted_at"]),
            [ServiceSegment.from_record(segment) for segment in data["included"]],
            [Exclusion.from_record(exclusion) for exclusion in data["excluded"]],
        )

    # ---------- 校验辅助 ----------

    def _volunteer(self, volunteer_id: str) -> Volunteer:
        volunteer = self.volunteers.get(volunteer_id)
        if volunteer is None:
            raise RosterError("志愿者未报名")
        return volunteer

    def _shift(self, shift_id: str) -> Shift:
        shift = self.shifts.get(shift_id)
        if shift is None:
            raise RosterError("班次不存在")
        return shift

    def _substitution(self, request_id: str) -> Substitution:
        sub = self.substitutions.get(request_id)
        if sub is None:
            raise RosterError("替班申请不存在")
        return sub

    def _append_log(self, kind: str, shift_id: str, author: str, detail: str, at: datetime) -> None:
        shift = self._shift(shift_id)
        if shift.state != SHIFT_STARTED:
            raise RosterError("仅已开始的班次可追加异常和交接")
        if not detail.strip():
            raise RosterError("追加内容不能为空")
        self._append(
            kind,
            at,
            {"shift_id": shift_id, "author": author, "detail": detail, "at": at.isoformat()},
        )

    def _validate_eligibility(self, shift: Shift, volunteer_id: str, freeing: Assignment | None) -> None:
        """排班与替班共用的约束：资质、语言、场地人数、互斥与休息间隔。"""
        volunteer = self.volunteers.get(volunteer_id)
        if volunteer is None:
            raise RosterError("志愿者未报名")
        position = self.positions[shift.position_id]
        if position.required_qualification and position.required_qualification not in volunteer.qualifications:
            raise RosterError("缺少岗位资质")
        if position.required_languages - set(volunteer.languages):
            raise RosterError("缺少服务对象语言")
        active = 0
        for assignment in self.assignments.values():
            if assignment.shift_id != shift.shift_id or assignment.state != ASSIGNMENT_ACTIVE:
                continue
            if assignment is freeing:
                continue
            if assignment.volunteer_id == volunteer_id:
                raise RosterError("不能重复排班")
            active += 1
        if active >= position.capacity:
            raise RosterError("场地人数已满")
        for assignment in self.assignments.values():
            if assignment.volunteer_id != volunteer_id or assignment.state != ASSIGNMENT_ACTIVE:
                continue
            other = self.shifts[assignment.shift_id]
            if other.state == SHIFT_CANCELLED:
                continue
            if other.window.overlaps(shift.window):
                raise RosterError("班次时间冲突")
            if other.window.gap_to(shift.window) < self.rest_interval:
                raise RosterError("休息间隔不足")

    def _maybe_complete_substitution(self, request_id: str, at: datetime) -> None:
        sub = self.substitutions[request_id]
        if sub.owner_confirmed_at is None or sub.supervisor_confirmed_at is None:
            return
        shift = self.shifts[sub.shift_id]
        original = self.assignments.get((sub.shift_id, sub.original_id))
        try:
            if shift.state != SHIFT_SCHEDULED or original is None or original.state != ASSIGNMENT_ACTIVE:
                raise RosterError("班次或名额状态已变化")
            self._validate_eligibility(shift, sub.substitute_id, freeing=original)
        except RosterError as exc:
            self._append("substitution_rejected", at, {"request_id": request_id, "reason": str(exc)})
            return
        self._append(
            "assignment_released",
            at,
            {
                "shift_id": sub.shift_id,
                "volunteer_id": sub.original_id,
                "state": ASSIGNMENT_SUBSTITUTED,
                "reason": "替班",
            },
        )
        self._append(
            "assignment_created",
            at,
            {"shift_id": sub.shift_id, "volunteer_id": sub.substitute_id, "source": "substitute"},
        )
        self._append("substitution_applied", at, {"request_id": request_id})
