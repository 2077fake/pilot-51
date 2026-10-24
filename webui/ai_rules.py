# -*- coding: utf-8 -*-
"""调试助手的纯逻辑层：本地规则 + 求助单队列。不碰网络、不碰硬件，可离线单测。

规则库里的每一条都来自这个项目真实踩到过的坑（错误码原文见 test_ai_rules.py 的夹具，
是从本机 UV4 / stcgal 输出里抓的），不是从通用教程抄的。
"""
import json
import os
import re
import time

# 8051 的硬边界：data 直接寻址区 128B（52 系列多出的 128B 是 idata，要间接寻址）
DATA_LIMIT = 128
# 11.0592MHz / 9600-8N1 时定时器 1 模式 2 的重装值；波特率飘了多半是这个数没对上
UART_RELOAD_9600 = 0xFD


def _ev(d, cap=3):
    """把诊断字典压成一句可展示的证据文本，最多 cap 条。

    detail 必须带上：L1/L2 的关键信息（是哪个符号）只在 detail 里，光看 message 用户不知道该查谁。
    """
    out = []
    for x in list(d)[:cap]:
        where = "%s:%s" % (x.get("file") or "?", x.get("line")) if x.get("line") else (x.get("file") or x.get("code"))
        line = "%s %s %s" % (where, x.get("code", ""), x.get("message", ""))
        detail = (x.get("detail") or "").strip()
        if detail:
            line += " ｜ " + detail
        out.append(line)
    return out


def _codes(diags):
    return set((d.get("code") or "").upper() for d in diags)


# ---------------------------------------------------------------- 规则表
# 每条规则：find(ctx) 返回证据列表（空=不命中）。证据非空即视为命中。

