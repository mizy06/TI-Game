#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MSPM0G3507 波形识别工程 - 串口自动化测试与评估工具

功能：
1. 自动检测 TI XDS110 虚拟串口 (COM15 等)
2. 触发采集 ('R' / 'r') 并监听 MCU 状态 ('CAPTURING', 'PROCESSING', 'RESULT', 'READY')
3. 结构化解析识别结果：波形类型、频率、峰峰值、方波占空比、模板得分、置信余量
4. 支持单次测试、连续稳定性统计测试、标称参数对比判定 (PASS/FAIL)
5. 测试记录导出为 CSV 文件

本工具的 R 命令会让固件从真实 ADC 引脚采样，因此它观察的是“模拟前端 +
ADC + 采样时钟 + 数字算法”的整体。只有在用户提供已知信号源参数时，
``--type/--freq/--vpp`` 对比才具有精度测试意义；否则只是读取和展示结果。
"""

import sys
import time
import re
import csv
import math
import argparse
from datetime import datetime

# 尽量让 Windows 终端正确显示中文；旧 Python/终端不支持时静默回退。
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# pyserial 是实际串口运行所需；rich 只负责美观显示。模块仍可在缺少它们
# 时被单元测试导入，以测试纯解析逻辑。
try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except ImportError:
    serial = None
    HAVE_SERIAL = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

console = Console() if HAVE_RICH else None

# 固件成功/失败 RESULT 行的严格语法。后半测量字段对失败行是可选的，但
# parse_result_line 会进一步要求 OK 行必须具备完整且合理的字段。
RESULT_REGEX = re.compile(
    r"RESULT,status=(?P<status>[^,]+)"
    r",type=(?P<type>[^,]+)"
    r"(?:,freq_hz=(?P<freq_hz>[0-9.]+))?"
    r"(?:,vpp_v=(?P<vpp_v>[0-9.]+))?"
    r"(?:,duty_pct=(?P<duty_pct>[0-9.]+|NA))?"
    r"(?:,score_pct=(?P<score_pct>[0-9.]+))?"
    r"(?:,margin_pct=(?P<margin_pct>[0-9.]+))?"
    r"(?:,cycles=(?P<cycles>[0-9]+))?"
    r"(?:,high_clip_samples=(?P<high_clip_samples>[0-9]+))?"
    r"(?:,low_clip_samples=(?P<low_clip_samples>[0-9]+))?"
    r"(?:,invalid_samples=(?P<invalid_samples>[0-9]+))?"
    r"(?:,freq_millihz=(?P<freq_millihz>[0-9]+))?"
    r"(?:,vpp_microvolts=(?P<vpp_microvolts>[0-9]+))?"
    r"(?:,duty_ppm=(?P<duty_ppm>[0-9]+|NA))?"
    r"(?:,sequence=(?P<sequence>[0-9]+))?"
    r"(?:,algorithm_version=(?P<algorithm_version>[0-9]+))?"
    r"(?:,quality_flags=(?P<quality_flags>[0-9]+))?"
)

def find_target_port(preferred_port=None):
    """寻找 TI XDS110 Application/User UART，返回 ``(端口, 说明)``。

    优先级是显式端口、明确的 XDS110 Application、任意非 Auxiliary XDS110、
    常见 COM15、最后才是系统第一个端口。正式测试最好显式传 ``--port``，
    因为最终回退可能选中与开发板无关的串口。
    """
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None, "未检测到任何可用串口，请检查开发板 USB 是否连接。"

    if preferred_port:
        for p in ports:
            if p.device.upper() == preferred_port.upper():
                return p.device, f"使用指定端口: {p.device} ({p.description})"
        return None, f"指定端口不存在: {preferred_port}"

    # 优先寻找 XDS110 Application/User UART
    for p in ports:
        desc = p.description or ""
        if "XDS110" in desc and "Application" in desc:
            return p.device, f"自动匹配到 TI XDS110 用户串口: {p.device} ({p.description})"

    # 其次寻找含有 XDS110 的任何非 Auxiliary 端口
    for p in ports:
        desc = p.description or ""
        if "XDS110" in desc and "Auxiliary" not in desc:
            return p.device, f"匹配到 TI XDS110 端口: {p.device} ({p.description})"

    # 回退到 COM15 (常见 LaunchPad 端口)
    for p in ports:
        if p.device.upper() == "COM15":
            return p.device, f"匹配到 COM15: {p.description}"

    # 默认返回检测到的第一个端口
    return ports[0].device, f"未匹配到明确的 XDS110，默认使用第一个端口: {ports[0].device} ({ports[0].description})"

def parse_result_line(line):
    """严格解析固件 RESULT 行，畸形或自相矛盾时返回 None。

    ``fullmatch`` 要求整行都符合协议；OK 行必须有正且有限的频率、Vpp、
    得分、余量、周期数，方波必须有 0~100% 占空比，非方波必须为 NA，且
    不能同时报告削顶/非法点。失败状态的类型必须是 unknown。
    """
    line = line.strip()
    match = RESULT_REGEX.fullmatch(line)
    if not match:
        return None
    data = match.groupdict()
    status = data.get("status", "UNKNOWN")
    wave_type = data.get("type", "unknown").lower()
    if status != "OK":
        if wave_type != "unknown":
            return None
    elif wave_type not in ("sine", "square", "triangle", "sawtooth"):
        return None
    # 字符串到数字的转换集中在 try 中，任何非法字面量统一判解析失败。
    try:
        res = {
            "status": status,
            "type": wave_type,
            "freq_hz": float(data["freq_hz"]) if data.get("freq_hz") else None,
            "vpp_v": float(data["vpp_v"]) if data.get("vpp_v") else None,
            "duty_pct": float(data["duty_pct"]) if data.get("duty_pct") and data["duty_pct"] != "NA" else None,
            "score_pct": float(data["score_pct"]) if data.get("score_pct") else None,
            "margin_pct": float(data["margin_pct"]) if data.get("margin_pct") else None,
            "cycles": int(data["cycles"]) if data.get("cycles") else None,
            "high_clip_samples": int(data["high_clip_samples"]) if data.get("high_clip_samples") else 0,
            "low_clip_samples": int(data["low_clip_samples"]) if data.get("low_clip_samples") else 0,
            "invalid_samples": int(data["invalid_samples"]) if data.get("invalid_samples") else 0,
            "raw": line,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except (TypeError, ValueError):
        return None
    if status == "OK":
        required = (res["freq_hz"], res["vpp_v"], res["score_pct"],
                    res["margin_pct"], res["cycles"])
        if (any(value is None or not math.isfinite(float(value)) or float(value) <= 0
                for value in required) or
                (wave_type == "square" and
                 (res["duty_pct"] is None or not math.isfinite(res["duty_pct"]) or
                  not 0.0 < res["duty_pct"] < 100.0)) or
                (wave_type != "square" and data.get("duty_pct") not in (None, "NA")) or
                (res["high_clip_samples"] != 0 or res["low_clip_samples"] != 0 or
                 res["invalid_samples"] != 0)):
            return None
    return res

def trigger_and_read(ser, timeout=3.0):
    """清空旧输入，发送 R 触发真实 ADC，并等待 RESULT 后的 READY。

    返回 ``(结果字典或 None, 收到的全部非空文本行)``。保存完整日志便于
    区分“没触发”“处理中超时”“RESULT 畸形”等故障；READY 只在已经得到
    有效 RESULT 后才结束本次读取。
    """
    ser.reset_input_buffer()
    ser.write(b'R')
    start_time = time.time()
    result_data = None
    log_messages = []

    while time.time() - start_time < timeout:
        line_bytes = ser.readline()
        if not line_bytes:
            continue
        try:
            line_str = line_bytes.decode('utf-8', errors='ignore').strip()
        except Exception:
            continue
        if not line_str:
            continue

        log_messages.append(line_str)
        if line_str.startswith("RESULT"):
            result_data = parse_result_line(line_str)
        elif line_str == "READY" and result_data is not None:
            break

    return result_data, log_messages

def display_single_result(res, expected=None):
    """显示一次测量；若提供 expected，再按工程阈值显示真值对比。

    rich 可用时输出彩色表格，否则输出纯文本。这个函数只负责展示，不把
    PASS/FAIL 写回算法，也不会改变 res。
    """
    if not res:
        if HAVE_RICH:
            console.print("[bold red][FAIL] 触发超时或未收到有效 RESULT 响应！[/bold red]")
        else:
            print("[FAIL] 触发超时或未收到有效 RESULT 响应！")
        return

    status_ok = (res["status"] == "OK")
    status_style = "green" if status_ok else "bold red"

    if HAVE_RICH:
        table = Table(title="MSPM0 波形识别结果", border_style="cyan")
        table.add_column("测试参数", justify="right", style="bold cyan")
        table.add_column("实测数值", justify="left", style="white")
        table.add_column("参考/评价", justify="left", style="dim")

        table.add_row("状态 (Status)", f"[{status_style}]{res['status']}[/{status_style}]", "OK 表示有效周期且无削顶")
        
        type_color = {
            "sine": "green",
            "square": "bright_blue",
            "triangle": "yellow",
            "sawtooth": "magenta",
            "unknown": "red"
        }.get(res["type"], "white")
        
        table.add_row("波形类别 (Type)", f"[{type_color} bold]{res['type'].upper()}[/{type_color} bold]", "四类: SINE / SQUARE / TRIANGLE / SAWTOOTH")

        if res["freq_hz"] is not None:
            table.add_row("频率 (Frequency)", f"{res['freq_hz']:.3f} Hz", "初赛范围 100 ~ 400 Hz")
        if res["vpp_v"] is not None:
            table.add_row("峰峰值 (Vpp)", f"{res['vpp_v']:.3f} V", "范围 0 ~ 3.3 V")
        if res["duty_pct"] is not None:
            table.add_row("占空比 (Duty)", f"{res['duty_pct']:.1f} %", "方波指标 20% ~ 80%")
        elif res["type"] != "square":
            table.add_row("占空比 (Duty)", "N/A", "非方波不适用")

        if res["score_pct"] is not None:
            score_color = "green" if res["score_pct"] >= 90.0 else ("yellow" if res["score_pct"] >= 80.0 else "red")
            table.add_row("模板匹配度 (Score)", f"[{score_color}]{res['score_pct']:.1f} %[/{score_color}]", "最佳模板相关度 (越大越吻合)")

        if res["margin_pct"] is not None:
            margin_color = "green" if res["margin_pct"] >= 15.0 else ("yellow" if res["margin_pct"] >= 8.0 else "red")
            table.add_row("置信余量 (Margin)", f"[{margin_color}]{res['margin_pct']:.1f} %[/{margin_color}]", "最佳与次佳模板差值 (>15% 极稳健)")

        if res["cycles"] is not None:
            table.add_row("有效周期数 (Cycles)", f"{res['cycles']}", "相干平均折叠的周期数")

        if res["high_clip_samples"] > 0:
            table.add_row("削顶采样点", f"[red]{res['high_clip_samples']}[/red]", "高电平削顶保护触发！")

        console.print(table)

        # 标称对比只检查用户实际提供的字段，不凭空假设输入信号。
        if expected:
            eval_table = Table(title="标称输入对比判定", border_style="yellow")
            eval_table.add_column("项目", style="cyan")
            eval_table.add_column("预期值", style="magenta")
            eval_table.add_column("实测值", style="white")
            eval_table.add_column("误差", style="yellow")
            eval_table.add_column("判定", style="bold")

            # 类别对比
            if "type" in expected:
                exp_t = expected["type"].lower()
                cur_t = res["type"].lower()
                t_pass = (exp_t == cur_t)
                eval_table.add_row("波形类别", exp_t.upper(), cur_t.upper(), "-", "[green]PASS[/green]" if t_pass else "[red]FAIL[/red]")

            # 频率对比
            if "freq_hz" in expected and res["freq_hz"] is not None:
                exp_f = float(expected["freq_hz"])
                cur_f = res["freq_hz"]
                err_f = abs(cur_f - exp_f)
                err_pct = (err_f / exp_f) * 100.0
                f_pass = err_pct <= 2.0  # 允许 2% 误差
                eval_table.add_row("频率 (Hz)", f"{exp_f:.3f}", f"{cur_f:.3f}", f"{err_f:.3f} Hz ({err_pct:.2f}%)", "[green]PASS[/green]" if f_pass else "[red]FAIL[/red]")

            # 峰峰值对比
            if "vpp_v" in expected and res["vpp_v"] is not None:
                exp_v = float(expected["vpp_v"])
                cur_v = res["vpp_v"]
                err_v = abs(cur_v - exp_v)
                err_v_pct = (err_v / exp_v) * 100.0 if exp_v > 0 else 0
                v_pass = err_v_pct <= 8.0  # 允许 8% 标称误差
                eval_table.add_row("峰峰值 (V)", f"{exp_v:.3f}", f"{cur_v:.3f}", f"{err_v:.3f} V ({err_v_pct:.2f}%)", "[green]PASS[/green]" if v_pass else "[red]FAIL[/red]")

            console.print(eval_table)
    else:
        print("=" * 60)
        print(f"状态: {res['status']} | 类别: {res['type'].upper()}")
        if res['freq_hz']: print(f"频率: {res['freq_hz']:.3f} Hz")
        if res['vpp_v']: print(f"峰峰值: {res['vpp_v']:.3f} V")
        if res['duty_pct']: print(f"占空比: {res['duty_pct']:.1f} %")
        if res['score_pct']: print(f"模板匹配度: {res['score_pct']:.1f} %")
        if res['margin_pct']: print(f"置信余量: {res['margin_pct']:.1f} %")
        if res['cycles']: print(f"有效周期数: {res['cycles']}")
        print("=" * 60)

def run_continuous_test(ser, count=10, interval=0.5, csv_writer=None):
    """连续触发真实采集，统计成功率、类别分布和测量波动。

    没有外部真值时这里的“成功率”是收到 status=OK 的比例，不是分类准确率。
    频率抖动使用本批最大值减最小值；若传入 csv_writer，每个有效解析结果
    会立即追加，减少中途断开导致的数据丢失。
    """
    if HAVE_RICH:
        console.print(f"[bold green]> 开始连续测试 {count} 次，触发间隔 {interval}s...[/bold green]")
    else:
        print(f"> 开始连续测试 {count} 次，触发间隔 {interval}s...")

    results = []
    type_counts = {}
    freqs = []
    vpps = []
    margins = []

    for i in range(1, count + 1):
        res, logs = trigger_and_read(ser)
        if res:
            results.append(res)
            t = res["type"]
            type_counts[t] = type_counts.get(t, 0) + 1
            if res["freq_hz"]: freqs.append(res["freq_hz"])
            if res["vpp_v"]: vpps.append(res["vpp_v"])
            if res["margin_pct"]: margins.append(res["margin_pct"])

            if csv_writer:
                csv_writer.writerow([
                    res["timestamp"], i, res["status"], res["type"],
                    res["freq_hz"], res["vpp_v"], res["duty_pct"],
                    res["score_pct"], res["margin_pct"], res["cycles"]
                ])

            info = f"#{i:02d} [{res['status']}] {res['type'].upper():<8} 频率={res['freq_hz'] or 0:.2f}Hz Vpp={res['vpp_v'] or 0:.3f}V 分类余量={res['margin_pct'] or 0:.1f}%"
            if HAVE_RICH:
                color = "green" if res["status"] == "OK" else "red"
                console.print(f"[{color}]{info}[/{color}]")
            else:
                print(info)
        else:
            if HAVE_RICH:
                console.print(f"[bold red]#{i:02d} 响应超时或失败！[/bold red]")
            else:
                print(f"#{i:02d} 响应超时或失败！")
        time.sleep(interval)

    # 只有成功解析的结果参与数值统计，但成功率分母仍是总触发次数 count。
    if not results:
        print("未获取到任何测试数据！")
        return

    valid_count = sum(1 for r in results if r["status"] == "OK")
    success_rate = (valid_count / count) * 100.0

    print("\n" + "=" * 60)
    print("【连续测试统计报告】")
    print(f"总触发次数: {count}, 成功解析次数: {valid_count} ({success_rate:.1f}%)")
    print("类别分布: " + ", ".join([f"{k.upper()}: {v}次 ({v/count*100:.1f}%)" for k, v in type_counts.items()]))
    if freqs:
        avg_f = sum(freqs) / len(freqs)
        max_f, min_f = max(freqs), min(freqs)
        print(f"频率统计: 均值={avg_f:.3f} Hz, 极差={max_f - min_f:.3f} Hz, 抖动={abs(max_f - min_f)/avg_f*100:.2f}%")
    if vpps:
        avg_v = sum(vpps) / len(vpps)
        max_v, min_v = max(vpps), min(vpps)
        print(f"Vpp 统计: 均值={avg_v:.3f} V, 极差={max_v - min_v:.3f} V")
    if margins:
        avg_m = sum(margins) / len(margins)
        print(f"置信余量均值: {avg_m:.1f} % (值越大说明波形分类越明确，无歧义)")
    print("=" * 60 + "\n")

def main():
    """解析命令行、打开串口，并进入单次/连续/交互三种工作流之一。"""
    if not HAVE_SERIAL:
        print("错误: 未安装 pyserial。请运行: pip install pyserial")
        return 1
    parser = argparse.ArgumentParser(description="MSPM0G3507 串口波形识别测试工具")
    parser.add_argument("-p", "--port", type=str, default=None, help="串口号 (如 COM15)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="波特率 (默认 115200)")
    parser.add_argument("-c", "--count", type=int, default=1, help="测试次数 (默认 1，>1 进入连续测试)")
    parser.add_argument("-i", "--interval", type=float, default=0.4, help="连续测试间隔 (秒)")
    parser.add_argument("--type", type=str, default=None, help="预期波形类别 (sine, square, triangle, sawtooth)")
    parser.add_argument("--freq", type=float, default=None, help="预期输入频率 (Hz)")
    parser.add_argument("--vpp", type=float, default=None, help="预期峰峰值 (V)")
    parser.add_argument("--csv", type=str, default="waveform_test_log.csv", help="导出 CSV 文件名")
    parser.add_argument("--interactive", action="store_true", help="进入交互式测试菜单")

    args = parser.parse_args()

    port_name, msg = find_target_port(args.port)
    if not port_name:
        print(f"[错误] {msg}")
        return 1

    if HAVE_RICH:
        console.print(Panel.fit(
            f"[bold cyan]MSPM0G3507 波形识别 串口自动化测试[/bold cyan]\n"
            f"目标端口: [bold green]{port_name}[/bold green] | 波特率: [bold yellow]{args.baud}[/bold yellow]\n"
            f"{msg}",
            border_style="blue"
        ))
    else:
        print(f"=== MSPM0G3507 波形识别 串口测试 ===")
        print(f"目标端口: {port_name}, 波特率: {args.baud}")
        print(f"{msg}")

    # 打开失败时对最常见的“串口被其他软件占用”给出针对性说明。
    try:
        ser = serial.Serial(port_name, args.baud, timeout=1.0)
    except serial.SerialException as e:
        err_msg = str(e)
        if "PermissionError" in err_msg or "拒绝访问" in err_msg or "Access is denied" in err_msg:
            print("\n" + "!" * 70)
            print(f"【串口占用提示】无法打开 {port_name}：拒绝访问。")
            print("原因：检测到电脑上有其他程序正占用该串口（如已打开的「串口调试助手 SerialDebug.exe」或 CCS 终端）。")
            print("解决方法：")
            print(" 1. 请在已经打开的「串口调试助手」窗口中点击「关闭串口」/「断开」；")
            print(" 2. 或直接关闭串口调试助手软件后重新运行本脚本；")
            print(" 3. 如果您想直接用已经打开的「串口调试助手」测试，请在助手中输入字符 R 并发送即可！")
            print("!" * 70 + "\n")
        else:
            print(f"[串口打开失败] {e}")
        return 1

    # 只收集用户显式提供的真值；0 在这里表示“未提供”，比赛合法频率/Vpp
    # 本来也应大于 0。
    expected = {}
    if args.type: expected["type"] = args.type
    if args.freq: expected["freq_hz"] = args.freq
    if args.vpp: expected["vpp_v"] = args.vpp

    # 追加模式保留历次真实采集；utf-8-sig 让 Excel 更可靠地识别中文。
    csv_file = open(args.csv, mode="a", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(csv_file)
    if csv_file.tell() == 0:
        csv_writer.writerow(["时间戳", "序号", "状态", "波形类型", "频率(Hz)", "峰峰值(V)", "方波占空比(%)", "匹配得分(%)", "分类余量(%)", "有效周期数"])

    try:
        if args.interactive:
            print("\n进入交互测试模式:")
            print("  * 按 [回车] 或输入 'r' : 触发单次识别")
            print("  * 输入 'c' 或 'c 10'   : 连续测试 10 次并生成稳定性统计")
            print("  * 输入 'q'             : 退出程序\n")

            while True:
                try:
                    cmd = input("Command [Enter=单次测试 / c=连续10次 / q=退出] > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    break

                if cmd == 'q':
                    break
                elif cmd.startswith('c'):
                    parts = cmd.split()
                    c_num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                    run_continuous_test(ser, count=c_num, interval=args.interval, csv_writer=csv_writer)
                    csv_file.flush()
                else:
                    res, logs = trigger_and_read(ser)
                    display_single_result(res, expected=expected if expected else None)
                    if res:
                        csv_writer.writerow([
                            res["timestamp"], 1, res["status"], res["type"],
                            res["freq_hz"], res["vpp_v"], res["duty_pct"],
                            res["score_pct"], res["margin_pct"], res["cycles"]
                        ])
                        csv_file.flush()
        else:
            if args.count > 1:
                run_continuous_test(ser, count=args.count, interval=args.interval, csv_writer=csv_writer)
            else:
                res, logs = trigger_and_read(ser)
                display_single_result(res, expected=expected if expected else None)
                if res:
                    csv_writer.writerow([
                        res["timestamp"], 1, res["status"], res["type"],
                        res["freq_hz"], res["vpp_v"], res["duty_pct"],
                        res["score_pct"], res["margin_pct"], res["cycles"]
                    ])
            csv_file.flush()
    finally:
        # 即使用户 Ctrl+C 或读取异常，也关闭文件和端口，避免 COM 口一直占用。
        csv_file.close()
        ser.close()
        print(f"\n测试记录已追加保存至: {args.csv}")

if __name__ == "__main__":
    raise SystemExit(main())
