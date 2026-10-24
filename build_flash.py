#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Keil 编译 + stcgal ISP 烧写一条龙。

用法：
    python build_flash.py                       # 编译 51模板 工程并烧写
    python build_flash.py 实验二\\keil project\\experiment2.uvproj
    python build_flash.py --build-only          # 只编译
    python build_flash.py --flash-only          # 只烧上一次的 hex
    python build_flash.py --erase               # 只擦除，不写入
    python build_flash.py --port COM3 --wait 90
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROJECT = os.path.join(HERE, "51模板", "template51.uvproj")
DEFAULT_PORT = "COM7"
DEFAULT_PROTOCOL = "stc89"
HANDSHAKE_BAUD = "2400"
TRANSFER_BAUD = "9600"
BUILD_TIMEOUT = 180


def say(text):
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def clean_output(raw):
    """stcgal 的输出混了进度条和控制台编码，只留能安全显示的结论行。"""
    text = raw.decode("utf-8", "replace")
    if "\ufffd" in text:
        text = raw.decode("gbk", "replace")
    keep = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "\ufffd" in ln or re.search(r"\d+%\|", ln):
            continue
        keep.append(ln)
    return keep


def port_exists(port):
    try:
        from serial.tools import list_ports
    except ImportError:
        return True
    return any(p.device.upper() == port.upper() for p in list_ports.comports())


def find_uv4():
    for cand in (os.environ.get("KEIL_UV4"),
                 os.environ.get("KEIL_ROOT") and os.path.join(os.environ["KEIL_ROOT"], "UV4", "UV4.exe"),
                 shutil.which("UV4.exe"), shutil.which("UV4"),
                 r"C:\Keil_v5\UV4\UV4.exe", r"C:\Keil\UV4\UV4.exe"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def local(tag):
    return tag.split("}")[-1]


def first_text(root, name, default=None):
    for node in root.iter():
        if local(node.tag) == name and (node.text or "").strip():
            return node.text.strip()
    return default


def hex_path_of(project):
    root = ET.parse(project).getroot()
    name = first_text(root, "OutputName", "a")
    outdir = first_text(root, "OutputDirectory", ".\\Objects\\")
    outdir = outdir.replace("/", os.sep).rstrip("\\/")
    base = os.path.dirname(os.path.abspath(project))
    if not os.path.isabs(outdir):
        outdir = os.path.normpath(os.path.join(base, outdir))
    return os.path.join(outdir, name + ".hex")


def summarize_log(log_file):
    if not os.path.isfile(log_file):
        return []
    with open(log_file, "rb") as fh:
        raw = fh.read()
    lines = raw.decode("gbk", "replace").splitlines()
    out = [ln for ln in lines if any(k in ln for k in ("Program Size", "error", "Error", "warning", "Warning"))]
    return out[-8:]


def build(uv4, project, target, rebuild, log_dir):
    os.makedirs(log_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(project))[0]
    log_file = os.path.join(log_dir, "%s.log" % stem)
    cmd = [uv4, "-r" if rebuild else "-b", project, "-j0", "-o", log_file]
    if target:
        cmd += ["-t", target]
    print(">> %s" % ("Rebuild" if rebuild else "Build"))
    try:
        rc = subprocess.call(cmd, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("!! 编译超时（%ss）" % BUILD_TIMEOUT)
        return False
    # µVision: 0=无错无警告 1=有警告 2及以上=有错误
    for ln in summarize_log(log_file):
        say("   %s" % ln.strip())
    if rc >= 2:
        print("!! 编译失败 (uv4 rc=%d)，日志：%s" % (rc, log_file))
        return False
    if rc == 1:
        print("   （有警告，已继续）")
    return True


def flash(hex_file, port, protocol, wait, erase, extra=None):
    # STC89 只认冷启动（断 VCC），stcgal 的 -a/DTR 在这块板上无效，所以靠人工断电 + 立即重试接住上电瞬间。
    deadline = time.time() + wait
    n = 0
    print(">> 烧写 %s -> %s" % (os.path.basename(hex_file), port))
    print("   %d 秒内请断电 3 秒再上电（可多试几次，脚本会自动接住）" % wait)
    while time.time() < deadline:
        n += 1
        left = max(5, int(deadline - time.time()))
        cmd = [sys.executable, "-m", "stcgal", "-p", port, "-P", protocol,
               "-l", HANDSHAKE_BAUD, "-b", TRANSFER_BAUD]
        if erase:
            cmd.append("-e")
        if extra:
            cmd += extra
        if not erase:
            cmd.append(hex_file)
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=left)
            rc = res.returncode
            lines = clean_output(res.stdout + res.stderr)
        except subprocess.TimeoutExpired:
            rc, lines = 124, []
        if rc == 0:
            for ln in lines[-6:]:
                say("   %s" % ln)
            say(">> 烧写成功（第 %d 次尝试）" % n)
            say("   再断电重上电一次，芯片才会开始跑新程序")
            return True
        if n == 1 or rc != 124:
            say("   第 %d 次未握手 (rc=%d)%s" % (n, rc, ("：" + lines[-1]) if lines else ""))
        time.sleep(0.5)
    say("!! %d 秒内没抓到冷启动窗口，烧写未完成（共尝试 %d 次）" % (wait, n))
    return False


def main():
    ap = argparse.ArgumentParser(description="Keil 编译 + stcgal 烧写")
    ap.add_argument("project", nargs="?", default=DEFAULT_PROJECT, help=".uvproj/.uvprojx 路径")
    ap.add_argument("--hex", help="直接指定 hex，跳过编译")
    ap.add_argument("--target", help="µVision Target 名，默认用工程第一个")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--protocol", default=DEFAULT_PROTOCOL,
                    help="stcgal 协议：stc89/stc12/stc15/...（-P）")
    ap.add_argument("--rebuild", action="store_true", help="全量重建（-r）")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--flash-only", action="store_true")
    ap.add_argument("--erase", action="store_true", help="只擦除 flash")
    ap.add_argument("--wait", type=int, default=60, help="等待冷启动的秒数")
    args = ap.parse_args()

    project = os.path.abspath(args.project)
    if not args.hex and not args.flash_only and not os.path.isfile(project):
        print("!! 找不到工程文件：%s" % project)
        return 3

    hex_file = os.path.abspath(args.hex) if args.hex else hex_path_of(project)

    if not args.flash_only:
        uv4 = find_uv4()
        if not uv4:
            print("!! 没找到 UV4.exe。设 KEIL_UV4 或 KEIL_ROOT 环境变量，或 --flash-only 直接烧已有 hex。")
            return 3
        log_dir = os.path.join(os.path.dirname(project), ".embeddedskills", "build")
        if not build(uv4, project, args.target, args.rebuild, log_dir):
            return 1
        if not os.path.isfile(hex_file):
            print("!! 编译说成功了，但没生成 %s（检查 Keil 里 Output 页的 'Create HEX File' 是否勾上）" % hex_file)
            return 1
        print("   hex: %s (%d bytes)" % (hex_file, os.path.getsize(hex_file)))

    if args.build_only:
        return 0

    if not os.path.isfile(hex_file):
        print("!! 找不到 hex：%s" % hex_file)
        return 3
    if not port_exists(args.port):
        print("!! 串口 %s 不在。检查 USB 和 CH340 驱动（可用 --port 指定别的口）。" % args.port)
        return 3

    return 0 if flash(hex_file, args.port, args.protocol, args.wait, args.erase) else 2


if __name__ == "__main__":
    sys.exit(main())