RULES = [
    {
        "id": "unresolved-external",
        "severity": "danger",
        "title": "链接器有没解决的外部符号——0 Error 也会跑飞",
        "why": "L1/L2 只是警告，所以 Error 数是 0、hex 照样生成，但那个符号在最终镜像里指向的是"
               "编译器随手填的地址。板子上电后一调用它就是乱跳，现象通常是「编译全过、灯一动不动」。",
        "fix": [
            "看 SYMBOL 后面的名字，确认它是哪个 .c/.a51 该提供的（多半是函数名拼错或大小写不符）",
            "如果函数确实写在另一个 .c 里：把那个文件加进 Keil 的工程分组（Project → Manage → Project Items），"
            "光把文件拷到目录里不算，µVision 只编译分组登记过的文件",
            "如果是自己写的汇编：检查 PUBLIC 声明和名字前缀（C51 外部符号带 ?PR? / ?C_PUB 之类修饰）",
            "改完必须重建（Rebuild），增量编译有时不会重跑链接",
        ],
        "verify": "重建后这条警告消失，且 Program Size 的 code 有变化，才算真链上了",
        "find": lambda c: _ev([d for d in c["diagnostics"] if (d.get("code") or "").upper() in ("L1", "L2")]),
    },
    {
        "id": "undefined-identifier",
        "severity": "error",
        "title": "标识符没定义（C202）/ 语法错位（C141）",
        "why": "C202 几乎都是「用了没声明的东西」：少 include、宏没展开、变量名拼错，或者上一行的分号"
               "丢了导致解析器错位——C141 和 C202 同时出现在同一行，通常只有一个是真错。",
        "fix": [
            "先看报错行的**上一行**有没有丢分号或多余的括号",
            "确认用到的函数在某个头文件里有声明（自己写的模块要 #include 对应的 .h）",
            "SFR（P2、TMOD、SCON 这类）必须有 #include <reg52.h>",
            "行号有时指向的是收尾位置，把该函数从头读一遍再改",
        ],
        "verify": "改完只留真错：重建后成对出现的 C141/C202 应该一起消失",
        "find": lambda c: _ev([d for d in c["diagnostics"]
                               if (d.get("code") or "").upper() in ("C202", "C141")]),
    },
    {
        "id": "asm-undefined",
        "severity": "error",
        "title": "汇编里符号未定义（A45）/ 语法错误（A9）",
        "why": "A45 是 PASS-2 还找不到这个符号：要么它属于另一个模块且没 PUBLIC/EXTERN，要么拼错。"
               "A9 常是写法问题——A51 的伪指令、立即数前缀（#）、位变量和字节变量混用。",
        "fix": [
            "被跨文件引用的标号要在定义处写 PUBLIC、在引用处写 EXTERN",
            "检查 MOV 的第二操作数：`MOV A,#0FFH`（立即数）和 `MOV A,0FFH`（直接寻址）不是一回事",
            "位操作只能用 bit 地址或 BIT 定义的标号，不能拿普通字节标号顶",
        ],
        "verify": "assembling 那一步不再报错，才会进 linking",
        "find": lambda c: _ev([d for d in c["diagnostics"]
                               if (d.get("code") or "").upper() in ("A45", "A9")]),
    },
    {
        "id": "uncalled-segment",
        "severity": "info",
        "title": "L16：有段代码没人调用，覆盖分析忽略它",
        "why": "这一般是留着的备用函数没被 main 调到。它不影响正确性，只是提醒你「这段编进去了但用不上」；"
               "另外因为不参与 overlay 分析，函数里的局部数组可能和别处抢同一块 data。",
        "fix": [
            "确认是留的备用 API 就可以忽略",
            "不想要就删掉，或者把调用补上；用不到的模板函数别一直留在工程里",
            "如果同时在为 data 不够发愁，优先删这类未调用函数",
        ],
        "verify": "SEGMENT 名字里 ?PR?_XXX 的 XXX 就是你那个函数名，对得上就说明判断没错",
        "find": lambda c: _ev([d for d in c["diagnostics"] if (d.get("code") or "").upper() == "L16"]),
    },
    {
        "id": "unreferenced-local",
        "severity": "info",
        "title": "C280：局部变量声明了没用到",
        "why": "通常是改代码改一半留下的，或者你以为在用其实拼错了另一个名字。后者是真 bug。",
        "fix": ["确认是不是本来想用它却写错了名字", "确实多余就删掉，别留着占 data"],
        "verify": "删掉后重建仍然 0 Error",
        "find": lambda c: _ev([d for d in c["diagnostics"] if (d.get("code") or "").upper() == "C280"]),
    },
    {
        "id": "no-reset-vector",
        "severity": "danger",
        "title": "hex 的 0x0000 复位向量是空的",
        "why": "8051 复位后从 0x0000 取指，正常应该是一条 LJMP 跳到 STARTUP 入口。代码从 0x0003 之后才开始，"
               "说明 STARTUP.A51 没参与链接——板子上电什么都不跑，且不会有任何报错提示你。",
        "fix": [
            "把 STARTUP.A51 加进工程分组（FileType 选 A51 Source），它不属于 C 文件",
            "确认 0x0000 处是 `02 xx xx`（LJMP）：调试台的 HEX 校验面板会直接显示入口地址",
        ],
        "verify": "HEX 校验里「复位向量」变成已写入、「入口」不是 0x0000",
        "find": lambda c: (c["hex"]["problems"] if c.get("hex") and not c["hex"].get("ok")
                          and any("复位向量" in p for p in c["hex"]["problems"]) else []),
    },
    {
        "id": "hex-broken",
        "severity": "error",
        "title": "hex 本身有问题，烧进去必然不对",
        "why": "校验和、结束记录或地址连续性有问题，多半是 Output 页配置不对、或者文件被别的程序写坏了一半。",
        "fix": ["先 Rebuild 一次再看校验面板", "确认 Options → Output 勾了 Create HEX File"],
        "verify": "HEX 校验显示「结构与校验和都正常」",
        "find": lambda c: (c["hex"]["problems"] if c.get("hex") and not c["hex"].get("ok") else []),
        "when": lambda c: not any("复位向量" in p for p in (c["hex"]["problems"] if c.get("hex") else [])),
    },
    {
        "id": "data-over",
        "severity": "warn",
        "title": "data 用量偏大，注意 128B 直接寻址区",
        "why": "Program Size 里的 data 是 8051 的直接寻址 RAM，只有 128B（52 系列另外的 128B 是 idata，"
               "必须间接寻址，C51 会自动分配但 overlay 深度会受影响）。数组、局部变量都算在这。",
        "fix": [
            "大数组改 `xdata`（代价是访问慢、用 MOVX）",
            "确认不需要常驻的数据加 `data` 之外的存储类型修饰，或缩小类型（unsigned char 代替 int）",
            "删掉没用到的全局/局部变量（C280、L16 那两条就是提示这个的）",
        ],
        "verify": "data 降下来，且程序行为没变",
        "find": lambda c: ([("Program Size data=%s" % c["program_size"]["data"])]
                          if c.get("program_size") and c["program_size"]["data"] > 96 else []),
    },
    {
        "id": "no-handshake",
        "severity": "danger",
        "title": "没抓到冷启动窗口：STC89 只认断电重上电",
        "why": "这块板（HC6800-ES / 普中 A2）的 CH340T 走的是普通 DTR/RTS，拉复位对 STC 的 ISP 引导无效——"
               "芯片只在上电那一瞬的几百毫秒里监听串口。实测把 DTR 拉低再上电也不会进引导。",
        "fix": [
            "点烧写后看到倒计时，再把 VCC 拔掉 3 秒插回（拔 5V，不是拔 RST）",
            "确认 P3.0/P3.1 跳线帽接在下载通路上，否则 PC 根本连不到 ISP 脚",
            "握手波特率 2400、传输 9600 别改；板子晶振是 11.0592MHz",
        ],
        "verify": "遮罩里出现 Loading Flash… 就是接住了",
        "find": lambda c: ([("flash cause=" + c["flash"]["cause"])]
                          if c.get("flash") and c["flash"].get("cause") in ("no_handshake", "no_response") else []),
    },
    {
        "id": "port-busy",
        "severity": "error",
        "title": "串口打不开：被别的程序占着",
        "why": "COM 口同一时刻只能有一个主人。调试台自己的串口监视在烧写前会自动释放，但串口助手、"
               "SSCOM、别的 IDE 会话不会。",
        "fix": ["关掉串口助手/SSCOM/其它监视窗口", "重新插拔一次 USB 让 COM 号刷新", "端口下拉里选带 USB-SERIAL CH340 描述的那个，别选蓝牙 SPP 口"],
        "verify": "顶栏端口显示 COM7 且连接后绿灯亮",
        "find": lambda c: ([("flash cause=" + c["flash"]["cause"])]
                          if c.get("flash") and c["flash"].get("cause") == "port_unavailable" else []),
    },
    {
        "id": "garbage-uart",
        "severity": "warn",
        "title": "串口全是乱码：先核对波特率与晶振",
        "why": "11.0592MHz + 9600-8N1 时定时器 1 模式 2 的重装值必须是 0xFD（256-11059200/12/32/9600=253）。"
               "按 12MHz 算出来的值发到 11.0592MHz 的板子上，收出来就是稳定的一堆乱码。",
        "fix": ["TH1/TL1 用 0xFD，别用 12MHz 算出来的 0xFA", "TMOD 只清高 4 位保留 T0 设置：`TMOD=(TMOD&0x0F)|0x20`",
                "SCON=0x50（模式 1 + 允许接收）、TR1=1"],
        "verify": "终端里能稳定出现 `v: 数字` 这种行，曲线面板开始画点",
        "find": lambda c: _ev([{"code": "", "file": "serial", "line": None, "message": ln}
                              for ln in c.get("serial_tail", []) if _looks_garbage(ln)]),
    },
    {
        "id": "led-port",
        "severity": "warn",
        "title": "灯不亮但程序在跑：先确认 LED 到底在哪个口",
        "why": "这块板 LED 在 P2.0–P2.7 且低电平点亮。很多例程是给另一套板子的（P1），"
               "口写错了现象就是「程序明明在跑（串口有输出）但灯一动不动」。",
        "fix": ["`#define LED P2`，输出用 `LED = ~(0x01 << k)`（低电平点亮要取反）",
                "拿不准就逐个口扫：让 P1/P2/P0 依次全亮全灭，看哪组跟着动"],
        "verify": "串口曲线和灯的移位节奏同步变化，说明口对了、程序也在跑",
        "find": lambda c: _ev([{"code": "", "file": "main.c", "line": None, "message": "P1 出现在源码里但本板 LED 在 P2"}])
                          if c.get("has_p1_gpio") else [],
    },
]

