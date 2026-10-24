# -*- coding: utf-8 -*-
"""ai_rules 的单测。夹具直接复用 test_core 里从本机 UV4 / stcgal 抓的真实输出。

跑法： cd Desktop\\单片微机\\webui && python -m unittest -v test_ai_rules
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_rules  # noqa: E402
import core  # noqa: E402
from test_core import (C51_ERRORS, A51_ERRORS, LINK_DANGER, CLEAN_BUILD,  # noqa: E402
                       GOOD_HEX, NO_RESET_HEX)


def ids(findings):
    return [f["rule_id"] for f in findings]


class TestRulesFromRealLogs(unittest.TestCase):
    def test_unresolved_external_is_the_headline(self):
        """0 Error 3 Warning 的那份日志，必须把 L1/L2 拎成头号结论，而不是当成成功。"""
        parsed = core.parse_build_log(LINK_DANGER)
        self.assertTrue(parsed["ok"])
        got = ai_rules.explain({"diagnostics": parsed["diagnostics"]})
        self.assertIn("unresolved-external", ids(got))
        hit = [f for f in got if f["rule_id"] == "unresolved-external"][0]
        self.assertTrue(any("NEVER_DEFINED" in e for e in hit["evidence"]),
                        "证据里要能看到具体符号，不然用户不知道该查哪个名字")

    def test_c51_error_pair_maps_to_one_rule(self):
        got = ai_rules.explain({"diagnostics": core.parse_build_log(C51_ERRORS)["diagnostics"]})
        self.assertIn("undefined-identifier", ids(got))
        self.assertNotIn("unresolved-external", ids(got))

    def test_a51_errors(self):
        got = ai_rules.explain({"diagnostics": core.parse_build_log(A51_ERRORS)["diagnostics"]})
        self.assertIn("asm-undefined", ids(got))

    def test_clean_build_hits_nothing(self):
        parsed = core.parse_build_log(CLEAN_BUILD)
        got = ai_rules.explain({"diagnostics": parsed["diagnostics"],
                                "program_size": parsed["program_size"],
                                "hex": core.parse_hex(GOOD_HEX)})
        self.assertEqual(got, [], "干净编译 + 健康 hex 不该硬凑结论")

    def test_missing_reset_vector_does_not_double_report(self):
        """复位向量那条已经够具体了，别再叠一条泛泛的「hex 有问题」把重点冲淡。"""
        h = core.parse_hex(NO_RESET_HEX)
        got = ai_rules.explain({"diagnostics": [], "hex": h})
        self.assertIn("no-reset-vector", ids(got))
        self.assertNotIn("hex-broken", ids(got))

    def test_other_hex_problem_uses_generic_rule(self):
        h = core.parse_hex(":03000000020040FF\n:00000001FF\n")   # 校验和错，复位向量本身是在的
        got = ai_rules.explain({"diagnostics": [], "hex": h})
        self.assertIn("hex-broken", ids(got))
        self.assertNotIn("no-reset-vector", ids(got))

    def test_data_size_threshold(self):
        big = {"data": 112, "data_bits": 0, "xdata": 0, "code": 900}
        small = {"data": 9, "data_bits": 0, "xdata": 0, "code": 76}
        self.assertIn("data-over", ids(ai_rules.explain({"diagnostics": [], "program_size": big})))
        self.assertNotIn("data-over", ids(ai_rules.explain({"diagnostics": [], "program_size": small})))

    def test_flash_causes_split_into_two_rules(self):
        got = ai_rules.explain({"diagnostics": [], "flash": {"cause": "no_handshake"}})
        self.assertIn("no-handshake", ids(got))
        got2 = ai_rules.explain({"diagnostics": [], "flash": {"cause": "port_unavailable"}})
        self.assertIn("port-busy", ids(got2))
        self.assertNotIn("no-handshake", ids(got2))

    def test_garbage_serial_line(self):
        got = ai_rules.explain({"diagnostics": [], "serial_tail": ["þ÷Ø\u0000\u0001\u0002\u0003\u0004"]})
        self.assertIn("garbage-uart", ids(got))
        self.assertNotIn("garbage-uart", ids(ai_rules.explain(
            {"diagnostics": [], "serial_tail": ["v: 3,17"]}, )))

    def test_p1_gpio_hint(self):
        got = ai_rules.explain({"diagnostics": [], "has_p1_gpio": True})
        self.assertIn("led-port", ids(got))


class TestQuestionMatching(unittest.TestCase):
    def test_keyword_routes(self):
        self.assertIn("no-handshake", ai_rules.match_question("为什么老是烧不进去"))
        self.assertIn("garbage-uart", ai_rules.match_question("串口全是乱码"))
        self.assertIn("unresolved-external", ai_rules.match_question("编译 0 error 但板子不动"))

    def test_no_nonsense_match(self):
        self.assertEqual(ai_rules.match_question("今天天气怎么样"), [],
                        "对不上就该空着走求助单，不能硬套一条规则糊弄")

    def test_every_returned_id_exists(self):
        known = set(ai_rules.RULE_INDEX)
        for q in ("乱码", "烧不进去", "data 不够", "a45", "跑飞"):
            for rid in ai_rules.match_question(q):
                self.assertIn(rid, known)


class TestTickets(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aiq")
        self.q = ai_rules.Tickets(archive_dir=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_rule_findings_answer_immediately(self):
        t = self.q.ask("板子不动", {"project": "x"}, findings=[{"rule_id": "unresolved-external",
                                                               "title": "t", "evidence": ["e"], "fix": []}])
        self.assertEqual(t["status"], "answered")
        self.assertEqual(t["answered_by"], "rule")
        self.assertEqual(self.q.pending(), [])

    def test_force_opens_a_ticket_even_when_rules_matched(self):
        t = self.q.ask("板子不动", {}, findings=[{"rule_id": "x"}], force=True)
        self.assertEqual(t["status"], "pending")

    def test_no_findings_stays_pending(self):
        t = self.q.ask("这个波形为什么周期不对", {})
        self.assertEqual(t["status"], "pending")
        self.assertIsNone(t["answer"])
        self.assertEqual([x["id"] for x in self.q.pending()], [t["id"]])

    def test_answer_flow(self):
        t = self.q.ask("问题", {})
        done = self.q.answer(t["id"], "去看 T0 的重装载值", by="qoder")
        self.assertEqual(done["status"], "answered")
        self.assertEqual(done["answered_by"], "qoder")
        self.assertEqual(done["answer"], "去看 T0 的重装载值")
        self.assertEqual(self.q.pending(), [])

    def test_answer_unknown_id(self):
        self.assertIsNone(self.q.answer("T999", "x"))

    def test_ids_unique_and_eviction(self):
        q = ai_rules.Tickets(limit=3)
        made = [q.ask("q%d" % i, {}) for i in range(5)]
        self.assertEqual(len(set(t["id"] for t in made)), 5)
        self.assertEqual(len(q.all_()), 3, "超上限要丢最老的，别把内存吃满")

    def test_archive_written_and_updated(self):
        t = self.q.ask("烧不进去", {"flash": {"cause": "no_handshake"}}, findings=[])
        path = os.path.join(self.dir, "%s.md" % t["id"])
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("等 Qoder 回答", text)
        self.assertIn("no_handshake", text)
        self.q.answer(t["id"], "断电重上电那一下要在倒计时里做")
        with open(path, encoding="utf-8") as f:
            text2 = f.read()
        self.assertIn("已回答（qoder）", text2)
        self.assertIn("倒计时", text2)

    def test_pending_survives_a_restart(self):
        """服务重启后「待回答」必须还在页面上——这是反向桥的前提，不然用户会以为答案丢了。"""
        t = self.q.ask("曲线能不能导出", {"project": "p"})
        self.q.ask("规则能答的", {}, findings=[{"rule_id": "unresolved-external", "title": "t"}])
        reopened = ai_rules.Tickets(archive_dir=self.dir)
        self.assertEqual([x["id"] for x in reopened.pending()], [t["id"]])
        self.assertEqual(len(reopened.all_()), 2)
        self.assertEqual(reopened.ask("下一张", {})["id"], "T003", "编号要接着存档走，别回头覆盖旧单")

    def test_broken_index_falls_back_to_empty(self):
        with open(os.path.join(self.dir, "tickets.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        q = ai_rules.Tickets(archive_dir=self.dir)
        self.assertEqual(q.all_(), [])
        self.assertEqual(q.ask("问题", {})["id"], "T001")

    def test_only_md_archive_still_continues_numbering(self):
        """上一版只写 md：读不到 json 时也不能从 T001 重来，那会把旧存档盖掉。"""
        with open(os.path.join(self.dir, "T017.md"), "w", encoding="utf-8") as f:
            f.write("# 求助单 T017\n")
        q = ai_rules.Tickets(archive_dir=self.dir)
        self.assertEqual(q.all_(), [])
        self.assertEqual(q.ask("问题", {})["id"], "T018")

    def test_archive_failure_does_not_break_ask(self):
        q = ai_rules.Tickets(archive_dir=os.path.join(self.dir, "no", "such", "deep"))
        os.makedirs(os.path.join(self.dir, "no"), exist_ok=True)
        # 把中间层做成一个文件，mkdir 必然失败——ask 不能因此炸掉
        with open(os.path.join(self.dir, "no", "such"), "w"):
            pass
        t = q.ask("问题", {})
        self.assertEqual(t["status"], "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
