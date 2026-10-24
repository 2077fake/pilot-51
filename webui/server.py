# -*- coding: utf-8 -*-
"""51 单片机在线调试台后端（零第三方依赖，只用标准库 + 已装好的 pyserial/stcgal）。

启动： python server.py            然后浏览器开 http://127.0.0.1:8765
只绑 127.0.0.1：本机自用工具，没有鉴权，别把它暴露到网络上。
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import core            # noqa: E402
import build_flash     # noqa: E402
import ai_rules        # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
ROOT = os.path.abspath(os.path.dirname(HERE))
HANDSHAKE_BAUD = "2400"
TRANSFER_BAUD = "9600"
BUILD_TIMEOUT = 180
MAX_BODY = 1 << 20
STATIC_DIR = os.path.join(HERE, "static")

BUSY_PCT_RE = re.compile(r"(\d+)%")
# 本板 LED 在 P2；源码里出现 P1 大概率是从别的板子的例程抄来的
P1_GPIO_RE = re.compile(r"(?<![A-Za-z0-9_])P1\b")
AI_INCLUDES = ("diags", "serial_tail", "hex", "source")
SOURCE_MAX_BYTES = 6000


def now():
    return time.strftime("%H:%M:%S", time.localtime()) + ".%03d" % (int(time.time() * 1000) % 1000)


def safe_decode(raw):
    text = core.decode_log(raw)
    return text.replace("\ufffd", "")


class Bus:
    """一条 SSE 通道多路复用所有推送；慢客户端丢事件，绝不拖死生产者。"""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=800)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, topic, **payload):
        event = dict(payload, topic=topic, ts=now())
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return event


class Job:
    def __init__(self, kind, label):
        self.id = "%s-%d" % (kind, int(time.time() * 1000))
        self.kind = kind
        self.label = label
        self.status = "running"
        self.started = time.time()
        self.finished = None
        self.result = None

    def snapshot(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "started": self.started, "finished": self.finished, "result": self.result,
                "elapsed": round((self.finished or time.time()) - self.started, 2)}


class Runner:
    """全局只有一个作业槽：µVision 同工程并发编译会互踩，烧写更要独占串口。"""

    def __init__(self, bus):
        self.bus = bus
        self.job = None
        self.last = None
        self.lock = threading.Lock()

    def start(self, kind, label, fn):
        with self.lock:
            if self.job is not None and self.job.status == "running":
                return None
            job = Job(kind, label)
            self.job = job
        t = threading.Thread(target=self._wrap, args=(job, fn), daemon=True)
        t.start()
        return job

    def _wrap(self, job, fn):
        self.bus.publish("job", job=job.snapshot(), event="start")
        try:
            result = fn(job)
            job.result = result
            job.status = "ok" if (result is None or result.get("ok", True)) else "failed"
        except Exception as exc:
            job.status = "failed"
            job.result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            self.bus.publish("log", level="error", text="后端异常：%s" % exc)
        job.finished = time.time()
        self.bus.publish("job", job=job.snapshot(), event="end")
        with self.lock:
            self.last = job
            self.job = None

    def busy(self):
        with self.lock:
            return self.job if self.job and self.job.status == "running" else None

    def latest(self):
        """作业跑完不能立刻蒸发，前端要点开看诊断结果。"""
        with self.lock:
            job = self.job if (self.job and self.job.status == "running") else self.last
        return job.snapshot() if job else None


class SerialHolder:
    """串口唯一主人。烧写前必须 close()，否则就是那个 could not open port。"""

    def __init__(self, bus):
        self.bus = bus
        self.lock = threading.Lock()
        self.ser = None
        self.port = None
        self.baud = None
        self.thread = None
        self._stop = None
        self._gen = 0
        self._buf = b""
        self.tail = deque(maxlen=80)   # 最近若干行，喂给调试助手当上下文

    @property
    def open_(self):
        return self.ser is not None

    def open(self, port, baud):
        import serial  # 只有真要用串口时才依赖
        with self.lock:
            if self.ser:
                self._close_locked()
            self._gen += 1
            gen = self._gen
            self.ser = serial.Serial(port, int(baud), timeout=0.05)
            self.port, self.baud = port, int(baud)
            self._stop = threading.Event()
            self.thread = threading.Thread(target=self._reader, args=(gen,), daemon=True)
            self.thread.start()
        self.bus.publish("serial_state", open=True, port=port, baud=baud)
        return {"port": port, "baud": int(baud)}

    def close(self):
        with self.lock:
            if not self.ser:
                return False
            self._gen += 1          # 让旧 reader 知道自己已经被取代，别再去关新的句柄
            self._close_locked()
        self.bus.publish("serial_state", open=False, port=None, baud=None)
        return True

    def _close_locked(self):
        if self._stop:
            self._stop.set()
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.port = None
        self.baud = None

    def write(self, text, newline=True):
        with self.lock:
            if not self.ser:
                raise RuntimeError("串口没打开，先在页面上点连接")
            payload = text + ("\r\n" if newline else "")
            self.ser.write(payload.encode("utf-8", "replace"))
        self.bus.publish("serial", data=payload.replace("\r\n", "\n"), direction="out")

    def _reader(self, gen):
        while not self._stop.is_set():
            try:
                n = self.ser.in_waiting
            except Exception as exc:
                self.bus.publish("log", level="error", text="串口读不到：%s（可能被拔掉了）" % exc)
                break
            if not n:
                self._stop.wait(0.01)
                continue
            try:
                chunk = self.ser.read(n)
            except Exception as exc:
                self.bus.publish("log", level="error", text="串口读失败：%s" % exc)
                break
            self._feed(chunk)
        with self.lock:
            mine = self.ser is not None and self._gen == gen
            if mine:
                self._close_locked()
        if mine:
            self.bus.publish("serial_state", open=False, port=None, baud=None)

    def _feed(self, chunk):
        self._buf += chunk
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) > 512:         # 没有换行的噪声，别让它无限涨
                    self.bus.publish("serial", data=safe_decode(self._buf[:512]) + "\n")
                    self._buf = self._buf[512:]
                return
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            text = safe_decode(line).replace("\r", "").strip()
            self.tail.append(text)
            self.bus.publish("serial", data=text + "\n")
            point = core.parse_curve_line(text)
            if point:
                self.bus.publish("curve", values=point["values"])


def scan_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "desc": p.description} for p in list_ports.comports()]


# ---------------------------------------------------------------- build

def run_build(job, state, project, rebuild):
    uv4 = build_flash.find_uv4()
    if not uv4:
        state.bus.publish("log", level="error", text="没找到 UV4.exe，设 KEIL_UV4 或 KEIL_ROOT")
        return {"ok": False, "error": "uv4_not_found"}
    log_file = os.path.join(os.path.dirname(project), ".embeddedskills", "build",
                            "%s-%s.log" % (os.path.splitext(os.path.basename(project))[0], now().replace(":", "-").replace(".", "-")))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    cmd = [uv4, "-r" if rebuild else "-b", project, "-j0", "-o", log_file]
    state.bus.publish("log", level="info", text="开始编译：%s（%s）" % (os.path.basename(project), "全量重建" if rebuild else "增量"))
    try:
        rc = subprocess.call(cmd, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        state.bus.publish("log", level="error", text="编译超过 %ds：µVision 可能弹了对话框卡住命令行" % BUILD_TIMEOUT)
        state.last_build = {"ok": False, "error": "timeout", "diagnostics": [], "totals": None,
                            "program_size": None, "project": project}
        return {"ok": False, "error": "timeout"}
    text = safe_decode(open(log_file, "rb").read()) if os.path.isfile(log_file) else ""
    parsed = core.parse_build_log(text)
    parsed["project"] = project
    parsed["uv4_rc"] = rc
    parsed["log_file"] = log_file
    for d in parsed["diagnostics"]:
        where = "%s:%s" % (d["file"], d["line"]) if d["line"] else (d["file"] or d["code"])
        state.bus.publish("build_diag", severity=d["severity"], code=d["code"],
                          where=where, message=d["message"], detail=d["detail"],
                          project=project)
    info = core.project_info(project)
    parsed["project_info"] = info
    if parsed["ok"] and not info["hex_exists"]:
        parsed["hex_missing"] = True
        state.bus.publish("log", level="error",
                          text="编译过了但没生成 hex：去 Keil 的 Output 页勾 Create HEX File")
    ps = parsed["program_size"]
    if ps:
        state.bus.publish("log", level="info",
                          text="Program Size: data=%d.%d xdata=%d code=%d"
                               % (ps["data"], ps["data_bits"], ps["xdata"], ps["code"]))
    totals = parsed["totals"]
    state.bus.publish("log", level="error" if totals["errors"] else "info",
                      text="结果：%d Error(s), %d Warning(s)" % (totals["errors"], totals["warnings"]))
    for d in parsed["danger_warnings"]:
        state.bus.publish("log", level="danger",
                          text="%s %s —— 链接器有没解决的外部符号，0 Error 也会跑飞：%s"
                               % (d["code"], d["message"], d["detail"]))
    state.last_build = parsed
    return parsed


# ---------------------------------------------------------------- flash

def run_flash(job, state, hex_file, port, protocol, wait):
    deadline = time.time() + wait
    attempts = 0
    state.last_flash = None      # 别拿上一轮的成功当本轮的结论
    if state.serial.close():
        state.bus.publish("log", level="warn", text="烧写前已释放串口（监视会话暂停）")
    if not os.path.isfile(hex_file):
        state.bus.publish("log", level="error", text="hex 不存在：%s" % hex_file)
        return {"ok": False, "error": "hex_missing"}
    cmd = [sys.executable, "-m", "stcgal", "-p", port, "-P", protocol,
           "-l", HANDSHAKE_BAUD, "-b", TRANSFER_BAUD, hex_file]
    state.bus.publish("flash_phase", phase="waiting_power", attempt=0, pct=0,
                      text="接下来请把板子断电 3 秒再上电，脚本会自己接住")
    last_err = ""
    while time.time() < deadline:
        attempts += 1
        state.bus.publish("flash_phase", phase="handshaking", attempt=attempts, pct=0,
                          text="第 %d 次等待冷启动握手…" % attempts)
        rc, text = _spawn(cmd, max(5, deadline - time.time()), state)
        last_err = text
        if rc == 0:
            state.bus.publish("flash_phase", phase="done", attempt=attempts, pct=100,
                              text="烧写成功：再断电重上电一次才会跑新程序")
            state.last_flash = {"ok": True, "cause": None, "attempts": attempts, "hex": hex_file, "port": port}
            return {"ok": True, "attempts": attempts, "log": text}
        cause, hint = core.classify_flash_failure(rc, text)
        state.last_flash = {"ok": False, "cause": cause, "hint": hint, "attempts": attempts,
                            "hex": hex_file, "port": port}
        state.bus.publish("flash_fail_reason", cause=cause, hint=hint, attempt=attempts)
        time.sleep(0.5)
    state.bus.publish("flash_phase", phase="failed", attempt=attempts, pct=0,
                      text="%d 秒内没抓到冷启动窗口（共 %d 次）" % (wait, attempts))
    state.last_flash = state.last_flash or {"ok": False, "cause": "no_handshake", "attempts": attempts}
    return {"ok": False, "error": "no_handshake", "attempts": attempts, "log": last_err}


def _spawn(cmd, seconds, state):
    """块读 stcgal 输出：进度条用 \\r，readline 会把它憋到行尾才吐出来。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    timer = threading.Timer(seconds, _kill, args=[proc, state])
    timer.daemon = True
    timer.start()
    collected = []
    tail = b""
    try:
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            tail += chunk
            parts = re.split(rb"[\r\n]+", tail)
            tail = parts.pop()
            for piece in parts:
                _emit_line(piece, collected, state)
        _emit_line(tail, collected, state)
        rc = proc.wait()
    finally:
        timer.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass
    return rc, "\n".join(collected)