RULE_INDEX = dict((r["id"], r) for r in RULES)


def _looks_garbage(line):
    """串口噪声判定：可打印比例低，或者整行都是控制字符。"""
    s = (line or "").strip()
    if len(s) < 3:
        return False
    ok = sum(1 for ch in s if 32 <= ord(ch) < 127 or ch in "\r\n\t")
    return ok / float(len(s)) < 0.7


def explain(ctx):
    """按当前上下文挑命中的规则，返回可直接渲染的结论列表。

    ctx 需要的字段都可缺省：diagnostics / program_size / hex / flash / serial_tail / has_p1_gpio。
    """
    ctx.setdefault("diagnostics", [])
    out = []
    for r in RULES:
        if r.get("when") and not r["when"](ctx):
            continue
        ev = r["find"](ctx)
        if not ev:
            continue
        out.append(_pack(r, ev))
    return out


def _pack(rule, evidence):
    return {"rule_id": rule["id"], "severity": rule["severity"], "title": rule["title"],
            "why": rule["why"], "fix": rule["fix"], "verify": rule.get("verify", ""),
            "evidence": [str(e) for e in evidence][:4]}


def for_question(text):
    """用户直接打字提问时按关键词找规则；对不上就返回空，交给求助单，不硬套。"""
    return [_pack(RULE_INDEX[i], ["按提问关键词匹配：「%s」" % (text or "").strip()[:60]])
            for i in match_question(text) if i in RULE_INDEX]


