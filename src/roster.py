"""赛事志愿服务的排班、替班、签到与表彰记录链。

设计要点：

- 所有命令都落成只增事件（``_record``），状态由 ``_apply`` 重放得到；
  系统恢复后用同一份事件日志重建，即可继续处理未审核替班和即将开始的班次。
- 签到、签退是事实：班次开始后，排班只能追加异常与交接，
  不能改派名额、不能回写签到（见 :meth:`Roster._ensure_not_started`）。
- 服务时长归实际服务者：替班在班前生效后，名额与后续签到记在替班人身上，
  原负责人的该段服务在表彰中标记为"替班转出"，并给出证据事件序号。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

REST_INTERVAL = timedelta(hours=10)


class RosterError(ValueError):
    """业务规则被违反。"""


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


@dataclass
class _State:
    volunteers: dict[str, dict[str, Any]] = field(default_factory=dict)
    requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (班次, 志愿者) -> 指派记录
    assignments: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # (班次, 志愿者) -> 签到事实
    attendance: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    substitutions: dict[str, dict[str, Any]] = field(default_factory=dict)
    awards: dict[str, dict[str, Any]] = field(default_factory=dict)
    award_authority: set[str] = field(default_factory=set)


class Roster:
    """围绕一条只增事件日志提供排班领域命令与查询。"""

    def __init__(self, rest_interval: timedelta = REST_INTERVAL) -> None:
        self.rest_interval = rest_interval
        self._events: list[dict[str, Any]] = []
        self.state = _State()

    # ---- 事件日志与恢复 -------------------------------------------------

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    def to_json(self) -> str:
        return json.dumps(self._events, ensure_ascii=False, indent=2)

    @classmethod
    def from_events(
        cls, events: Iterable[dict[str, Any]], rest_interval: timedelta = REST_INTERVAL
    ) -> "Roster":
        roster = cls(rest_interval=rest_interval)
        for event in events:
            roster._apply(event)
            roster._events.append(event)
        return roster

    def _record(self, event_type: str, at: str | datetime, **payload: Any) -> dict[str, Any]:
        event = {"seq": len(self._events) + 1, "at": iso(parse_dt(at)), "type": event_type}
        event.update(payload)
        self._apply(event)
        self._events.append(event)
        return event

    @staticmethod
    def _apply(event: dict[str, Any]) -> None:  # noqa: C901 - 投影按事件类型分派
        raise NotImplementedError  # 在下方与命令一起实现，避免方法顺序影响阅读

    # ---- 基础资料 -------------------------------------------------------

    def register_volunteer(self, volunteer_id: str, name: str, at: str | datetime) -> None:
        if volunteer_id in self.state.volunteers:
            raise RosterError("志愿者已报名，不能重复建档")
        if not name.strip():
            raise RosterError("志愿者姓名不能为空")
        self._record("VolunteerRegistered", at, volunteer_id=volunteer_id, name=name)

    def add_language(self, volunteer_id: str, language: str, at: str | datetime) -> None:
        volunteer = self._volunteer(volunteer_id)
        language = language.strip()
        if not language:
            raise RosterError("语言能力不能为空")
        if language in volunteer["languages"]:
            raise RosterError("语言能力已登记")
        self._record("LanguageRecorded", at, volunteer_id=volunteer_id, language=language)

    def certify_training(self, volunteer_id: str, role: str, at: str | datetime) -> None:
        volunteer = self._volunteer(volunteer_id)
        role = role.strip()
        if not role:
            raise RosterError("岗位名称不能为空")
        if role in volunteer["trainings"]:
            raise RosterError("岗位培训已合格")
        self._record("TrainingCertified", at, volunteer_id=volunteer_id, role=role)

    def grant_award_authority(self, person: str, at: str | datetime) -> None:
        if not person.strip():
            raise RosterError("审核人不能为空")
        if person in self.state.award_authority:
            raise RosterError("表彰审核权限已授予")
        self._record("AwardAuthorityGranted", at, person=person)

    # ---- 班次需求与排班 -------------------------------------------------

    def define_shift(
        self,
        shift_id: str,
        venue: str,
        role: str,
        start: str | datetime,
        end: str | datetime,
        headcount: int,
        required_languages: Iterable[str] = (),
        at: str | datetime | None = None,
    ) -> None:
        start_dt, end_dt = parse_dt(start), parse_dt(end)
        if end_dt <= start_dt:
            raise RosterError("班次结束时间必须晚于开始时间")
        if not isinstance(headcount, int) or isinstance(headcount, bool) or headcount < 1:
            raise RosterError("场地人数必须为正整数")
        if shift_id in self.state.requirements:
            raise RosterError("班次需求已存在")
        if at is None:
            at = start_dt
        self._record(
            "ShiftDefined",
            at,
            shift_id=shift_id,
            venue=venue,
            role=role,
            start=iso(start_dt),
            end=iso(end_dt),
            headcount=headcount,
            required_languages=sorted(set(required_languages)),
        )

    def assign(self, shift_id: str, volunteer_id: str, at: str | datetime) -> None:
        requirement = self._requirement(shift_id, active=True)
        volunteer = self._volunteer(volunteer_id)
        self._ensure_not_started(shift_id, at)
        if requirement["role"] not in volunteer["trainings"]:
            raise RosterError("岗位资质不符：缺少该岗位培训合格记录")
        if (shift_id, volunteer_id) in self.state.assignments:
            raise RosterError("不能在同一班次重复排班")
        self._ensure_capacity(shift_id)
        self._ensure_language_coverage(requirement, volunteer_id, adding=True)
        self._ensure_rest_and_exclusive(volunteer_id, requirement)
        self._record("VolunteerAssigned", at, shift_id=shift_id, volunteer_id=volunteer_id)

    def release_assignment(
        self, shift_id: str, volunteer_id: str, reason: str, at: str | datetime
    ) -> None:
        """班前言自愿退出/释放名额；已开始的班次拒绝改派。"""
        self._active_assignment(shift_id, volunteer_id)
        self._ensure_not_started(shift_id, at)
        requirement = self.state.requirements[shift_id]
        remaining = self._active_languages(shift_id, exclude=(volunteer_id,))
        missing = set(requirement["required_languages"]) - remaining
        if missing:
            free_slots = requirement["headcount"] - self._active_count(shift_id) + 1
            if len(missing) > free_slots:
                raise RosterError(f"释放后服务对象语言将无人保障：{sorted(missing)}")
        self._record(
            "AssignmentReleased",
            at,
            shift_id=shift_id,
            volunteer_id=volunteer_id,
            reason=reason,
        )

    def cancel_requirement(self, shift_id: str, reason: str, at: str | datetime) -> None:
        """取消单班需求（如机场接送、酒店安排临时取消）。

        只释放该班次自己的名额，其他班次的指派完全不动；签到事实仍然保留。
        """
        requirement = self._requirement(shift_id, active=True)
        if self._is_started(shift_id, parse_dt(at)):
            raise RosterError("班次已开始：只能追加异常和交接，不能整体取消")
        self._record(
            "RequirementCancelled",
            at,
            shift_id=shift_id,
            venue=requirement["venue"],
            reason=reason,
        )

    # ---- 签到（不可回写的事实） ----------------------------------------

    def check_in(self, shift_id: str, volunteer_id: str, at: str | datetime) -> None:
        self._active_assignment(shift_id, volunteer_id)
        record = self.state.attendance.setdefault(
            (shift_id, volunteer_id), {"in": None, "out": None, "exceptions": [], "handovers": []}
        )
        if record["in"] is not None:
            raise RosterError("签到事实已存在，不能重复签到或回写")
        self._record("CheckedIn", at, shift_id=shift_id, volunteer_id=volunteer_id)

    def check_out(self, shift_id: str, volunteer_id: str, at: str | datetime) -> None:
        self._active_assignment(shift_id, volunteer_id)
        record = self._attendance(shift_id, volunteer_id)
        if record["in"] is None:
            raise RosterError("尚未签到，不能签退")
        if record["out"] is not None:
            raise RosterError("签退事实已存在，不能回写")
        if parse_dt(at) < record["in"]:
            raise RosterError("签退时间不能早于签到时间")
        self._record("CheckedOut", at, shift_id=shift_id, volunteer_id=volunteer_id)

    def add_exception(
        self,
        shift_id: str,
        volunteer_id: str,
        at: str | datetime,
        note: str,
        recorded_by: str,
    ) -> None:
        """异常在班次开始前后都可追加，但绝不改动签到事实。"""
        if not note.strip():
            raise RosterError("异常说明不能为空")
        if (shift_id, volunteer_id) not in self.state.assignments:
            raise RosterError("只能为本班次相关人员记录异常")
        self._record(
            "ShiftExceptionRecorded",
            at,
            shift_id=shift_id,
            volunteer_id=volunteer_id,
            note=note,
            recorded_by=recorded_by,
        )

    def record_handover(
        self,
        shift_id: str,
        outgoing_id: str,
        incoming_id: str,
        at: str | datetime,
        note: str,
    ) -> None:
        """交接是开始后唯一允许的班次变更类记录，不改写签到与时长归属。"""
        if outgoing_id == incoming_id:
            raise RosterError("交接双方不能是同一人")
        self._volunteer(outgoing_id)
        self._volunteer(incoming_id)
        if (shift_id, outgoing_id) not in self.state.assignments:
            raise RosterError("交出人未关联该班次")
        self._record(
            "HandoverRecorded",
            at,
            shift_id=shift_id,
            outgoing_id=outgoing_id,
            incoming_id=incoming_id,
            note=note,
        )

    # ---- 替班 -----------------------------------------------------------

    def request_substitution(
        self,
        substitution_id: str,
        shift_id: str,
        original_id: str,
        substitute_id: str,
        reason: str,
        at: str | datetime,
    ) -> None:
        if substitution_id in self.state.substitutions:
            raise RosterError("替班申请已存在")
        if original_id == substitute_id:
            raise RosterError("替班人不能与原负责人相同")
        self._active_assignment(shift_id, original_id)
        self._volunteer(substitute_id)
        if self.state.assignments.get((shift_id, substitute_id), {}).get("status") == "active":
            raise RosterError("替班人已在该班次在岗")
        self._ensure_not_started(shift_id, at)
        requirement = self._requirement(shift_id, active=True)
        # 替班是等额换人名额不变，仍需独立满足资质、语言与休息规则。
        if requirement["role"] not in self.state.volunteers[substitute_id]["trainings"]:
            raise RosterError("替班人岗位资质不符")
        self._ensure_language_coverage(requirement, substitute_id, adding=True, replacing=original_id)
        self._ensure_rest_and_exclusive(substitute_id, requirement, ignore_shift=shift_id)
        self._record(
            "SubstitutionRequested",
            at,
            substitution_id=substitution_id,
            shift_id=shift_id,
            original_id=original_id,
            substitute_id=substitute_id,
            reason=reason,
        )

    def acknowledge_substitution(
        self, substitution_id: str, actor: str, at: str | datetime
    ) -> None:
        sub = self._substitution(substitution_id, status="pending")
        if actor != sub["original_id"]:
            raise RosterError("替班必须由原负责人本人确认")
        self._record("SubstitutionAcknowledged", at, substitution_id=substitution_id, actor=actor)

    def approve_substitution(
        self, substitution_id: str, supervisor: str, at: str | datetime
    ) -> None:
        sub = self._substitution(substitution_id)
        if sub["status"] not in {"pending", "acknowledged"}:
            raise RosterError("替班申请已结束审批")
        if sub["ack_at"] is None:
            raise RosterError("主管审批前必须先有原负责人确认")
        if supervisor in {sub["original_id"], sub["substitute_id"]}:
            raise RosterError("主管不能是替班当事人，确认必须分别完成")
        self._requirement(sub["shift_id"], active=True)
        self._active_assignment(sub["shift_id"], sub["original_id"])
        if self.state.assignments.get(
            (sub["shift_id"], sub["substitute_id"]), {}
        ).get("status") == "active":
            raise RosterError("替班人已在该班次在岗，替班无法生效")
        self._ensure_not_started(sub["shift_id"], at)
        return self._record(
            "SubstitutionApproved", at, substitution_id=substitution_id, supervisor=supervisor
        )

    def reject_substitution(
        self, substitution_id: str, actor: str, at: str | datetime, note: str = ""
    ) -> None:
        sub = self._substitution(substitution_id)
        if sub["status"] not in {"pending", "acknowledged"}:
            raise RosterError("替班申请已结束审批")
        self._record(
            "SubstitutionRejected",
            at,
            substitution_id=substitution_id,
            actor=actor,
            note=note,
        )

    def pending_substitutions(self) -> tuple[dict[str, Any], ...]:
        """系统恢复后优先继续处理的未审核替班。"""
        return tuple(
            {"substitution_id": sid, **sub}
            for sid, sub in self.state.substitutions.items()
            if sub["status"] in {"pending", "acknowledged"}
        )

    def upcoming_shifts(self, now: str | datetime, within: timedelta) -> tuple[str, ...]:
        """恢复后需要立即关注的即将开始且仍有名额缺口的班次。"""
        now_dt = parse_dt(now)
        result = []
        for sid, requirement in self.state.requirements.items():
            if requirement["cancelled"]:
                continue
            if now_dt <= requirement["start"] <= now_dt + within and self._active_count(sid) < (
                requirement["headcount"]
            ):
                result.append(sid)
        return tuple(sorted(result))

    # ---- 服务时长与表彰 -------------------------------------------------

    def service_periods(self, volunteer_id: str, at: str | datetime) -> list[dict[str, Any]]:
        """逐段给出服务时长的计入/排除结论与证据，回答表彰追溯问题。"""
        self._volunteer(volunteer_id)
        now_dt = parse_dt(at)
        periods: list[dict[str, Any]] = []
        for (sid, vid), assignment in sorted(self.state.assignments.items(), key=lambda kv: kv[0][0]):
            if vid != volunteer_id:
                continue
            requirement = self.state.requirements[sid]
            attendance = self.state.attendance.get((sid, vid))
            period = {
                "shift_id": sid,
                "venue": requirement["venue"],
                "role": requirement["role"],
                "included": False,
                "served_minutes": 0,
                "reasons": [],
                "evidence": [assignment["seq"]],
            }
            if requirement["cancelled"]:
                period["reasons"].append("班次需求已取消")
            elif assignment["status"] == "released":
                reason = "替班已转出，时长归实际服务者" if assignment.get("via_substitution") else "名额已释放"
                period["reasons"].append(reason)
                if assignment.get("via_substitution"):
                    period["evidence"].append(assignment["via_substitution"])
            elif requirement["end"] > now_dt:
                period["reasons"].append("班次尚未结束，暂不计入")
            elif attendance is None or attendance["in"] is None:
                period["reasons"].append("已排班但未签到")
            elif attendance["out"] is None:
                period["reasons"].append("缺少签退，服务时长未闭环")
                period["evidence"].append(attendance["in_seq"])
            else:
                minutes = int((attendance["out"] - attendance["in"]).total_seconds() // 60)
                period["included"] = True
                period["served_minutes"] = minutes
                period["reasons"].append("签到签退完整")
                period["evidence"].extend([attendance["in_seq"], attendance["out_seq"]])
            if attendance:
                period["evidence"].extend(attendance["exceptions"])
                period["evidence"].extend(attendance["handovers"])
            period["evidence"] = sorted(set(period["evidence"]))
            periods.append(period)
        return periods

    def decide_award(
        self,
        award_id: str,
        volunteer_id: str,
        approver: str,
        at: str | datetime,
        min_minutes: int = 0,
    ) -> dict[str, Any]:
        if award_id in self.state.awards:
            raise RosterError("表彰决定已存在")
        if approver == volunteer_id:
            raise RosterError("任何人不能批准自己的奖励")
        if approver not in self.state.award_authority:
            raise RosterError("审批人不具备表彰审核权限")
        periods = self.service_periods(volunteer_id, at)
        total = sum(p["served_minutes"] for p in periods if p["included"])
        granted = total >= min_minutes
        self._record(
            "AwardDecided",
            at,
            award_id=award_id,
            volunteer_id=volunteer_id,
            approver=approver,
            granted=granted,
            total_minutes=total,
            periods=periods,
        )
        return self.state.awards[award_id]

    def award_explanation(self, award_id: str) -> dict[str, Any]:
        if award_id not in self.state.awards:
            raise RosterError("表彰记录不存在")
        return self.state.awards[award_id]

    # ---- 校验辅助 -------------------------------------------------------

    def _volunteer(self, volunteer_id: str) -> dict[str, Any]:
        if volunteer_id not in self.state.volunteers:
            raise RosterError("志愿者尚未报名")
        return self.state.volunteers[volunteer_id]

    def _requirement(self, shift_id: str, active: bool = False) -> dict[str, Any]:
        requirement = self.state.requirements.get(shift_id)
        if requirement is None:
            raise RosterError("班次需求不存在")
        if active and requirement["cancelled"]:
            raise RosterError("班次需求已取消")
        return requirement

    def _active_assignment(self, shift_id: str, volunteer_id: str) -> dict[str, Any]:
        assignment = self.state.assignments.get((shift_id, volunteer_id))
        if assignment is None or assignment["status"] != "active":
            raise RosterError("该志愿者未在此班次在岗")
        return assignment

    def _attendance(self, shift_id: str, volunteer_id: str) -> dict[str, Any]:
        record = self.state.attendance.get((shift_id, volunteer_id))
        if record is None:
            raise RosterError("该人员尚无签到记录")
        return record

    def _substitution(self, substitution_id: str, status: str | None = None) -> dict[str, Any]:
        sub = self.state.substitutions.get(substitution_id)
        if sub is None:
            raise RosterError("替班申请不存在")
        if status is not None and sub["status"] != status:
            raise RosterError(f"替班申请状态应为{status}")
        return sub

    def _active_count(self, shift_id: str) -> int:
        return sum(
            1
            for (sid, _), assignment in self.state.assignments.items()
            if sid == shift_id and assignment["status"] == "active"
        )

    def _active_languages(self, shift_id: str, exclude: Iterable[str] = ()) -> set[str]:
        excluded = set(exclude)
        languages: set[str] = set()
        for (sid, vid), assignment in self.state.assignments.items():
            if sid == shift_id and assignment["status"] == "active" and vid not in excluded:
                languages.update(self.state.volunteers[vid]["languages"])
        return languages

    def _ensure_capacity(self, shift_id: str) -> None:
        requirement = self.state.requirements[shift_id]
        if self._active_count(shift_id) >= requirement["headcount"]:
            raise RosterError("场地人数已满")

    def _ensure_language_coverage(
        self,
        requirement: dict[str, Any],
        volunteer_id: str,
        adding: bool,
        replacing: str | None = None,
    ) -> None:
        exclude = () if replacing is None else (replacing,)
        languages = self._active_languages(requirement["id"], exclude=exclude)
        if adding:
            languages |= self.state.volunteers[volunteer_id]["languages"]
        missing = set(requirement["required_languages"]) - languages
        if not missing:
            return
        # 仍可补齐：每种缺失语言至少占用一个剩余名额；名额不足则该排法无法成立。
        count_after = self._active_count(requirement["id"]) - len(exclude) + (1 if adding else 0)
        free_slots = requirement["headcount"] - count_after
        if len(missing) > free_slots:
            raise RosterError(f"服务对象语言缺少保障：{sorted(missing)}")

    def validate_shift_ready(self, shift_id: str) -> None:
        """班前就绪校验：人数到齐且服务对象语言全部有人保障。"""
        requirement = self._requirement(shift_id, active=True)
        missing = set(requirement["required_languages"]) - self._active_languages(shift_id)
        if missing:
            raise RosterError(f"班前语言保障未到位：{sorted(missing)}")
        if self._active_count(shift_id) < requirement["headcount"]:
            raise RosterError("班前人数未到齐")

    def _ensure_rest_and_exclusive(
        self,
        volunteer_id: str,
        requirement: dict[str, Any],
        ignore_shift: str | None = None,
    ) -> None:
        new_start, new_end = requirement["start"], requirement["end"]
        for (sid, vid), assignment in self.state.assignments.items():
            if vid != volunteer_id or assignment["status"] != "active" or sid == ignore_shift:
                continue
            other = self.state.requirements[sid]
            if other["cancelled"]:
                continue
            if new_start < other["end"] and other["start"] < new_end:
                raise RosterError("班次时间互斥，不能重复排班")
            if other["end"] <= new_start and new_start - other["end"] < self.rest_interval:
                raise RosterError("与上一班之间休息间隔不足")
            if new_end <= other["start"] and other["start"] - new_end < self.rest_interval:
                raise RosterError("与下一班之间休息间隔不足")

    def _is_started(self, shift_id: str, at: datetime) -> bool:
        requirement = self.state.requirements[shift_id]
        if at >= requirement["start"]:
            return True
        return any(
            sid == shift_id and record["in"] is not None
            for (sid, _), record in self.state.attendance.items()
        )

    def _ensure_not_started(self, shift_id: str, at: str | datetime) -> None:
        if self._is_started(shift_id, parse_dt(at)):
            raise RosterError("班次已开始：只能追加异常和交接，不能改派或回写")


# ---- 事件投影：与命令分离，保证恢复时逐字重放 -----------------------------


def _apply(self: Roster, event: dict[str, Any]) -> None:  # noqa: C901
    state = self.state
    etype = event["type"]
    payload = event
    if etype == "VolunteerRegistered":
        state.volunteers[payload["volunteer_id"]] = {
            "name": payload["name"],
            "languages": set(),
            "trainings": set(),
        }
    elif etype == "LanguageRecorded":
        state.volunteers[payload["volunteer_id"]]["languages"].add(payload["language"])
    elif etype == "TrainingCertified":
        state.volunteers[payload["volunteer_id"]]["trainings"].add(payload["role"])
    elif etype == "AwardAuthorityGranted":
        state.award_authority.add(payload["person"])
    elif etype == "ShiftDefined":
        state.requirements[payload["shift_id"]] = {
            "id": payload["shift_id"],
            "venue": payload["venue"],
            "role": payload["role"],
            "start": parse_dt(payload["start"]),
            "end": parse_dt(payload["end"]),
            "headcount": payload["headcount"],
            "required_languages": set(payload["required_languages"]),
            "cancelled": False,
            "cancel_reason": None,
        }
    elif etype == "VolunteerAssigned":
        state.assignments[(payload["shift_id"], payload["volunteer_id"])] = {
            "status": "active",
            "via_substitution": None,
            "seq": event["seq"],
        }
    elif etype == "AssignmentReleased":
        state.assignments[(payload["shift_id"], payload["volunteer_id"])].update(
            status="released", reason=payload["reason"]
        )
    elif etype == "RequirementCancelled":
        requirement = state.requirements[payload["shift_id"]]
        requirement["cancelled"] = True
        requirement["cancel_reason"] = payload["reason"]
        for (sid, vid), assignment in state.assignments.items():
            if sid == payload["shift_id"] and assignment["status"] == "active":
                assignment["status"] = "released"
                assignment["reason"] = f"班次取消：{payload['reason']}"
    elif etype == "CheckedIn":
        record = state.attendance.setdefault(
            (payload["shift_id"], payload["volunteer_id"]),
            {"in": None, "out": None, "exceptions": [], "handovers": []},
        )
        record["in"] = parse_dt(event["at"])
        record["in_seq"] = event["seq"]
    elif etype == "CheckedOut":
        record = state.attendance[(payload["shift_id"], payload["volunteer_id"])]
        record["out"] = parse_dt(event["at"])
        record["out_seq"] = event["seq"]
    elif etype == "ShiftExceptionRecorded":
        record = state.attendance.setdefault(
            (payload["shift_id"], payload["volunteer_id"]),
            {"in": None, "out": None, "exceptions": [], "handovers": []},
        )
        record["exceptions"].append(event["seq"])
    elif etype == "HandoverRecorded":
        record = state.attendance.setdefault(
            (payload["shift_id"], payload["outgoing_id"]),
            {"in": None, "out": None, "exceptions": [], "handovers": []},
        )
        record["handovers"].append(event["seq"])
    elif etype == "SubstitutionRequested":
        state.substitutions[payload["substitution_id"]] = {
            "shift_id": payload["shift_id"],
            "original_id": payload["original_id"],
            "substitute_id": payload["substitute_id"],
            "reason": payload["reason"],
            "status": "pending",
            "ack_at": None,
            "approved_at": None,
            "supervisor": None,
        }
    elif etype == "SubstitutionAcknowledged":
        sub = state.substitutions[payload["substitution_id"]]
        sub["status"] = "acknowledged"
        sub["ack_at"] = event["at"]
    elif etype == "SubstitutionApproved":
        sub = state.substitutions[payload["substitution_id"]]
        sub["status"] = "effective"
        sub["approved_at"] = event["at"]
        sub["supervisor"] = payload["supervisor"]
        key = (sub["shift_id"], sub["original_id"])
        state.assignments[key].update(status="released", via_substitution=event["seq"])
        state.assignments[(sub["shift_id"], sub["substitute_id"])] = {
            "status": "active",
            "via_substitution": event["seq"],
            "seq": event["seq"],
        }
    elif etype == "SubstitutionRejected":
        state.substitutions[payload["substitution_id"]]["status"] = "rejected"
    elif etype == "AwardDecided":
        state.awards[payload["award_id"]] = {
            "volunteer_id": payload["volunteer_id"],
            "approver": payload["approver"],
            "granted": payload["granted"],
            "total_minutes": payload["total_minutes"],
            "periods": payload["periods"],
            "seq": event["seq"],
        }
    else:
        raise RosterError(f"未知事件类型：{etype}")


Roster._apply = _apply  # type: ignore[method-assign]
