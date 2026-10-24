# -*- coding: utf-8 -*-
"""调试台的纯逻辑层：不碰硬件、不碰网络，可离线单测。

日志格式来自本机 UV4 真实输出（C51 编译器 / A51 汇编器 / 链接器三种写法都不同）。
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import build_flash  # noqa: E402  复用 find_uv4 / hex_path_of / clean_output

DIAG_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)\):\s+(?P<sev>error|warning)\s+(?P<code>[A-Z]\d+):\s+(?P<msg>.*)$")
LINK_RE = re.compile(r"^\*\*\*\s+(?P<sev>ERROR|WARNING)\s+(?P<code>L\d+):\s*(?P<msg>.*)$")
DETAIL_RE = re.compile(r"^\s{4}(?P<key>SYMBOL|MODULE|ADDRESS|PAGE):\s*(?P<val>.*)$")
SIZE_RE = re.compile(r"Program Size:\s*data=(?P<data>\d+)\.(?P<dbits>\d+)\s+xdata=(?P<xdata>\d+)\s+code=(?P<code>\d+)")
TOTAL_RE = re.compile(r'-\s*(?P<errors>\d+)\s+Error\(s\),\s*(?P<warnings>\d+)\s+Warning\(s\)\.')
STEP_RE = re.compile(r"^(?:assembling|compiling)\s+(?P<file>\S+)", re.I)
DANGER_LINK = ("L1", "L2")  # 未解决外部符号：链接能过、烧进去必跑飞

HEX_REC_RE = re.compile(r"^:([0-9A-Fa-f]{2})([0-9A-Fa-f]{4})([0-9A-Fa-f]{2})([0-9A-Fa-f]*)([0-9A-Fa-f]{2})$")


def decode_log(raw):
    """Keil 日志是 GBK，别按 UTF-8 硬解——替换符会让上层显示直接崩。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace").replace("\ufffd", "")


def parse_build_log(text):
    """把 UV4 日志拆成结构化诊断 + 产物摘要。"""
    diags = []
    size = None
    totals = None
    current = None
    failed = False
    tail = None          # 正在攒链接器的多行块
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            tail = None
            continue
        m = DIAG_RE.match(line)
        if m:
            diags.append({"severity": m.group("sev").lower(), "code": m.group("code"),
                          "file": os.path.basename(m.group("file")), "line": int(m.group("line")),
                          "message": m.group("msg").strip(), "detail": ""})
            continue
        m = LINK_RE.match(line)
        if m:
            tail = {"severity": m.group("sev").lower(), "code": m.group("code"),
                    "file": None, "line": None, "message": m.group("msg").strip(), "detail": []}
            diags.append(tail)
            continue
        m = DETAIL_RE.match(line)
        if m and tail is not None:
            tail["detail"].append("%s=%s" % (m.group("key").lower(), m.group("val").strip()))
            continue
        m = STEP_RE.match(line)
        if m:
            current = os.path.basename(m.group("file").strip().rstrip("."))
            continue
        if tail is not None and line.startswith("    "):
            tail["detail"].append(line.strip())
            continue
        m = SIZE_RE.search(line)
        if m:
            size = {"data": int(m.group("data")), "data_bits": int(m.group("dbits")),
                    "xdata": int(m.group("xdata")), "code": int(m.group("code"))}
            continue
        m = TOTAL_RE.search(line)
        if m:
            totals = {"errors": int(m.group("errors")), "warnings": int(m.group("warnings"))}
            continue
        if "Target not created" in line:
            failed = True
    for d in diags:
        if d["file"] is None and current:
            d["file"] = current
        if isinstance(d["detail"], list):
            d["detail"] = " ".join(d["detail"])
    errors = [d for d in diags if d["severity"] == "error"]
    warnings = [d for d in diags if d["severity"] == "warning"]
    danger = [d for d in warnings if d["code"] in DANGER_LINK]
    if totals is None:
        totals = {"errors": len(errors), "warnings": len(warnings)}
    if failed and not totals["errors"]:
        totals["errors"] = 1
    return {
        "diagnostics": diags,
        "errors": errors,
        "warnings": warnings,
        "danger_warnings": danger,
        "program_size": size,
        "totals": totals or {"errors": len(errors), "warnings": len(warnings)},
        "ok": len(errors) == 0 and (totals is None or totals["errors"] == 0),
    }


