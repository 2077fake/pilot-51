# -*- coding: utf-8 -*-
"""server.py 里调试助手那一层的接线测试（不联网、不碰硬件，只喂假状态）。

诊断文本复用 test_core 的夹具——那些是从本机 UV4 真实输出抓的，不在这里重编。
跑法： cd Desktop\\单片微机\\webui && python -m unittest -v test_server_ai
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core            # noqa: E402
import ai_rules        # noqa: E402
import server          # noqa: E402
from test_core import LINK_DANGER, C51_ERRORS  # noqa: E402


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, topic, **payload):
        event = dict(payload, topic=topic)
        self.events.append(event)
        return event

    def topics(self, topic):
        return [e for e in self.events if e["topic"] == topic]


class FakeSerial:
    def __init__(self, tail=()):
        self.tail = list(tail)


class FakeState:
    """只放 ai_context / api_ai_ask 会读到的那几样东西。"""

    def __init__(self, archive_dir=None):
        self.bus = FakeBus()
        self.serial = FakeSerial()
        self.ai = ai_rules.Tickets(archive_dir=archive_dir)
        self.last_build = None
        self.last_flash = None


def build_state(log_text=None, project=None, archive_dir=None):
    st = FakeState(archive_dir)
    if log_text is not None:
        parsed = core.parse_build_log(log_text)
        parsed["project"] = project
        st.last_build = parsed
    return st


class TestAiContext(unittest.TestCase):
    def test_nothing_faked_when_no_build_yet(self):
        """没编译过就不能假装看过：diagnostics 必须是空的，而不是 None 崩掉规则层。"""
        ctx = server.ai_context(FakeState())
        self.assertEqual(ctx["diagnostics"], [])
        self.assertEqual(ctx["serial_tail"], [])
        self.assertIsNone(ctx["totals"])
        self.assertNotIn("flash", ctx)

    def test_serial_tail_flows_in(self):
        st = build_state()
        st.serial.tail = ["v: 1,2", "乱码\x01\x02\x03"]
        self.assertEqual(server.ai_context(st)["serial_tail"], ["v: 1,2", "乱码\x01\x02\x03"])

    def test_p1_gpio_smell(self):
        self.assertTrue(server.ai_context(build_state(), source="P1 = 0xFE;")["has_p1_gpio"])
        self.assertTrue(server.ai_context(build_state(), source="sbit LED = P1^0;")["has_p1_gpio"])
        # 别把 RP1 / P1_ / 0xP1 这种也算进去
        self.assertFalse(server.ai_context(build_state(), source="int RP1 = 1; P1_LOW = 2;")["has_p1_gpio"])
        self.assertFalse(server.ai_context(build_state(), source="LED = ~(0x01<<k);")["has_p1_gpio"])

    def test_flash_attempts_are_echoed_for_the_archive(self):
        st = build_state()
        st.last_flash = {"ok": False, "cause": "no_handshake", "attempts": 7}
        ctx = server.ai_context(st)
        self.assertEqual(ctx["attempts"], 7)
        self.assertEqual(ctx["flash"]["cause"], "no_handshake")


class TestAskRoute(unittest.TestCase):
    def test_rules_answer_without_a_ticket_wait(self):
        st = build_state(LINK_DANGER)
        r = server.api_ai_ask(st, {}, lambda p: p)
        self.assertEqual(r["origin"], "explain")
        self.assertIn("unresolved-external", [f["rule_id"] for f in r["findings"]])
        self.assertEqual(r["ticket"]["status"], "answered")
        self.assertEqual(r["ticket"]["answered_by"], "rule")
        self.assertEqual(r["pending"], 0)
        self.assertEqual(st.bus.topics("ai")[0]["kind"], "ask")

    def test_public_ticket_drops_the_source_blob(self):
        st = build_state(C51_ERRORS)
        r = server.api_ai_ask(st, {"source": "int main(void) { P1 = 0; }"}, lambda p: p)
        self.assertNotIn("source", r["ticket"]["context"])
        self.assertIn("undefined-identifier", [f["rule_id"] for f in r["findings"]])

    def test_unmatched_question_stays_pending_and_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = FakeState(archive_dir=tmp)
            r = server.api_ai_ask(st, {"question": "我想把曲线导出成 csv"}, lambda p: p)
            self.assertEqual(r["origin"], "none")
            self.assertEqual(r["ticket"]["status"], "pending")
            self.assertEqual(r["findings"], [])
            path = os.path.join(tmp, r["ticket"]["id"] + ".md")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                md = f.read()
            self.assertIn("等 Qoder 回答", md)
            self.assertIn("还没有人回答", md)

    def test_archive_markdown_stays_readable(self):
        """存档是给人和 Qoder 在编辑器里读的：不能隔行空，也不能把 None 印进定位串。"""
        with tempfile.TemporaryDirectory() as tmp:
            st = build_state(LINK_DANGER, archive_dir=tmp)
            r = server.api_ai_ask(st, {"question": "灯不动",
                                       "source": "int main(void) {\r\n  uart_put();\r\n}\r\n"}, lambda p: p)
            with open(os.path.join(tmp, r["ticket"]["id"] + ".md"), "rb") as f:
                raw = f.read().decode("utf-8")
            self.assertNotIn("\r", raw)
            self.assertNotIn("None", raw)

    def test_force_still_files_a_ticket_even_when_rules_can_answer(self):
        """用户点名要问 Qoder：规则答案照样给，但单子得挂着等真人回答。"""
        st = build_state(LINK_DANGER)
        r = server.api_ai_ask(st, {"question": "0 error 但灯不动", "force": True}, lambda p: p)
        self.assertEqual(r["ticket"]["status"], "pending")
        self.assertTrue(r["findings"], "规则命中过就该一起显示，别为了问 Qoder 把已有结论藏起来")
        self.assertEqual(r["pending"], 1)

    def test_bad_include_list_does_not_crash(self):
        st = build_state(LINK_DANGER)
        r = server.api_ai_ask(st, {"include": ["nope"]}, lambda p: p)
        self.assertEqual(r["findings"], [], "没要 diagnostics 就不该凭空报规则")


class TestAnswerRoute(unittest.TestCase):
    def test_unknown_id_is_none(self):
        st = build_state()
        self.assertIsNone(server.api_ai_answer(st, {"id": "T404", "text": "x"}))

    def test_empty_text_is_rejected(self):
        st = build_state()
        with self.assertRaises(ValueError):
            server.api_ai_answer(st, {"id": "T001", "text": "   "})

    def test_answer_moves_ticket_and_pushes_sse(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = FakeState(archive_dir=tmp)
            tid = server.api_ai_ask(st, {"question": "串口只有首行对"}, lambda p: p)["ticket"]["id"]
            r = server.api_ai_answer(st, {"id": tid, "text": "波特率重装值用 0xFD"})
            self.assertEqual(r["ticket"]["status"], "answered")
            self.assertEqual(r["ticket"]["answered_by"], "qoder")
            self.assertEqual(r["pending"], 0)
            self.assertEqual(st.bus.topics("ai")[-1]["kind"], "answer")
            with open(os.path.join(tmp, tid + ".md"), encoding="utf-8") as f:
                self.assertIn("波特率重装值用 0xFD", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