def _emit_line(piece, collected, state):
    text = safe_decode(piece).strip()
    if not text:
        return
    collected.append(text)
    _publish_progress(text, state)


def _kill(proc, state):
    if proc.poll() is None:
        proc.kill()
        state.bus.publish("log", level="warn", text="stcgal 这一轮超时被终止，继续下一轮")


def _publish_progress(text, state):
    phase = None
    low = text.lower()
    if "loading flash" in low:
        phase = "loading"
    elif "erasing" in low:
        phase = "erasing"
    elif "writing flash" in low:
        phase = "writing"
    elif "setting options" in low:
        phase = "options"
    elif "target model" in low or "name:" in low:
        phase = "identified"
    elif "disconnect" in low:
        phase = "done"
    m = BUSY_PCT_RE.search(text)
    pct = int(m.group(1)) if m and phase == "writing" else None
    state.bus.publish("flash_line", text=text, phase=phase, pct=pct)


# ---------------------------------------------------------------- 调试助手上下文

def read_project_source(project, names=("main.c",), max_bytes=SOURCE_MAX_BYTES):
    """读工程同目录的主要源文件，拼一段给规则做源码层面的检查。"""
    base = os.path.dirname(project)
    chunks = []
    for name in names:
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                chunks.append("// %s\n%s" % (name, safe_decode(f.read(max_bytes))))
        except OSError:
            continue
    return "\n".join(chunks)