def parse_hex(text):
    """校验 Intel HEX：校验和、记录类型、地址覆盖，并检查 0x0000 复位向量是否真的写了东西。"""
    problems = []
    data_bytes = []
    base = 0
    saw_eof = False
    rec_count = 0
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            problems.append("第 %d 行不以 ':' 开头" % i)
            continue
        m = HEX_REC_RE.match(line)
        if not m:
            problems.append("第 %d 行格式不合法" % i)
            continue
        rec_count += 1
        count = int(m.group(1), 16)
        addr = int(m.group(2), 16)
        rtype = int(m.group(3), 16)
        body = m.group(4)
        ck = int(m.group(5), 16)
        if len(body) != count * 2:
            problems.append("第 %d 行长度字节 %02X 与实际数据 %d 不符" % (i, count, len(body) // 2))
            continue
        vals = [int(body[j:j + 2], 16) for j in range(0, len(body), 2)]
        chk_sum = count + (addr >> 8) + (addr & 0xFF) + rtype + sum(vals)
        if (chk_sum + ck) & 0xFF:
            problems.append("第 %d 行校验和错误" % i)
        if rtype == 0x00:
            full = base + addr
            for j, v in enumerate(vals):
                data_bytes.append((full + j, v))
        elif rtype == 0x01:
            saw_eof = True
        elif rtype == 0x04:
            if count != 2:
                problems.append("第 %d 行扩展线性地址记录长度应为 2" % i)
            base = (vals[0] << 8 | vals[1]) << 16 if count == 2 else 0
        elif rtype == 0x05:
            pass
        else:
            problems.append("第 %d 行出现不支持的记录类型 %02X" % (i, rtype))
    if not saw_eof:
        problems.append("缺少结束记录 :00000001FF")
    if not data_bytes:
        problems.append("没有任何数据记录，hex 是空的")
    addrs = sorted(a for a, _ in data_bytes)
    mem = dict(data_bytes)
    reset_vector = all(0x0000 + k in mem for k in (0, 1, 2)) if addrs else False
    low = [a for a in addrs if a < 3]
    size = len(data_bytes)
    span = (addrs[-1] - addrs[0] + 1) if addrs else 0
    if addrs and addrs[0] > 0x0002:
        problems.append("代码从 0x%04X 开始，0x0000 复位向量是空的：STARTUP.A51 多半没放进编译分组" % addrs[0])
    entry = None
    if 0x0000 in mem and mem.get(0x0000) == 0x02 and len(low) == 3:
        entry = (mem.get(0x0001, 0) << 8) | mem.get(0x0002, 0)
    return {
        "ok": not problems,
        "problems": problems,
        "records": rec_count,
        "bytes": size,
        "span": span,
        "start": addrs[0] if addrs else None,
        "end": addrs[-1] if addrs else None,
        "has_reset_vector": reset_vector,
        "entry": entry,
        "eof": saw_eof,
    }


def parse_curve_line(line):
    """曲线协议：`v: 12` 或 `v: 1.5,2.0`；其它行走文本日志。返回 None 表示不是数据行。"""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if not (s[:1].lower() == "v" and ":" in s[:3]):
        return None
    body = s.split(":", 1)[1]
    vals = []
    for tok in body.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            vals.append(float(tok))
        except ValueError:
            return None
    if not vals:
        return None
    return {"values": vals}


def classify_flash_failure(rc, text):
    """把 stcgal 的失败说成人话；宁可少猜，也别给假原因。"""
    t = text or ""
    low = t.lower()
    if "could not open port" in low or "permissionerror" in low or "filenotfounderror" in low:
        return ("port_unavailable", "串口打不开：被别的程序占着，或 USB 刚被拔掉。关掉串口助手/监视会话再试。")
    if "unknown mcu" in low or "magic" in low and "not" in low:
        return ("unknown_mcu", "型号没认出来：把 protocol 从 stc89 换成 stc12/stc15 试试。")
    if "verify" in low or "checksum" in low:
        return ("verify_failed", "校验不过：写进去和读回来不一致，多半是供电不稳或波特率飘了。")
    if "please cycle power" in low:
        return ("no_handshake", "没抓到冷启动窗口：STC89 只认断 VCC 重上电，拉 RST 和 DTR 都没用，再断一次。")
    if "programmer is not responding" in low:
        return ("no_response", "芯片不响应：确认 P3.0/P3.1 跳线帽接在下载路上。")
    return ("unknown", (t.strip().splitlines() or ["stcgal 退出码 %d，无输出" % rc])[-1])


def local(tag):
    return tag.split("}")[-1]


def first_text(root, name, default=None):
    for node in root.iter():
        if local(node.tag) == name and (node.text or "").strip():
            return node.text.strip()
    return default


def project_info(uvproj):
    """读一个 Keil 工程的 XML，返回调试台需要的元信息（含 hex 位置和是否存在）。"""
    absproj = os.path.abspath(uvproj)
    parent = os.path.basename(os.path.dirname(absproj))
    info = {"project": absproj, "name": os.path.splitext(os.path.basename(absproj))[0],
            "dir": parent, "label": "%s / %s" % (parent, os.path.splitext(os.path.basename(absproj))[0]),
            "device": None, "clock": None, "hex": None, "hex_exists": False,
            "hex_mtime": None, "hex_bytes": 0, "sources": [], "error": None}
    try:
        root = ET.parse(absproj).getroot()
    except Exception as exc:
        info["error"] = "工程 XML 读不了：%s" % exc
        return info
    info["device"] = first_text(root, "Device")
    cpu = first_text(root, "Cpu", "")
    m = re.search(r"CLOCK\((\d+)\)", cpu or "")
    info["clock"] = int(m.group(1)) if m else None
    info["create_hex"] = first_text(root, "CreateHexFile", "0") == "1"
    try:
        info["hex"] = build_flash.hex_path_of(absproj)
    except Exception as exc:
        info["error"] = "hex 路径解析失败：%s" % exc
    if info["hex"] and os.path.isfile(info["hex"]):
        info["hex_exists"] = True
        st = os.stat(info["hex"])
        info["hex_mtime"] = st.st_mtime
        info["hex_bytes"] = st.st_size
    for node in root.iter():
        if local(node.tag) == "FilePath":
            fp = (node.text or "").strip()
            if fp:
                info["sources"].append(os.path.abspath(
                    os.path.join(os.path.dirname(absproj), fp.replace("/", os.sep))))
    return info


EXCLUDE_DEFAULT = ("配套资料", "开发板资料", "程序源码", "课件", "third-party", "node_modules")


def scan_projects(root_dir, max_depth=4, exclude=EXCLUDE_DEFAULT):
    """扫描工作区里的 Keil 工程；厂商资料目录太吵，默认跳过。"""
    root_dir = os.path.abspath(root_dir)
    found = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        rel = os.path.relpath(dirpath, root_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and not os.path.normcase(d).startswith("objects")
                       and not os.path.normcase(d).startswith("listings")
                       and not any(e in d for e in exclude)]
        for fn in sorted(filenames):
            if fn.lower().endswith((".uvproj", ".uvprojx")):
                info = project_info(os.path.join(dirpath, fn))
                rel = os.path.relpath(dirpath, root_dir)
                top = "" if rel == "." else rel.split(os.sep)[0]
                info["top"] = top
                # 实验二/实验三 里都叫 experiment2，父目录也同名，只能用相对根的首层目录区分
                info["label"] = "%s / %s" % (top, info["name"]) if top else info["name"]
                found.append(info)
    return found
