"""排班、替班、签到与表彰记录链的业务规则测试。"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta

from src.roster import REST_INTERVAL, Roster, RosterError


def build_roster() -> Roster:
    """构造闭幕日前后的基础资料：四名志愿者、两类岗位。"""
    roster = Roster()
    roster.register_volunteer("v-lin", "林晓", "2026-09-01T09:00")
    roster.register_volunteer("v-qiao", "乔伊", "2026-09-01T09:05")
    roster.register_volunteer("v-ma", "马麟", "2026-09-01T09:10")
    roster.register_volunteer("v-he", "何笛", "2026-09-01T09:15")
    roster.grant_award_authority("sup-wang", "2026-09-02T10:00")

    roster.certify_training("v-lin", "接待", "2026-09-03T10:00")
    roster.certify_training("v-qiao", "接待", "2026-09-03T10:05")
    roster.certify_training("v-ma", "引导", "2026-09-03T10:10")
    roster.certify_training("v-he", "接待", "2026-09-03T10:15")

    roster.add_language("v-lin", "中文", "2026-09-04T10:00")
    roster.add_language("v-lin", "法语", "2026-09-04T10:01")
    roster.add_language("v-qiao", "中文", "2026-09-04T10:02")
    roster.add_language("v-qiao", "阿拉伯语", "2026-09-04T10:03")
    roster.add_language("v-ma", "中文", "2026-09-04T10:04")
    roster.add_language("v-he", "中文", "2026-09-04T10:05")
    roster.add_language("v-he", "法语", "2026-09-04T10:06")
    return roster


class RegistrationTest(unittest.TestCase):
    def test_duplicate_registration_and_empty_fields_rejected(self) -> None:
        roster = Roster()
        roster.register_volunteer("v1", "甲", "2026-09-01T09:00")
        with self.assertRaisesRegex(RosterError, "不能重复建档"):
            roster.register_volunteer("v1", "甲", "2026-09-01T09:01")
        with self.assertRaisesRegex(RosterError, "姓名不能为空"):
            roster.register_volunteer("v2", "  ", "2026-09-01T09:02")
        with self.assertRaisesRegex(RosterError, "语言能力已登记"):
            roster.add_language("v1", "中文", "2026-09-04T10:00")
            roster.add_language("v1", "中文", "2026-09-04T10:01")


class SchedulingRuleTest(unittest.TestCase):
    def test_qualification_language_capacity_and_rest_interval(self) -> None:
        roster = build_roster()
        roster.define_shift(
            "s-media", "媒体酒店", "接待",
            "2026-10-02T06:00", "2026-10-02T10:00",
            headcount=2, required_languages=["法语", "阿拉伯语"],
        )
        # 无培训合格记录不能上岗
        with self.assertRaisesRegex(RosterError, "岗位资质不符"):
            roster.assign("s-media", "v-ma", "2026-09-20T09:00")
        roster.assign("s-media", "v-lin", "2026-09-20T09:02")
        # 单语法语志愿者到岗后仍未就绪：阿拉伯语必须靠剩余名额补齐
        with self.assertRaisesRegex(RosterError, "阿拉伯语"):
            roster.validate_shift_ready("s-media")
        roster.assign("s-media", "v-qiao", "2026-09-20T09:01")
        roster.validate_shift_ready("s-media")
        # 场地人数上限
        with self.assertRaisesRegex(RosterError, "场地人数已满"):
            roster.assign("s-media", "v-he", "2026-09-20T09:03")
        # 同一人不能重复排班
        with self.assertRaisesRegex(RosterError, "不能在同一班次重复排班"):
            roster.assign("s-media", "v-lin", "2026-09-20T09:04")

        # 清晨班之后 10 小时内再排深夜/日间班：休息间隔不足
        roster.define_shift(
            "s-badge", "制证中心", "接待",
            "2026-10-02T18:00", "2026-10-02T22:00", headcount=1,
        )
        with self.assertRaisesRegex(RosterError, "休息间隔不足"):
            roster.assign("s-badge", "v-qiao", "2026-09-20T09:05")
        # 时间互斥
        roster.define_shift(
            "s-overlap", "媒体酒店", "接待",
            "2026-10-02T09:30", "2026-10-02T12:00", headcount=1,
        )
        with self.assertRaisesRegex(RosterError, "班次时间互斥"):
            roster.assign("s-overlap", "v-lin", "2026-09-20T09:06")
        # 满足间隔后可以排班
        roster.define_shift(
            "s-party", "告别派对", "接待",
            "2026-10-02T20:30", "2026-10-03T00:30", headcount=2,
        )
        roster.assign("s-party", "v-lin", "2026-09-20T09:07")
        self.assertEqual(roster.rest_interval, REST_INTERVAL)

    def test_release_cannot_break_language_coverage(self) -> None:
        roster = build_roster()
        roster.add_language("v-qiao", "西班牙语", "2026-09-04T10:07")
        roster.certify_training("v-ma", "接待", "2026-09-03T10:20")

        # 场景一：乔伊独掌阿语和西语，一个释放名额补不回两种语言
        roster.define_shift(
            "s-hotel", "媒体酒店", "接待",
            "2026-10-02T08:00", "2026-10-02T12:00",
            headcount=2, required_languages=["阿拉伯语", "西班牙语"],
        )
        roster.assign("s-hotel", "v-qiao", "2026-09-20T09:01")
        roster.assign("s-hotel", "v-lin", "2026-09-20T09:00")
        roster.validate_shift_ready("s-hotel")
        with self.assertRaisesRegex(RosterError, "语言将无人保障"):
            roster.release_assignment("s-hotel", "v-qiao", "航班调整", "2026-09-25T09:00")

        # 场景二：释放留出一个名额，但只会中文的人补不回法语缺口
        roster.define_shift(
            "s-airport", "机场", "接待",
            "2026-10-02T22:00", "2026-10-03T02:00",
            headcount=2, required_languages=["法语", "阿拉伯语"],
        )
        roster.assign("s-airport", "v-lin", "2026-09-20T09:02")
        roster.assign("s-airport", "v-qiao", "2026-09-20T09:03")
        roster.release_assignment("s-airport", "v-lin", "临时有事", "2026-09-25T09:01")
        with self.assertRaisesRegex(RosterError, "法语"):
            roster.assign("s-airport", "v-ma", "2026-09-25T09:02")
        roster.assign("s-airport", "v-he", "2026-09-25T09:03")
        roster.validate_shift_ready("s-airport")


class StartedShiftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = build_roster()
        self.roster.define_shift(
            "s-airport", "机场", "接待",
            "2026-10-02T08:00", "2026-10-02T12:00", headcount=1,
        )
        self.roster.assign("s-airport", "v-lin", "2026-09-20T09:00")

    def test_checkin_facts_cannot_be_rewritten(self) -> None:
        roster = self.roster
        roster.check_in("s-airport", "v-lin", "2026-10-02T08:05")
        with self.assertRaisesRegex(RosterError, "不能重复签到或回写"):
            roster.check_in("s-airport", "v-lin", "2026-10-02T08:20")
        with self.assertRaisesRegex(RosterError, "签退时间不能早于签到"):
            roster.check_out("s-airport", "v-lin", "2026-10-02T07:55")
        roster.check_out("s-airport", "v-lin", "2026-10-02T11:50")
        with self.assertRaisesRegex(RosterError, "签退事实已存在"):
            roster.check_out("s-airport", "v-lin", "2026-10-02T11:55")

    def test_started_shift_only_allows_exception_and_handover(self) -> None:
        roster = self.roster
        roster.check_in("s-airport", "v-lin", "2026-10-02T08:05")
        with self.assertRaisesRegex(RosterError, "只能追加异常和交接"):
            roster.assign("s-airport", "v-he", "2026-10-02T08:30")
        with self.assertRaisesRegex(RosterError, "只能追加异常和交接"):
            roster.release_assignment("s-airport", "v-lin", "身体不适", "2026-10-02T08:30")
        with self.assertRaisesRegex(RosterError, "只能追加异常和交接"):
            roster.cancel_requirement("s-airport", "航班取消", "2026-10-02T08:30")
        # 异常与交接可以追加，且不改变签到事实
        roster.add_exception(
            "s-airport", "v-lin", "2026-10-02T08:40", "临时引导延误的代表团", "sup-wang"
        )
        roster.record_handover(
            "s-airport", "v-lin", "v-he", "2026-10-02T10:00", "剩余旅客名单已交接"
        )
        record = roster.state.attendance[("s-airport", "v-lin")]
        self.assertTrue(record["in"].isoformat().startswith("2026-10-02T08:05"))
        self.assertEqual(len(record["exceptions"]), 1)
        self.assertEqual(len(record["handovers"]), 1)


class SubstitutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = build_roster()
        self.roster.define_shift(
            "s-badge", "制证中心", "接待",
            "2026-10-02T13:00", "2026-10-02T17:00",
            headcount=1, required_languages=["法语"],
        )
        self.roster.assign("s-badge", "v-lin", "2026-09-20T09:00")

    def test_both_confirmations_required_in_order(self) -> None:
        roster = self.roster
        roster.request_substitution(
            "sub-dup", "s-badge", "v-lin", "v-he", "家中急事", "2026-09-28T07:30"
        )
        with self.assertRaisesRegex(RosterError, "替班申请已存在"):
            roster.request_substitution(
                "sub-dup", "s-badge", "v-lin", "v-he", "家中急事", "2026-09-28T08:00"
            )
        roster.request_substitution(
            "sub-1", "s-badge", "v-lin", "v-he", "家中急事", "2026-09-28T08:00"
        )
        # 主管不能抢在原负责人确认之前审批
        with self.assertRaisesRegex(RosterError, "必须先有原负责人确认"):
            roster.approve_substitution("sub-1", "sup-wang", "2026-09-28T09:00")
        # 非本人不能替原负责人确认
        with self.assertRaisesRegex(RosterError, "原负责人本人确认"):
            roster.acknowledge_substitution("sub-1", "v-he", "2026-09-28T09:05")
        roster.acknowledge_substitution("sub-1", "v-lin", "2026-09-28T09:10")
        # 主管不能是替班当事人
        with self.assertRaisesRegex(RosterError, "主管不能是替班当事人"):
            roster.approve_substitution("sub-1", "v-he", "2026-09-28T09:15")
        roster.approve_substitution("sub-1", "sup-wang", "2026-09-28T09:20")
        self.assertEqual(roster.state.substitutions["sub-1"]["status"], "effective")
        self.assertNotIn(
            ("s-badge", "v-lin"),
            {k for k, a in roster.state.assignments.items() if a["status"] == "active"},
        )

    def test_substitute_must_satisfy_qualification_language_and_rest(self) -> None:
        roster = build_roster()
        roster.define_shift(
            "s-morning", "媒体酒店", "接待",
            "2026-10-02T06:00", "2026-10-02T10:00",
            headcount=2, required_languages=["法语", "阿拉伯语"],
        )
        roster.assign("s-morning", "v-lin", "2026-09-20T09:00")
        roster.assign("s-morning", "v-qiao", "2026-09-20T09:01")
        # v-ma 无接待资质
        with self.assertRaisesRegex(RosterError, "岗位资质不符"):
            roster.request_substitution(
                "sub-x", "s-morning", "v-lin", "v-ma", "替班", "2026-09-28T08:00"
            )
        # v-he 会法语但不会阿拉伯语，替换乔伊后语言缺口
        with self.assertRaisesRegex(RosterError, "阿拉伯语"):
            roster.request_substitution(
                "sub-x", "s-morning", "v-qiao", "v-he", "替班", "2026-09-28T08:01"
            )
        # 替换林晓（法语岗）可行：v-he 也会法语
        roster.request_substitution(
            "sub-1", "s-morning", "v-lin", "v-he", "替班", "2026-09-28T08:02"
        )
        roster.acknowledge_substitution("sub-1", "v-lin", "2026-09-28T08:12")
        roster.approve_substitution("sub-1", "sup-wang", "2026-09-28T08:22")
        # v-he 清晨刚下班，休息不足不能再进紧邻的班
        roster.define_shift(
            "s-noon", "制证中心", "接待",
            "2026-10-02T13:00", "2026-10-02T17:00", headcount=1,
        )
        # 林晓已从清晨班转出，由她担任下午班原负责人不冲突
        roster.assign("s-noon", "v-lin", "2026-09-20T09:02")
        with self.assertRaisesRegex(RosterError, "休息间隔不足"):
            roster.request_substitution(
                "sub-2", "s-noon", "v-lin", "v-he", "连轴转", "2026-09-28T08:03"
            )

    def test_approval_after_shift_start_is_refused(self) -> None:
        roster = self.roster
        roster.request_substitution(
            "sub-late", "s-badge", "v-lin", "v-he", "堵在路上", "2026-10-02T12:30"
        )
        roster.acknowledge_substitution("sub-late", "v-lin", "2026-10-02T12:35")
        roster.check_in("s-badge", "v-lin", "2026-10-02T13:02")
        with self.assertRaisesRegex(RosterError, "只能追加异常和交接"):
            roster.approve_substitution("sub-late", "sup-wang", "2026-10-02T13:10")

    def test_service_time_follows_the_actual_server(self) -> None:
        roster = self.roster
        roster.request_substitution(
            "sub-1", "s-badge", "v-lin", "v-he", "家中急事", "2026-09-28T08:00"
        )
        roster.acknowledge_substitution("sub-1", "v-lin", "2026-09-28T08:10")
        approval = roster.approve_substitution("sub-1", "sup-wang", "2026-09-28T08:20")
        # 替班人签到签退，时长归替班人；原负责人该段标记为转出
        roster.check_in("s-badge", "v-he", "2026-10-02T13:00")
        roster.check_out("s-badge", "v-he", "2026-10-02T17:00")
        after = "2026-10-02T18:00"
        he = roster.service_periods("v-he", after)[0]
        self.assertTrue(he["included"])
        self.assertEqual(he["served_minutes"], 240)
        self.assertIn(approval["seq"], he["evidence"])

        lin = roster.service_periods("v-lin", after)[0]
        self.assertFalse(lin["included"])
        self.assertIn("替班已转出", "；".join(lin["reasons"]))
        self.assertIn(approval["seq"], lin["evidence"])


class PartialCancellationTest(unittest.TestCase):
    def test_cancelling_one_service_releases_only_its_slots(self) -> None:
        roster = build_roster()
        roster.define_shift(
            "s-airport", "机场", "接待",
            "2026-10-02T08:00", "2026-10-02T12:00", headcount=1,
        )
        roster.define_shift(
            "s-hotel", "媒体酒店", "引导",
            "2026-10-02T08:00", "2026-10-02T12:00", headcount=1,
        )
        roster.assign("s-airport", "v-lin", "2026-09-20T09:00")
        roster.assign("s-hotel", "v-ma", "2026-09-20T09:01")
        roster.cancel_requirement("s-airport", "机场接送临时取消", "2026-10-01T18:00")
        # 机场名额释放：林晓可改排他班；酒店岗位原样保留
        self.assertEqual(
            roster.state.assignments[("s-airport", "v-lin")]["status"], "released"
        )
        self.assertEqual(
            roster.state.assignments[("s-hotel", "v-ma")]["status"], "active"
        )
        with self.assertRaisesRegex(RosterError, "班次需求已取消"):
            roster.assign("s-airport", "v-he", "2026-10-01T18:30")
        periods = roster.service_periods("v-lin", "2026-10-02T18:00")[0]
        self.assertFalse(periods["included"])
        self.assertIn("班次需求已取消", periods["reasons"])


class AwardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = build_roster()
        self.roster.define_shift(
            "s-party", "告别派对", "接待",
            "2026-10-02T20:00", "2026-10-03T00:00", headcount=1,
        )
        self.roster.assign("s-party", "v-lin", "2026-09-20T09:00")

    def test_no_self_approval_and_authority_required(self) -> None:
        roster = self.roster
        with self.assertRaisesRegex(RosterError, "不能批准自己的奖励"):
            roster.decide_award("a1", "v-lin", "v-lin", "2026-10-03T01:00")
        with self.assertRaisesRegex(RosterError, "不具备表彰审核权限"):
            roster.decide_award("a1", "v-lin", "v-qiao", "2026-10-03T01:00")

    def test_award_includes_and_excludes_periods_with_reasons(self) -> None:
        roster = self.roster
        # 第一段：闭环，计入 240 分钟
        roster.check_in("s-party", "v-lin", "2026-10-02T20:00")
        roster.check_out("s-party", "v-lin", "2026-10-03T00:00")
        # 第二段：未签到，排除
        roster.define_shift(
            "s-airport", "机场", "接待",
            "2026-10-01T08:00", "2026-10-01T12:00", headcount=1,
        )
        roster.assign("s-airport", "v-lin", "2026-09-21T09:00")
        award = roster.decide_award(
            "award-lin", "v-lin", "sup-wang", "2026-10-03T02:00", min_minutes=200
        )
        self.assertTrue(award["granted"])
        self.assertEqual(award["total_minutes"], 240)
        explanation = roster.award_explanation("award-lin")
        by_shift = {p["shift_id"]: p for p in explanation["periods"]}
        self.assertTrue(by_shift["s-party"]["included"])
        self.assertFalse(by_shift["s-airport"]["included"])
        self.assertIn("已排班但未签到", by_shift["s-airport"]["reasons"])
        # 证据链可追到具体事件序号
        self.assertTrue(all(by_shift["s-party"]["evidence"]))

        # 缺签退导致时长未闭环：不满足门槛则不表彰
        roster2 = build_roster()
        roster2.define_shift(
            "x", "制证中心", "接待",
            "2026-10-02T13:00", "2026-10-02T17:00", headcount=1,
        )
        roster2.assign("x", "v-he", "2026-09-20T09:00")
        roster2.check_in("x", "v-he", "2026-10-02T13:00")
        denied = roster2.decide_award(
            "a-he", "v-he", "sup-wang", "2026-10-02T18:00", min_minutes=60
        )
        self.assertFalse(denied["granted"])
        self.assertIn("缺少签退", "；".join(denied["periods"][0]["reasons"]))


class RecoveryTest(unittest.TestCase):
    def test_rebuild_from_event_log_resumes_pending_work(self) -> None:
        roster = build_roster()
        roster.define_shift(
            "s-badge", "制证中心", "接待",
            "2026-10-02T13:00", "2026-10-02T17:00",
            headcount=2, required_languages=["法语"],
        )
        roster.assign("s-badge", "v-lin", "2026-09-20T09:00")
        roster.request_substitution(
            "sub-1", "s-badge", "v-lin", "v-he", "生病", "2026-10-02T11:00"
        )
        # 系统宕机：仅保留事件日志，恢复后原样重放
        raw = roster.to_json()
        restored = Roster.from_events(json.loads(raw))

        pending = restored.pending_substitutions()
        self.assertEqual([p["substitution_id"] for p in pending], ["sub-1"])
        self.assertIn(
            "s-badge",
            restored.upcoming_shifts("2026-10-02T11:30", timedelta(hours=2)),
        )
        # 已开始（但未签到）班不再列为"即将开始"，且审批按已开始拒绝
        self.assertNotIn(
            "s-badge",
            restored.upcoming_shifts("2026-10-02T13:30", timedelta(hours=2)),
        )
        # 续办：原负责人确认 + 主管审批（此时未到 13:00）
        restored.acknowledge_substitution("sub-1", "v-lin", "2026-10-02T11:35")
        restored.approve_substitution("sub-1", "sup-wang", "2026-10-02T11:40")
        self.assertEqual(restored.state.substitutions["sub-1"]["status"], "effective")
        # 再次重放结果与续办后的日志一致
        self.assertEqual(
            [e["type"] for e in restored.events],
            [e["type"] for e in Roster.from_events(json.loads(restored.to_json())).events],
        )

    def test_facts_survive_restart_and_cannot_be_mutated(self) -> None:
        roster = build_roster()
        roster.define_shift(
            "s-airport", "机场", "接待",
            "2026-10-02T08:00", "2026-10-02T12:00", headcount=1,
        )
        roster.assign("s-airport", "v-lin", "2026-09-20T09:00")
        roster.check_in("s-airport", "v-lin", "2026-10-02T08:03")
        restored = Roster.from_events(json.loads(roster.to_json()))
        with self.assertRaisesRegex(RosterError, "不能重复签到或回写"):
            restored.check_in("s-airport", "v-lin", "2026-10-02T08:10")


if __name__ == "__main__":
    unittest.main()