def ai_context(state, project=None, include=None, source=""):
    """把散在各处的现场（编译结果、hex、串口尾、烧写结论、源码）收成规则层要的那份 ctx。

    缺哪块就缺哪块：没编译过就没有 diagnostics，助手不会假装看过。
    """
    include = list(include or AI_INCLUDES)
    ctx = {"diagnostics": [], "serial_tail": []}
    if not project and state.last_build:
        project = state.last_build.get("project")
    if project:
        ctx["project"] = project
        info = core.project_info(project) if os.path.isfile(project) else {}
        ctx["device"] = info.get("device")
        ctx["clock"] = info.get("clock")
        if "hex" in include and info.get("hex_exists"):
            try:
                with open(info["hex"], "r", encoding="ascii", errors="replace") as f:
                    ctx["hex"] = core.parse_hex(f.read())
                    ctx["hex_problems"] = ctx["hex"]["problems"]
            except OSError:
                pass
    if "source" in include and not source:
        source = read_project_source(project) if project else ""
    source = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    if source:
        ctx["source"] = source
        ctx["has_p1_gpio"] = bool(P1_GPIO_RE.search(source))
    build = state.last_build or {}
    ctx["totals"] = build.get("totals")
    ctx["program_size"] = build.get("program_size")
    if "diags" in include:
        ctx["diagnostics"] = build.get("diagnostics") or []
    if "serial_tail" in include:
        ctx["serial_tail"] = list(state.serial.tail)
    if state.last_flash:
        ctx["flash"] = state.last_flash
        ctx["attempts"] = state.last_flash.get("attempts")
    return ctx