def match_question(text):
    """用户直接打字提问时，按关键词找最贴近的一条规则。宁可说不确定，也别硬套。"""
    q = (text or "").lower()
    table = [
        (("跑飞", "不动", "不亮", "没反应", "0 error", "0error"), ("unresolved-external", "no-reset-vector", "led-port")),
        (("乱码", "波特", "baud", "9600"), ("garbage-uart",)),
        (("烧不进去", "下载", "握手", "冷启动", "断电", "cycle power"), ("no-handshake",)),
        (("打不开", "占用", "port", "com"), ("port-busy",)),
        (("data", "内存", "ram", "不够"), ("data-over", "uncalled-segment")),
        (("undefined", "c202", "c141", "未定义"), ("undefined-identifier",)),
        (("a45", "a9", "汇编"), ("asm-undefined",)),
        (("l16", "uncalled"), ("uncalled-segment",)),
    ]
    for keys, ids in table:
        if any(k in q for k in keys):
            return list(ids)
    return []


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _num(ticket_id):
    m = re.search(r"\d+", ticket_id or "")
    return int(m.group(0)) if m else 0


class Tickets:
    """求助单：规则答不了的，落一份 markdown 存档并挂起，等 Qoder 里的 agent 来回写。

    状态只有两种——answered（有答案，来源标 rule 或 qoder）和 pending（等人/等 agent）。
    绝不伪造答案：pending 就是 pending。
    markdown 给人读，同目录的 tickets.json 给程序读回（服务重启后「待回答」不会凭空消失）。
    """

    def __init__(self, archive_dir=None, limit=50):
        self.items = []
        self.archive_dir = archive_dir
        self.limit = limit
        self._seq = 0
        self._load()

    def _index_path(self):
        return os.path.join(self.archive_dir, "tickets.json") if self.archive_dir else None

    def _load(self):
        """重启后「待回答」不能凭空消失：工单在 tickets.json 里存一份，读回来。

        读失败就退回空列表——调试台本体照常工作，只是这一轮没有历史。
        """
        path = self._index_path()
        items = None
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
            except (OSError, ValueError):
                items = None
        if isinstance(items, list):
            self.items = [t for t in items if isinstance(t, dict) and t.get("id")][-self.limit:]
        self._seq = max([_num(t["id"]) for t in self.items] or [0]) or self._highest_archived()

    def _highest_archived(self):
        """tickets.json 不在（刚被删，或是上一版只写 md）时，编号也别退回 T001 去覆盖旧存档。"""
        if not self.archive_dir:
            return 0
        top = 0
        try:
            names = os.listdir(self.archive_dir)
        except OSError:
            return 0
        for name in names:
            m = re.match(r"T(\d+)\.md$", name)
            if m:
                top = max(top, int(m.group(1)))
        return top

    def _save(self):
        path = self._index_path()
        if not path:
            return
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
        except OSError:
            pass      # 存不下不能拖累调试台本体

    def _next_id(self):
        self._seq += 1
        return "T%03d" % self._seq

    def ask(self, question, ctx=None, findings=None, force=False):
        """force=True 表示用户明确要问 Qoder，即使规则能答也照样开工单。"""
        t = {"id": self._next_id(), "created": now(), "question": (question or "").strip(),
             "status": "pending", "context": ctx or {}, "findings": findings or [],
             "answer": None, "answered_by": None, "answered_at": None}
        if findings and not force:
            t["status"] = "answered"
            t["answered_by"] = "rule"
            t["answered_at"] = now()
        self.items.append(t)
        if len(self.items) > self.limit:
            self.items.pop(0)
        self._archive(t)
        self._save()
        return t

    def answer(self, tid, text, by="qoder"):
        t = self.get(tid)
        if not t:
            return None
        t["status"] = "answered"
        t["answered_by"] = by
        t["answered_at"] = now()
        t["answer"] = (text or "").strip()
        self._archive(t)
        self._save()
        return t

    def get(self, tid):
        for t in self.items:
            if t["id"] == tid:
                return t
        return None

    def pending(self):
        return [t for t in self.items if t["status"] == "pending"]

    def all_(self):
        return list(self.items)

    def _archive(self, t):
        if not self.archive_dir:
            return
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            path = os.path.join(self.archive_dir, "%s.md" % t["id"])
            # newline="\n"：源码片段自带 \r\n，不关掉写换行翻译就会落成 \r\r\n，整篇隔行空
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(render_ticket_md(t))
        except OSError:
            pass      # 存档写不进去不能拖累调试台本体


