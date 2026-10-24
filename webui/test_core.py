# -*- coding: utf-8 -*-
"""core.py 的单测。日志文本是从本机 UV4 真实输出里抓的，不是编的。

跑法： cd Desktop\\单片微机\\webui && python -m unittest -v test_core
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

# 真实抓取：C51 编译错误（uv4_rc=2）
C51_ERRORS = """Rebuild target 'Target 1'
assembling STARTUP.A51...
compiling main.c...
main.c(7): error C141: syntax error near 'undeclared_var'
main.c(7): error C202: 'undeclared_var': undefined identifier
Target not created.
Build Time Elapsed:  00:00:01
"""

# 真实抓取：A51 汇编错误（uv4_rc=2）
A51_ERRORS = """Rebuild target 'Target 1'
assembling P2.a51...
P2.a51(5): error A45: UNDEFINED SYMBOL (PASS-2)
P2.a51(7): error A9: SYNTAX ERROR
Target not created.
Build Time Elapsed:  00:00:00
"""

# 真实抓取：0 error 但链接器未解决外部符号 + 编译警告（uv4_rc=1，hex 照样生成）
LINK_DANGER = """Rebuild target 'Target 1'
assembling STARTUP.A51...
compiling main.c...
main.c(5): warning C280: 'unused_local': unreferenced local variable
linking...
*** WARNING L1: UNRESOLVED EXTERNAL SYMBOL
    SYMBOL:  NEVER_DEFINED
    MODULE:  .\\Objects\\main.obj (MAIN)

*** WARNING L2: REFERENCE MADE TO UNRESOLVED EXTERNAL
    SYMBOL:  NEVER_DEFINED
    MODULE:  .\\Objects\\main.obj (MAIN)
    ADDRESS: 002CH

Program Size: data=12.0 xdata=0 code=67
creating hex file from ".\\Objects\\blink"...
".\\Objects\\blink" - 0 Error(s), 3 Warning(s).
Build Time Elapsed:  00:00:00
"""

CLEAN_BUILD = """Build target 'Target 1'
compiling main.c...
linking...
Program Size: data=9.0 xdata=0 code=76
creating hex file from ".\\Objects\\blink"...
".\\Objects\\blink" - 0 Error(s), 0 Warning(s).
Build Time Elapsed:  00:00:02
"""

GOOD_HEX = """:03000000020040BB
:0C004000787FE4F6D8FD758107020023EC
:00000001FF
"""

# 今天真的踩过的坑：STARTUP 没进编译分组，hex 从 0x0003 开始，0x0000 是空的
NO_RESET_HEX = """:10000300E4E4E4E4E4E4E4E4E4E4E4E4E4E4E4E4AD
:00000001FF
"""


class TestBuildLog(unittest.TestCase):
    def test_c51_errors_are_located(self):
        r = core.parse_build_log(C51_ERRORS)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["errors"]), 2)
        self.assertEqual(r["errors"][0]["file"], "main.c")
        self.assertEqual(r["errors"][0]["line"], 7)
        self.assertEqual(r["errors"][0]["code"], "C141")
        self.assertEqual(r["totals"]["errors"], 2)

    def test_a51_errors_are_located(self):
        r = core.parse_build_log(A51_ERRORS)
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["file"], "P2.a51")
        self.assertEqual(r["errors"][0]["line"], 5)
        self.assertEqual(r["errors"][0]["code"], "A45")

    def test_linker_warning_block_is_aggregated(self):
        r = core.parse_build_log(LINK_DANGER)
        l1 = [d for d in r["warnings"] if d["code"] == "L1"]
        self.assertEqual(len(l1), 1, "多行链接警告应该合成一条，不该拆成三条孤儿行")
        self.assertIn("NEVER_DEFINED", l1[0]["detail"])
        self.assertEqual(l1[0]["file"], "main.c", "链接器块没有文件名，应回退到当前编译单元")

    def test_unresolved_external_is_flagged_as_danger(self):
        """0 Error 但板子不动的最常见原因，必须单独拎出来报警。"""
        r = core.parse_build_log(LINK_DANGER)
        self.assertTrue(r["ok"], "按 Error 数它确实是成功的")
        self.assertEqual(len(r["danger_warnings"]), 2)
        self.assertEqual(r["program_size"], {"data": 12, "data_bits": 0, "xdata": 0, "code": 67})

    def test_clean_build(self):
        r = core.parse_build_log(CLEAN_BUILD)
        self.assertTrue(r["ok"])
        self.assertEqual(r["diagnostics"], [])
        self.assertEqual(r["program_size"]["code"], 76)


class TestHex(unittest.TestCase):
    def test_good_hex(self):
        h = core.parse_hex(GOOD_HEX)
        self.assertTrue(h["ok"], h["problems"])
        self.assertTrue(h["has_reset_vector"])
        self.assertEqual(h["entry"], 0x0040, "LJMP 目标应解析成 0x0040（STARTUP 入口）")
        self.assertEqual(h["bytes"], 15)

    def test_missing_reset_vector_is_reported(self):
        h = core.parse_hex(NO_RESET_HEX)
        self.assertFalse(h["ok"])
        self.assertFalse(h["has_reset_vector"])
        self.assertTrue(any("复位向量" in p for p in h["problems"]))

    def test_bad_checksum(self):
        h = core.parse_hex(":03000000020040FF\n:00000001FF\n")
        self.assertFalse(h["ok"])
        self.assertTrue(any("校验和" in p for p in h["problems"]))

    def test_missing_eof(self):
        h = core.parse_hex(":03000000020040BB\n")
        self.assertTrue(any("结束记录" in p for p in h["problems"]))

    def test_empty_hex(self):
        h = core.parse_hex(":00000001FF\n")
        self.assertTrue(any("空的" in p for p in h["problems"]))


class TestCurve(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(core.parse_curve_line("v: 42"), {"values": [42.0]})

    def test_multi_channel(self):
        self.assertEqual(core.parse_curve_line("v: 1.5,-2"), {"values": [1.5, -2.0]})

    def test_plain_text_is_not_a_curve(self):
        self.assertIsNone(core.parse_curve_line("hello world"))
        self.assertIsNone(core.parse_curve_line(""))
        self.assertIsNone(core.parse_curve_line("# comment"))
        self.assertIsNone(core.parse_curve_line("v: abc"))


class TestFlashFailure(unittest.TestCase):
    def test_port_unavailable(self):
        cause, _ = core.classify_flash_failure(1, "could not open port 'COM7': FileNotFoundError")
        self.assertEqual(cause, "port_unavailable")

    def test_no_handshake(self):
        cause, hint = core.classify_flash_failure(124, "Waiting for MCU, please cycle power: ")
        self.assertEqual(cause, "no_handshake")
        self.assertIn("断", hint)

    def test_dtr_is_never_the_answer(self):
        """这块板的 DTR 复位无效是实测结论，提示里不许再建议 -a。"""
        for rc, text in [(124, "Waiting for MCU, please cycle power:"), (1, "programmer is not responding")]:
            _, hint = core.classify_flash_failure(rc, text)
            self.assertNotIn("-a", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