def ticket_public(t):
    """回给前端的单子只留展示要用的字段；源码片段和长串口尾巴不来回传（存档 markdown 里有）。"""
    ctx = dict(t.get("context") or {})
    ctx.pop("source", None)
    tail = ctx.get("serial_tail") or []
    if len(tail) > 12:
        ctx["serial_tail"] = tail[-12:]
    return dict(t, context=ctx)


def api_ai_ask(state, body, resolve):
    """一次提问：先跑本地规则，规则答不了（或用户点名要问 Qoder）就落成求助单。

    不编造答案——pending 的单子在界面上就写「待回答（Qoder 未接单）」。
    """
    question = (body.get("question") or "").strip()
    include = list(body.get("include") or AI_INCLUDES)
    if "source_snippet" in include and "source" not in include:
        include.append("source")
    project = resolve(body["project"]) if body.get("project") else None
    source = body.get("source") or ""
    ctx = ai_context(state, project, include, source)
    findings = ai_rules.explain(ctx)
    if findings:
        origin = "explain"
    elif question:
        findings = ai_rules.for_question(question)
        origin = "question" if findings else "none"
    else:
        origin = "none"
    ticket = state.ai.ask(question, ctx=ctx, findings=findings, force=bool(body.get("force")))
    public = ticket_public(ticket)
    state.bus.publish("ai", kind="ask", ticket=public, findings=findings, origin=origin)
    return {"ok": True, "ticket": public, "findings": findings, "origin": origin,
            "pending": len(state.ai.pending())}


def api_ai_answer(state, body):
    tid = (body.get("id") or "").strip()
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("回答不能为空")
    ticket = state.ai.answer(tid, text, by=body.get("by") or "qoder")
    if not ticket:
        return None
    state.bus.publish("ai", kind="answer", ticket=ticket_public(ticket))
    return {"ok": True, "ticket": ticket_public(ticket), "pending": len(state.ai.pending())}


# ---------------------------------------------------------------- http

class State:
    def __init__(self, root):
        self.root = root
        self.bus = Bus()
        self.serial = SerialHolder(self.bus)
        self.runner = Runner(self.bus)
        # 调试助手：规则 + 求助单。求助单同时落一份 markdown 到 调试台存档/ai/，
        # 这样 Qoder 关着的时候单子不会丢，重启后还能看见「待回答」。
        self.ai = ai_rules.Tickets(archive_dir=os.path.join(root, "调试台存档", "ai"))
        self.last_build = None
        self.last_flash = None


STATE = State(ROOT)


