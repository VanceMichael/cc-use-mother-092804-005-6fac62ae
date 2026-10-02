"""校验赛事服务中心排班、签到、替班与表彰的记录链规则。"""

import json
import unittest
from datetime import datetime

from src.roster import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_RELEASED,
    ASSIGNMENT_SUBSTITUTED,
    REASON_NO_CHECKIN,
    REASON_OUTSIDE_PERIOD,
    REASON_SHIFT_CANCELLED,
    REASON_SUBSTITUTED,
    SHIFT_CANCELLED,
    SHIFT_SCHEDULED,
    SUBSTITUTION_APPLIED,
    SUBSTITUTION_PENDING,
    SUBSTITUTION_REJECTED,
    Roster,
    RosterError,
    TimeWindow,
)


def at(day_time: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-{day_time}")


def window(start: str, end: str) -> TimeWindow:
    return TimeWindow(at(start), at(end))


T0 = at("01T09:00:00")
PERIOD = window("07T00:00:00", "13T00:00:00")


def base_roster() -> Roster:
    """闭幕周的岗位：制证、机场抵离、媒体酒店与需要小语种的告别派对。"""
    roster = Roster()
    roster.register_supervisor("sup-1", "主管一", at=T0)
    roster.register_reviewer("rev-1", "审核一", at=T0)
    roster.register_position("pos-badge", "制证", "制证中心", 2, "accreditation", at=T0)
    roster.register_position("pos-airport", "机场抵离", "机场", 2, "airport-ops", at=T0)
    roster.register_position("pos-hotel", "媒体酒店", "媒体酒店", 1, "hotel-ops", at=T0)
    roster.register_position("pos-farewell", "告别派对", "主场馆", 3, "event-ops", ("fr",), at=T0)
    return roster


def add_volunteer(roster: Roster, volunteer_id: str, *, languages=None, qualifications=()) -> None:
    roster.register_volunteer(volunteer_id, f"志愿者{volunteer_id}", languages=languages or {}, at=T0)
    for qualification in qualifications:
        roster.record_training(volunteer_id, qualification, at=T0)


class SchedulingConstraintTest(unittest.TestCase):
    """排班同时满足休息间隔、岗位资质、场地人数和服务对象语言。"""

    def setUp(self) -> None:
        self.roster = base_roster()
        self.roster.open_shift("s-badge", "pos-badge", window("08T06:00:00", "08T14:00:00"), at=T0)
        self.roster.open_shift("s-farewell", "pos-farewell", window("09T18:00:00", "10T00:30:00"), at=T0)

    def test_missing_qualification_is_rejected(self) -> None:
        add_volunteer(self.roster, "vol-a")
        with self.assertRaisesRegex(RosterError, "缺少岗位资质"):
            self.roster.assign("s-badge", "vol-a", at=T0)

    def test_missing_service_language_is_rejected(self) -> None:
        add_volunteer(self.roster, "vol-a", languages={"en": "B2"}, qualifications=("event-ops",))
        with self.assertRaisesRegex(RosterError, "缺少服务对象语言"):
            self.roster.assign("s-farewell", "vol-a", at=T0)

    def test_minority_language_volunteer_is_accepted(self) -> None:
        add_volunteer(self.roster, "vol-a", languages={"fr": "C1"}, qualifications=("event-ops",))
        self.roster.assign("s-farewell", "vol-a", at=T0)
        self.assertEqual(self.roster.assignments[("s-farewell", "vol-a")].state, ASSIGNMENT_ACTIVE)

    def test_capacity_is_enforced(self) -> None:
        self.roster.open_shift("s-hotel", "pos-hotel", window("08T08:00:00", "08T16:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("hotel-ops",))
        add_volunteer(self.roster, "vol-b", qualifications=("hotel-ops",))
        self.roster.assign("s-hotel", "vol-a", at=T0)
        with self.assertRaisesRegex(RosterError, "场地人数已满"):
            self.roster.assign("s-hotel", "vol-b", at=T0)

    def test_overlapping_shift_is_rejected(self) -> None:
        self.roster.open_shift("s-badge-2", "pos-badge", window("08T13:00:00", "08T20:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-a", at=T0)
        with self.assertRaisesRegex(RosterError, "班次时间冲突"):
            self.roster.assign("s-badge-2", "vol-a", at=T0)

    def test_duplicate_assignment_is_rejected(self) -> None:
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-a", at=T0)
        with self.assertRaisesRegex(RosterError, "不能重复排班"):
            self.roster.assign("s-badge", "vol-a", at=T0)

    def test_insufficient_rest_is_rejected(self) -> None:
        self.roster.open_shift("s-night", "pos-badge", window("08T18:00:00", "09T02:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-a", at=T0)
        with self.assertRaisesRegex(RosterError, "休息间隔不足"):
            self.roster.assign("s-night", "vol-a", at=T0)

    def test_full_rest_interval_is_accepted(self) -> None:
        self.roster.open_shift("s-night", "pos-badge", window("08T22:00:00", "09T06:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-a", at=T0)
        self.roster.assign("s-night", "vol-a", at=T0)
        self.assertEqual(self.roster.assignments[("s-night", "vol-a")].state, ASSIGNMENT_ACTIVE)


class StartedShiftTest(unittest.TestCase):
    """已开始的班次只能追加异常和交接，不能回写签到事实。"""

    def setUp(self) -> None:
        self.roster = base_roster()
        self.roster.open_shift("s-badge", "pos-badge", window("08T06:00:00", "08T14:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation",))
        add_volunteer(self.roster, "vol-b", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-a", at=T0)
        self.roster.assign("s-badge", "vol-b", at=T0)
        self.roster.record_checkin("s-badge", "vol-a", at=at("08T05:50:00"))
        self.roster.start_shift("s-badge", at=at("08T06:00:00"))

    def test_checkin_facts_are_frozen_after_start(self) -> None:
        with self.assertRaisesRegex(RosterError, "不能回写签到事实"):
            self.roster.record_checkin("s-badge", "vol-b", at=at("08T06:05:00"))
        with self.assertRaisesRegex(RosterError, "不能回写签到事实"):
            self.roster.record_checkin("s-badge", "vol-a", at=at("08T06:05:00"))
        checkins = self.roster.shifts["s-badge"].checkins
        self.assertEqual(list(checkins), ["vol-a"])
        self.assertEqual(checkins["vol-a"].at, at("08T05:50:00"))

    def test_anomaly_and_handover_are_appendable(self) -> None:
        self.roster.append_anomaly("s-badge", "vol-a", "制证设备故障，启用备用设备", at=at("08T09:00:00"))
        self.roster.append_handover("s-badge", "vol-a", "向岗位负责人交接未制证件清单", at=at("08T13:30:00"))
        log = self.roster.shifts["s-badge"].log
        self.assertEqual([entry.kind for entry in log], ["anomaly", "handover"])

    def test_log_append_requires_started_shift(self) -> None:
        self.roster.open_shift("s-later", "pos-badge", window("09T06:00:00", "09T14:00:00"), at=T0)
        with self.assertRaisesRegex(RosterError, "追加异常和交接"):
            self.roster.append_anomaly("s-later", "vol-a", "尚未开始", at=T0)
        self.roster.close_shift("s-badge", at=at("08T14:00:00"))
        with self.assertRaisesRegex(RosterError, "追加异常和交接"):
            self.roster.append_handover("s-badge", "vol-a", "已结束", at=at("08T14:05:00"))

    def test_checkin_cannot_be_rewritten_before_start(self) -> None:
        self.roster.open_shift("s-later", "pos-badge", window("09T06:00:00", "09T14:00:00"), at=T0)
        self.roster.assign("s-later", "vol-a", at=T0)
        self.roster.record_checkin("s-later", "vol-a", at=at("09T05:50:00"))
        with self.assertRaisesRegex(RosterError, "不可改写"):
            self.roster.record_checkin("s-later", "vol-a", at=at("09T05:55:00"))


class SubstitutionTest(unittest.TestCase):
    """替班申请需要原负责人和主管分别确认。"""

    def setUp(self) -> None:
        self.roster = base_roster()
        self.roster.open_shift("s-hotel", "pos-hotel", window("12T08:00:00", "12T16:00:00"), at=T0)
        add_volunteer(self.roster, "vol-a", qualifications=("hotel-ops",))
        add_volunteer(self.roster, "vol-b", qualifications=("hotel-ops",))
        self.roster.assign("s-hotel", "vol-a", at=T0)
        self.roster.request_substitution("sub-1", "s-hotel", "vol-a", "vol-b", "原负责人突发不适", at=at("11T10:00:00"))

    def test_single_confirmation_keeps_request_pending(self) -> None:
        self.roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("11T10:30:00"))
        self.assertEqual(self.roster.substitutions["sub-1"].state, SUBSTITUTION_PENDING)
        self.assertEqual(self.roster.assignments[("s-hotel", "vol-a")].state, ASSIGNMENT_ACTIVE)

    def test_owner_confirmation_must_come_from_original(self) -> None:
        with self.assertRaisesRegex(RosterError, "原负责人"):
            self.roster.confirm_substitution_by_owner("sub-1", "vol-b", at=at("11T10:30:00"))

    def test_supervisor_must_be_registered_and_independent(self) -> None:
        with self.assertRaisesRegex(RosterError, "主管未登记"):
            self.roster.confirm_substitution_by_supervisor("sub-1", "vol-b", at=at("11T10:30:00"))
        self.roster.register_supervisor("vol-b", "志愿者vol-b", at=T0)
        with self.assertRaisesRegex(RosterError, "独立"):
            self.roster.confirm_substitution_by_supervisor("sub-1", "vol-b", at=at("11T10:30:00"))

    def test_both_confirmations_apply_substitution(self) -> None:
        self.roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("11T10:30:00"))
        self.roster.confirm_substitution_by_supervisor("sub-1", "sup-1", at=at("11T11:00:00"))
        sub = self.roster.substitutions["sub-1"]
        self.assertEqual(sub.state, SUBSTITUTION_APPLIED)
        self.assertEqual(self.roster.assignments[("s-hotel", "vol-a")].state, ASSIGNMENT_SUBSTITUTED)
        substitute = self.roster.assignments[("s-hotel", "vol-b")]
        self.assertEqual(substitute.state, ASSIGNMENT_ACTIVE)
        self.assertEqual(substitute.source, "substitute")

    def test_started_shift_rejects_substitution_request(self) -> None:
        self.roster.open_shift("s-badge", "pos-badge", window("12T06:00:00", "12T14:00:00"), at=T0)
        add_volunteer(self.roster, "vol-c", qualifications=("accreditation",))
        add_volunteer(self.roster, "vol-d", qualifications=("accreditation",))
        self.roster.assign("s-badge", "vol-c", at=T0)
        self.roster.start_shift("s-badge", at=at("12T06:00:00"))
        with self.assertRaisesRegex(RosterError, "只能追加异常和交接"):
            self.roster.request_substitution("sub-2", "s-badge", "vol-c", "vol-d", "临时替班", at=at("12T07:00:00"))

    def test_ineligible_substitute_is_rejected_at_request(self) -> None:
        add_volunteer(self.roster, "vol-c")
        with self.assertRaisesRegex(RosterError, "缺少岗位资质"):
            self.roster.request_substitution("sub-2", "s-hotel", "vol-a", "vol-c", "测试", at=at("11T10:00:00"))

    def test_completion_rejects_when_substitute_became_ineligible(self) -> None:
        self.roster.open_shift("s-badge", "pos-badge", window("12T10:00:00", "12T18:00:00"), at=T0)
        self.roster.record_training("vol-b", "accreditation", at=T0)
        self.roster.assign("s-badge", "vol-b", at=T0)
        self.roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("11T10:30:00"))
        self.roster.confirm_substitution_by_supervisor("sub-1", "sup-1", at=at("11T11:00:00"))
        sub = self.roster.substitutions["sub-1"]
        self.assertEqual(sub.state, SUBSTITUTION_REJECTED)
        self.assertIn("班次时间冲突", sub.decided_reason)
        self.assertEqual(self.roster.assignments[("s-hotel", "vol-a")].state, ASSIGNMENT_ACTIVE)


class SubstitutionHoursTest(unittest.TestCase):
    """临时替班不丢失服务时长和表彰依据。"""

    def test_served_hours_survive_substitution(self) -> None:
        roster = base_roster()
        roster.open_shift("s-1", "pos-badge", window("08T06:00:00", "08T14:00:00"), at=T0)
        roster.open_shift("s-2", "pos-badge", window("10T06:00:00", "10T14:00:00"), at=T0)
        add_volunteer(roster, "vol-a", qualifications=("accreditation",))
        add_volunteer(roster, "vol-b", qualifications=("accreditation",))
        roster.assign("s-1", "vol-a", at=T0)
        roster.assign("s-2", "vol-a", at=T0)
        roster.record_checkin("s-1", "vol-a", at=at("08T05:50:00"))
        roster.start_shift("s-1", at=at("08T06:00:00"))
        roster.close_shift("s-1", at=at("08T14:00:00"))
        roster.request_substitution("sub-1", "s-2", "vol-a", "vol-b", "临时替班", at=at("09T20:00:00"))
        roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("09T20:10:00"))
        roster.confirm_substitution_by_supervisor("sub-1", "sup-1", at=at("09T20:20:00"))
        roster.record_checkin("s-2", "vol-b", at=at("10T05:55:00"))
        roster.start_shift("s-2", at=at("10T06:00:00"))
        roster.close_shift("s-2", at=at("10T14:00:00"))

        assessment_a = roster.assess_service("vol-a", PERIOD)
        self.assertEqual([segment.shift_id for segment in assessment_a.included], ["s-1"])
        self.assertEqual(assessment_a.total_hours, 8.0)
        self.assertIn("s-2", [exclusion.shift_id for exclusion in assessment_a.excluded])

        assessment_b = roster.assess_service("vol-b", PERIOD)
        self.assertEqual([segment.shift_id for segment in assessment_b.included], ["s-2"])
        self.assertEqual(assessment_b.total_hours, 8.0)


class CancellationTest(unittest.TestCase):
    """机场或酒店安排取消时只释放相关名额，其他岗位保持不变。"""

    def test_cancel_arrangement_releases_only_related_slots(self) -> None:
        roster = base_roster()
        roster.open_shift("s-pickup-1", "pos-airport", window("10T04:00:00", "10T08:00:00"), arrangement_id="flight-ca123", at=T0)
        roster.open_shift("s-pickup-2", "pos-airport", window("10T23:00:00", "11T03:00:00"), arrangement_id="flight-ca123", at=T0)
        roster.open_shift("s-hotel", "pos-hotel", window("10T08:00:00", "10T16:00:00"), arrangement_id="hotel-block-9", at=T0)
        roster.open_shift("s-badge", "pos-badge", window("10T06:00:00", "10T14:00:00"), at=T0)
        add_volunteer(roster, "vol-a", qualifications=("airport-ops",))
        add_volunteer(roster, "vol-b", qualifications=("airport-ops",))
        add_volunteer(roster, "vol-c", qualifications=("hotel-ops",))
        add_volunteer(roster, "vol-d", qualifications=("accreditation",))
        roster.assign("s-pickup-1", "vol-a", at=T0)
        roster.assign("s-pickup-2", "vol-b", at=T0)
        roster.assign("s-hotel", "vol-c", at=T0)
        roster.assign("s-badge", "vol-d", at=T0)

        roster.cancel_arrangement("flight-ca123", "代表团航班取消", at=at("09T20:00:00"))

        self.assertEqual(roster.shifts["s-pickup-1"].state, SHIFT_CANCELLED)
        self.assertEqual(roster.shifts["s-pickup-2"].state, SHIFT_CANCELLED)
        self.assertEqual(roster.assignments[("s-pickup-1", "vol-a")].state, ASSIGNMENT_RELEASED)
        self.assertEqual(roster.assignments[("s-pickup-2", "vol-b")].state, ASSIGNMENT_RELEASED)
        self.assertEqual(roster.shifts["s-hotel"].state, SHIFT_SCHEDULED)
        self.assertEqual(roster.shifts["s-badge"].state, SHIFT_SCHEDULED)
        self.assertEqual(roster.assignments[("s-hotel", "vol-c")].state, ASSIGNMENT_ACTIVE)
        self.assertEqual(roster.assignments[("s-badge", "vol-d")].state, ASSIGNMENT_ACTIVE)

    def test_unknown_arrangement_is_rejected(self) -> None:
        roster = base_roster()
        with self.assertRaisesRegex(RosterError, "未找到可取消"):
            roster.cancel_arrangement("flight-none", "测试", at=T0)


class CommendationTest(unittest.TestCase):
    """任何人不能批准自己的奖励，表彰能解释包含或排除。"""

    def setUp(self) -> None:
        self.roster = base_roster()
        add_volunteer(self.roster, "vol-a", qualifications=("accreditation", "airport-ops"))
        add_volunteer(self.roster, "vol-b", qualifications=("accreditation",))
        self.roster.open_shift("s-done", "pos-badge", window("08T06:00:00", "08T14:00:00"), at=T0)
        self.roster.open_shift("s-nocheckin", "pos-badge", window("09T06:00:00", "09T14:00:00"), at=T0)
        self.roster.open_shift("s-cancelled", "pos-airport", window("10T04:00:00", "10T08:00:00"), arrangement_id="flight-1", at=T0)
        self.roster.open_shift("s-substituted", "pos-badge", window("11T06:00:00", "11T14:00:00"), at=T0)
        self.roster.open_shift("s-outside", "pos-badge", window("20T06:00:00", "20T14:00:00"), at=T0)
        for shift_id in ("s-done", "s-nocheckin", "s-cancelled", "s-substituted", "s-outside"):
            self.roster.assign(shift_id, "vol-a", at=T0)
        self.roster.record_checkin("s-done", "vol-a", at=at("08T05:50:00"))
        self.roster.start_shift("s-done", at=at("08T06:00:00"))
        self.roster.close_shift("s-done", at=at("08T14:00:00"))
        self.roster.start_shift("s-nocheckin", at=at("09T06:00:00"))
        self.roster.close_shift("s-nocheckin", at=at("09T14:00:00"))
        self.roster.cancel_arrangement("flight-1", "航班取消", at=at("09T18:00:00"))
        self.roster.request_substitution("sub-1", "s-substituted", "vol-a", "vol-b", "临时替班", at=at("10T20:00:00"))
        self.roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("10T20:10:00"))
        self.roster.confirm_substitution_by_supervisor("sub-1", "sup-1", at=at("10T20:20:00"))

    def test_self_approval_is_rejected(self) -> None:
        self.roster.register_reviewer("vol-a", "志愿者vol-a", at=T0)
        with self.assertRaisesRegex(RosterError, "不能批准自己的奖励"):
            self.roster.grant_commendation("c-1", "vol-a", PERIOD, "vol-a", at=at("13T09:00:00"))

    def test_unregistered_reviewer_is_rejected(self) -> None:
        with self.assertRaisesRegex(RosterError, "表彰审核人员未登记"):
            self.roster.grant_commendation("c-1", "vol-a", PERIOD, "vol-b", at=at("13T09:00:00"))

    def test_commendation_explains_included_and_excluded_segments(self) -> None:
        self.roster.grant_commendation("c-1", "vol-a", PERIOD, "rev-1", at=at("13T09:00:00"))
        commendation = self.roster.commendations["c-1"]
        self.assertEqual([segment.shift_id for segment in commendation.included], ["s-done"])
        reasons = {exclusion.shift_id: exclusion.reason for exclusion in commendation.excluded}
        self.assertEqual(reasons["s-nocheckin"], REASON_NO_CHECKIN)
        self.assertEqual(reasons["s-cancelled"], REASON_SHIFT_CANCELLED)
        self.assertEqual(reasons["s-substituted"], REASON_SUBSTITUTED)
        self.assertEqual(reasons["s-outside"], REASON_OUTSIDE_PERIOD)

        text = self.roster.explain_commendation("c-1")
        self.assertIn("s-done", text)
        self.assertIn("缺少签到记录", text)
        self.assertIn("班次已取消", text)
        self.assertIn("已由他人替班", text)
        self.assertIn("不在表彰周期内", text)
        self.assertIn("合计：8 小时", text)

        restored = Roster.recover(json.loads(json.dumps(self.roster.records, ensure_ascii=False)))
        self.assertEqual(restored.explain_commendation("c-1"), text)


class RecoveryTest(unittest.TestCase):
    """系统恢复后继续处理未审核的替班和即将开始的班次。"""

    def test_recovery_resumes_pending_substitutions_and_upcoming_shifts(self) -> None:
        roster = base_roster()
        roster.open_shift("s-1", "pos-badge", window("12T06:00:00", "12T14:00:00"), at=T0)
        roster.open_shift("s-2", "pos-badge", window("12T22:00:00", "13T06:00:00"), at=T0)
        add_volunteer(roster, "vol-a", qualifications=("accreditation",))
        add_volunteer(roster, "vol-b", qualifications=("accreditation",))
        roster.assign("s-1", "vol-a", at=T0)
        roster.request_substitution("sub-1", "s-1", "vol-a", "vol-b", "临时替班", at=at("11T08:00:00"))
        roster.confirm_substitution_by_owner("sub-1", "vol-a", at=at("11T08:30:00"))

        persisted = json.loads(json.dumps(roster.records, ensure_ascii=False))
        restored = Roster.recover(persisted)
        self.assertEqual(len(restored.records), len(roster.records))
        self.assertEqual([sub.request_id for sub in restored.pending_substitutions()], ["sub-1"])
        self.assertEqual([shift.shift_id for shift in restored.upcoming_shifts(at("11T12:00:00"))], ["s-1", "s-2"])

        restored.confirm_substitution_by_supervisor("sub-1", "sup-1", at=at("11T09:00:00"))
        self.assertEqual(restored.substitutions["sub-1"].state, SUBSTITUTION_APPLIED)
        self.assertEqual(restored.assignments[("s-1", "vol-b")].state, ASSIGNMENT_ACTIVE)
        self.assertEqual(restored.assignments[("s-1", "vol-a")].state, ASSIGNMENT_SUBSTITUTED)
        self.assertEqual(restored.records[-1]["kind"], "substitution_applied")


if __name__ == "__main__":
    unittest.main()
