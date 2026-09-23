#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 MSPM0G3507 板卡的启动横幅，用于确认在板固件身份。

用途：
  烧录后确认板上运行的固件版本。关键判据是 READY 行是否含
  "D=Dump samples"（2026-09-22 新增的样点导出命令）。旧固件该行
  只有 "R=ADC, W=Inject, T=Simulate"。

背景约束（本机实测）：
  - 只有系统 Python 3.11 装 pyserial；托管 3.13 没有。
  - 必须锁定 XDS110 Application/User UART（VID_0451&PID_BEF3），
    不能落到 COM16 辅助口。

用法：
  python read_boot_banner.py [--port COM15] [--timeout 6] [--reset]
"""
from __future__ import annotations

import argparse
import re
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 pyserial（请在系统 Python 3.11 下运行）\n")
    raise SystemExit(2)

VID_PID = "0451:BEF3"
BANNER_MARK = "TI-CUP WAVEFORM CLASSIFIER"
NEW_FW_MARK = "D=Dump samples"


def find_app_port(preferred: str) -> str:
    """在 XDS110 的两个串口里挑出 Application/User UART。"""
    if preferred:
        for p in list_ports.comports():
            if p.device.upper() == preferred.upper():
                return p.device
        raise SystemExit(f"指定端口 {preferred} 不存在")
    cands = []
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        if VID_PID in hwid and "APPLICATION" in (p.description or "").upper():
            cands.append(p.device)
    if len(cands) != 1:
        raise SystemExit(
            "无法唯一确定 XDS110 Application/User UART，候选="
            + repr(cands)
            + "；请用 --port 指定"
        )
    return cands[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM15")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument(
        "--reset",
        action="store_true",
        help="打开串口后触发 DTR/RTS 复位脉冲以重放横幅",
    )
    args = ap.parse_args()

    port = find_app_port(args.port)
    sys.stdout.write(f"[port] {port}\n")

    with serial.Serial(port, args.baud, timeout=0.2) as ser:
        if args.reset:
            ser.setDTR(False)
            ser.setRTS(True)
            time.sleep(0.05)
            ser.setRTS(False)
            time.sleep(0.2)
        deadline = time.time() + args.timeout
        buf = b""
        while time.time() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
            if NEW_FW_MARK.encode() in buf or b"READY" in buf:
                # 再多读半秒，把 READY 行收全
                time.sleep(0.3)
                buf += ser.read(4096)
                break
        text = buf.decode("ascii", "replace")

    sys.stdout.write("[raw]\n" + text + "\n")
    sys.stdout.write("[verdict]\n")
    if BANNER_MARK not in text:
        sys.stdout.write("未捕获到横幅（可能已先输出、或设备未复位）\n")
        return 1

    fw = re.search(r"FW=([^,\r\n]+)", text)
    sys.stdout.write(f"fw_tag={fw.group(1) if fw else '?'}\n")
    has_dump = NEW_FW_MARK in text
    sys.stdout.write(f"has_dump_command={has_dump}\n")
    sys.stdout.write(
        "在板固件=NEW(含 D 样点导出)\n" if has_dump else "在板固件=OLD(无 D 命令)\n"
    )
    return 0 if has_dump else 1


if __name__ == "__main__":
    raise SystemExit(main())