def render_ticket_md(t):
    """求助单的可读文本形式——Qoder 侧读它就能拿到全部上下文，不用再去猜。"""
    lines = ["# 求助单 %s" % t["id"], "", "- 提问：%s" % (t["question"] or "（未填写，只想要一份诊断）"),
             "- 状态：**%s**" % ("已回答（%s）" % t["answered_by"] if t["status"] == "answered" else "等 Qoder 回答"),
             "- 创建：%s" % t["created"]]
    if t["answered_at"]:
        lines.append("- 回答时间：%s" % t["answered_at"])
    c = t.get("context") or {}
    if c:
        lines += ["", "## 上下文", "```"]
        for k in ("project", "device", "clock", "totals", "program_size", "hex_problems", "flash", "attempts"):
            if c.get(k) not in (None, "", [], {}):
                lines.append("%s: %s" % (k, c[k]))
        lines.append("```")
    if c.get("diagnostics"):
        lines += ["", "## 诊断", "```"]
        for d in c["diagnostics"][:20]:
            where = "%s:%s" % (d.get("file"), d.get("line")) if d.get("line") else (d.get("file") or "-")
            lines.append("%s %s %s %s" % (d.get("severity"), d.get("code"), where, d.get("message")))
        lines.append("```")
    if c.get("serial_tail"):
        lines += ["", "## 串口尾部", "```"]
        lines += c["serial_tail"][:40]
        lines.append("```")
    if c.get("source"):
        lines += ["", "## 源码片段", "```c", c["source"], "```"]
    if t["findings"]:
        lines += ["", "## 本地规则命中"]
        for f in t["findings"]:
            lines.append("- **%s**（%s）" % (f.get("title") or f.get("rule_id") or "规则", f.get("rule_id", "")))
            for e in f.get("evidence") or []:
                lines.append("  - 证据：`%s`" % e)
            for s in f.get("fix") or []:
                lines.append("  - 修法：%s" % s)
    if t["answer"]:
        lines += ["", "## 回答（来自 %s）" % t["answered_by"], "", t["answer"]]
    else:
        lines += ["", "## 回答", "", "（还没有人回答。Qoder 里跑着的时候，用 "
                  "`POST /api/ai/answer {id:\"%s\", text:\"...\"}` 回写。）" % t["id"]]
    return "\n".join(lines) + "\n"