class Handler(BaseHTTPRequestHandler):
    server_version = "MCUConsole/1.0"
    protocol_version = "HTTP/1.1"
    # Edge 睡眠标签页会停读 SSE，阻塞式 write 会把线程挂死在这条连接上；
    # 30s 写不出即放弃并释放，浏览器侧拿到断链信号后自动重连
    timeout = 30

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("请求体太大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def resolve_in_root(self, path):
        p = unquote(path or "")
        candidate = p if os.path.isabs(p) else os.path.join(STATE.root, p)
        real = os.path.realpath(os.path.abspath(candidate))
        root = os.path.realpath(STATE.root)
        if os.path.normcase(real) != root and not os.path.normcase(real).startswith(os.path.normcase(root) + os.sep):
            raise PermissionError("越界路径已被拒绝：%s" % p)
        return real

    def query(self, qs):
        d = parse_qs(qs)
        return {k: v[0] for k, v in d.items()}

    # ---- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        route, qs = parsed.path, self.query(parsed.query)
        try:
            if route in ("/", "/index.html"):
                return self.serve_static(os.path.join(STATIC_DIR, "index.html"))
            if route.startswith("/static/"):
                return self.serve_static(os.path.join(STATIC_DIR, route[len("/static/"):]))
            if route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if route == "/api/events":
                return self.sse()
            if route == "/api/state":
                return self.api_state()
            if route == "/api/projects":
                return self.api_projects()
            if route == "/api/ports":
                return self.send_json({"ports": scan_ports()})
            if route == "/api/hex":
                path = self.resolve_in_root(qs.get("project", ""))
                info = core.project_info(path)
                if not info["hex_exists"]:
                    return self.send_json({"ok": False, "problems": ["hex 还没生成，先编译"], "info": info})
                text = open(info["hex"], "r", encoding="ascii", errors="replace").read()
                return self.send_json(dict(core.parse_hex(text), info=info))
            if route == "/api/files":
                return self.api_files(qs)
            if route == "/api/job":
                return self.send_json({"job": STATE.runner.latest()})
            if route == "/api/ai/pending":
                return self.send_json({"ok": True, "tickets": [ticket_public(t) for t in STATE.ai.pending()]})
            if route == "/api/ai/tickets":
                return self.send_json({"ok": True, "tickets": [ticket_public(t) for t in STATE.ai.all_()],
                                       "archive": STATE.ai.archive_dir})
        except PermissionError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 403)
        except Exception as exc:
            return self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return self.send_json({"ok": False, "error": "no such route: %s" % route}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            body = self.read_body()
            if route == "/api/build":
                return self.api_build(body)
            if route == "/api/flash":
                return self.api_flash(body)
            if route == "/api/serial/open":
                info = STATE.serial.open(body["port"], body.get("baud", 9600))
                return self.send_json({"ok": True, **info})
            if route == "/api/serial/close":
                return self.send_json({"ok": STATE.serial.close()})
            if route == "/api/serial/write":
                STATE.serial.write(body.get("text", ""), body.get("newline", True))
                return self.send_json({"ok": True})
            if route == "/api/stop":
                return self.send_json({"ok": False, "error": "编译没法中途取消；烧写会在超时窗口结束后自己停"})
            if route == "/api/ai/ask":
                return self.send_json(api_ai_ask(STATE, body, self.resolve_in_root))
            if route == "/api/ai/answer":
                result = api_ai_answer(STATE, body)
                if result is None:
                    return self.send_json({"ok": False, "error": "没有这张求助单：%s" % body.get("id")}, 404)
                return self.send_json(result)
        except PermissionError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 403)
        except (KeyError, ValueError) as exc:
            return self.send_json({"ok": False, "error": "参数不对：%s" % exc}, 400)
        except RuntimeError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            return self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return self.send_json({"ok": False, "error": "no such route: %s" % route}, 404)

    # ---- implementations
    def serve_static(self, path):
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(STATIC_DIR)) or not os.path.isfile(real):
            return self.send_json({"ok": False, "error": "找不到文件"}, 404)
        body = open(real, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_state(self):
        job = STATE.runner.busy()
        return self.send_json({
            "root": STATE.root,
            "uv4": build_flash.find_uv4(),
            "stcgal_ok": _module_ok("stcgal"),
            "serial_ok": _module_ok("serial"),
            "serial": {"open": STATE.serial.open_, "port": STATE.serial.port, "baud": STATE.serial.baud},
            "running": job.snapshot() if job else None,
            "handshake_baud": int(HANDSHAKE_BAUD), "transfer_baud": int(TRANSFER_BAUD),
            "ai_rules": len(ai_rules.RULES),
            "ai_pending": len(STATE.ai.pending()),
            "ai_tickets": len(STATE.ai.all_()),
            "ai_archive": STATE.ai.archive_dir,
        })

    def api_projects(self):
        projects = core.scan_projects(STATE.root)
        for p in projects:
            p["hex_mtime_text"] = time.strftime("%m-%d %H:%M", time.localtime(p["hex_mtime"])) if p["hex_mtime"] else None
        return self.send_json({"projects": projects, "default": _pick_default(projects)})

    def api_files(self, qs):
        target = self.resolve_in_root(qs.get("dir", ""))
        if not os.path.isdir(target):
            return self.send_json({"ok": False, "error": "不是目录"}, 404)
        entries = []
        for name in sorted(os.listdir(target), key=str.lower):
            full = os.path.join(target, name)
            if os.path.isdir(full):
                entries.append({"name": name, "type": "dir"})
            elif name.lower().endswith((".c", ".h", ".a51", ".asm", ".uvproj", ".uvprojx", ".hex", ".m51", ".lst")):
                entries.append({"name": name, "type": "file", "bytes": os.path.getsize(full)})
        return self.send_json({"dir": target, "entries": entries})

    def api_build(self, body):
        project = self.resolve_in_root(body.get("project", ""))
        if not os.path.isfile(project):
            return self.send_json({"ok": False, "error": "工程不存在"}, 404)
        if STATE.runner.busy():
            return self.send_json({"ok": False, "error": "有作业在跑，等它结束"}, 409)
        rebuild = bool(body.get("rebuild"))
        job = STATE.runner.start("build", os.path.basename(project),
                                 lambda j: run_build(j, STATE, project, rebuild))
        return self.send_json({"ok": True, "job": job.snapshot()})

    def api_flash(self, body):
        if body.get("hex"):
            hex_file = self.resolve_in_root(body["hex"])
        else:
            project = self.resolve_in_root(body.get("project", ""))
            info = core.project_info(project)
            if not info["hex"]:
                return self.send_json({"ok": False, "error": "读不出 hex 路径"}, 400)
            hex_file = info["hex"]
        if STATE.runner.busy():
            return self.send_json({"ok": False, "error": "有作业在跑（多半是串口被占着）"}, 409)
        port = body.get("port") or (STATE.serial.port or "COM7")
        wait = int(body.get("wait", 90))
        protocol = body.get("protocol") or "stc89"
        job = STATE.runner.start("flash", "%s -> %s" % (os.path.basename(hex_file), port),
                                 lambda j: run_flash(j, STATE, hex_file, port, protocol, wait))
        return self.send_json({"ok": True, "job": job.snapshot()})

    # ---- sse
    def sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = STATE.bus.subscribe()
        try:
            self._sse_send({"topic": "hello", "ts": now()})
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    # 必须是 data 事件：注释行 ": ping" 不触发前端 onmessage，看门狗感知不到
                    self._sse_send({"topic": "ping"})
                    continue
                self._sse_send(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            STATE.bus.unsubscribe(q)

    def _sse_send(self, event):
        self.wfile.write(("data: %s\n\n" % json.dumps(event, ensure_ascii=False, default=str)).encode("utf-8"))
        self.wfile.flush()


def _module_ok(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _pick_default(projects):
    for p in projects:
        if p["name"] == "template51":
            return p["project"]
    return projects[0]["project"] if projects else None


class SingleInstanceServer(ThreadingHTTPServer):
    """Windows 的 SO_REUSEADDR 允许两个进程绑同一个端口，第二个会悄悄抢走连接——
    表现就是「重启过了但行为时对时错」。关掉复用，端口被占就直接报错，不留下影子实例。"""
    allow_reuse_address = 0
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="51 在线调试台")
    ap.add_argument("--root", default=ROOT, help="工作区根目录（默认 单片微机）")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    STATE.root = os.path.abspath(args.root)
    url = "http://%s:%d" % (HOST, args.port)
    try:
        httpd = SingleInstanceServer((HOST, args.port), Handler)
    except OSError as e:
        print("端口 %d 起不来：%s（多半是已经有一个调试台在跑，先把它关掉）" % (args.port, e))
        return 1
    print("调试台就绪：%s" % url)
    print("工作区：%s" % STATE.root)
    if not _module_ok("serial"):
        print("警告：pyserial 没装，串口监视和烧写都不可用（pip install pyserial）")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main() or 0)
