"""Render the revision comparison into a Markdown report and a per-case CSV."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path

ERROR_FIELDS = ("frequency_error_pct", "vpp_error_pct", "duty_error_pp", "dc_error_v")


def quantiles(values, points=(50, 90, 95, 99)):
    if not values:
        return {}
    ordered = sorted(values)
    out = {"n": len(ordered), "max": ordered[-1],
           "mean": sum(ordered) / len(ordered)}
    for point in points:
        index = min(len(ordered) - 1, int(round(point / 100 * (len(ordered) - 1))))
        out[f"p{point}"] = ordered[index]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", type=Path)
    parser.add_argument("--versions", type=Path, help="versions.json with revision metadata")
    parser.add_argument("--appendix", type=Path, help="extra Markdown appended verbatim")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.comparison.read_text(encoding="utf-8"))
    meta = json.loads(args.versions.read_text(encoding="utf-8")) if args.versions else {}
    args.output.mkdir(parents=True, exist_ok=True)
    names = list(data["versions"])
    cases = data["per_case"]
    labels = {name: meta.get("labels", {}).get(name, name) for name in names}

    lines = []
    add = lines.append
    add(f"# {meta.get('title', '版本对比报告')}\n")
    add(f"- 数据源：`{data['report']}`（最近一次实机 RO 采集矩阵，365 项）")
    add(f"- 筛选：`{data['frequency_min_hz']:.0f} Hz <= 源频率 <= {data['frequency_max_hz']:.0f} Hz` "
        f"且 `noise_pp_ratio < {data['noise_pp_ratio_below']:.0%}` → **{data['selected_cases']} 项**")
    add(f"- 共同评价器：当前工作树的 `tools/ro_test.py` + `tools/self_test.py`"
        f"（`final_strong` 门限：频率 1%、Vpp 10%、占空比 2.5 pp、偏置 max(20 mV, 5% Vpp)、15 s）")
    add("")
    add("## 版本\n")
    add("| 代号 | 版本 | 标识 |")
    add("| --- | --- | --- |")
    for name in names:
        info = meta.get("versions", {}).get(name, {})
        add(f"| {name} | {labels[name]} | {info.get('detail', '')} |")
    add("")
    transitions = data["transitions"]
    first, second = names[0], names[1]
    add("## 结论\n")
    for name in names:
        s = data["versions"][name]
        add(f"- **{labels[name]}**：通过 {s['passed']}/{s['total']}"
            f"（{s['passed'] / max(1, s['total']):.1%}），"
            f"分类正确 {s['classified']}/{s['total']}，有效输出 {s['valid_outputs']}，"
            f"异常 {s['exceptions']}，平均/最大耗时 "
            f"{s['wall_time_s']['mean']:.2f}/{s['wall_time_s']['max']:.2f} s。")
    add("")
    add(f"逐例转移（{labels[first]} → {labels[second]}）：" +
        "，".join(f"{k} {v}" for k, v in sorted(transitions.items())) + "。")
    lost = transitions.get("PASS->FAIL", 0)
    gained = transitions.get("FAIL->PASS", 0)
    add("")
    add(f"- {labels[second]} 相对 {labels[first]}：新增通过 {gained} 项，"
        f"丢失通过 {lost} 项（{'无回归' if lost == 0 else '**存在回归**'}）。")
    for name in names[2:]:
        other = data["versions"][name]
        add(f"- 参考列 {labels[name]}：通过 {other['passed']}/{other['total']}"
            f"（{other['passed'] / max(1, other['total']):.1%}）。")
    add("")
    add("## 总览\n")
    header = ["指标"] + [f"{labels[n]}" for n in names]
    add("| " + " | ".join(header) + " |")
    add("| " + " | ".join(["---"] * len(header)) + " |")
    rows = [
        ("通过 / 总数", lambda s: f"{s['passed']} / {s['total']}"),
        ("通过率", lambda s: f"{s['passed'] / max(1, s['total']):.1%}"),
        ("分类正确", lambda s: f"{s['classified']} / {s['total']}"),
        ("有效输出（非 unknown/异常）", lambda s: str(s["valid_outputs"])),
        ("异常", lambda s: str(s["exceptions"])),
        ("平均耗时 s", lambda s: f"{s['wall_time_s']['mean']:.2f}"),
        ("最大耗时 s", lambda s: f"{s['wall_time_s']['max']:.2f}"),
    ]
    for title, getter in rows:
        add("| " + " | ".join([title] + [getter(data["versions"][n]) for n in names]) + " |")
    add("")
    add("## 判定转移（" + labels[names[0]] + " → " + labels[names[1]] + "）\n")
    add("| 转移 | 例数 |")
    add("| --- | ---: |")
    for key, value in sorted(data["transitions"].items()):
        add(f"| {key} | {value} |")
    add("")
    for field, title in (("groups", "分组"), ("by_wave", "波形")):
        add(f"## 按{title}\n")
        keys = sorted({k for n in names for k in data["versions"][n][field]})
        add("| " + " | ".join([title] + [labels[n] for n in names]) + " |")
        add("| " + " | ".join(["---"] * (len(names) + 1)) + " |")
        for key in keys:
            cells = []
            for name in names:
                sub = data["versions"][name][field].get(key)
                cells.append(f"{sub['passed']}/{sub['total']}" if sub else "-")
            add(f"| {key} | " + " | ".join(cells) + " |")
        add("")
    add("## 失败原因分布\n")
    reasons = sorted({r for n in names for r in data["versions"][n]["failures"]})
    add("| 原因 | " + " | ".join(labels[n] for n in names) + " |")
    add("| " + " | ".join(["---"] * (len(names) + 1)) + " |")
    for reason in reasons:
        add(f"| {reason} | " +
            " | ".join(str(data["versions"][n]["failures"].get(reason, 0)) for n in names) + " |")
    add("")
    add("## 有效输出误差分位数\n")
    add("每个字段给出两种口径：**全部用例**（拒识按 Vpp=0 计入，会拉高 Vpp 误差）"
        "与**仅有效输出**（算法未拒识）。占空比只在方波上统计。\n")
    for field in ERROR_FIELDS:
        add(f"**{field}**\n")
        add("| 版本 | 口径 | n | mean | p50 | p90 | p95 | p99 | max |")
        add("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name in names:
            square_only = field == "duty_error_pp"
            all_values = [c["verdicts"][name]["errors"][field] for c in cases
                          if not square_only or c["wave"] == "square"]
            valid_values = [c["verdicts"][name]["errors"][field] for c in cases
                            if (not square_only or c["wave"] == "square")
                            and c["verdicts"][name]["classification"] != "unknown"]
            for label, values in (("全部用例", all_values), ("仅有效输出", valid_values)):
                stats = quantiles(values)
                if not stats:
                    add(f"| {labels[name]} | {label} | 0 | | | | | | |")
                    continue
                add(f"| {labels[name]} | {label} | {stats['n']} | {stats['mean']:.4g} | "
                    f"{stats['p50']:.4g} | {stats['p90']:.4g} | {stats['p95']:.4g} | "
                    f"{stats['p99']:.4g} | {stats['max']:.4g} |")
        add("")
    changed = [c for c in cases if c["left_verdict"] != c["right_verdict"]]
    add(f"## 判定发生变化的用例（{len(changed)} 项）\n")
    if changed:
        add("| 用例 | 分组 | 波形 | 频率 Hz | 噪声 | " +
            " | ".join(f"{labels[n]} 判定 / 输出类型" for n in names) + " |")
        add("| " + " | ".join(["---"] * (5 + len(names))) + " |")
        for c in changed:
            cells = [f"{c['verdicts'][n]['verdict']} / {c['verdicts'][n]['classification']}"
                     for n in names]
            add(f"| {c['case_id']} | {c['group']} | {c['wave']} | {c['frequency_hz']:.1f} | "
                f"{c['noise_pp_ratio']:.2f} | " + " | ".join(cells) + " |")
        add("")
    else:
        add("（无）\n")
    failures = [c for c in cases if not all(c["verdicts"][n]["passed"] for n in names)]
    add(f"## 任一版本未通过的用例（{len(failures)} 项）\n")
    add("| 用例 | 分组 | 波形 | 频率 Hz | 噪声 | 真值类型 | " +
        " | ".join(f"{labels[n]} 输出类型 / 原因" for n in names) + " |")
    add("| " + " | ".join(["---"] * (6 + len(names))) + " |")
    for c in failures:
        cells = []
        for n in names:
            v = c["verdicts"][n]
            cells.append(f"{v['classification']} / {';'.join(v['reasons']) or 'PASS'}")
        add(f"| {c['case_id']} | {c['group']} | {c['wave']} | {c['frequency_hz']:.1f} | "
            f"{c['noise_pp_ratio']:.2f} | {c['wave']} | " + " | ".join(cells) + " |")
    add("")
    if meta.get("notes"):
        add("## 口径与限制\n")
        for note in meta["notes"]:
            add(f"- {note}")
        add("")
    if meta.get("commands"):
        add("## 复现\n")
        add("```bash")
        for command in meta["commands"]:
            add(command)
        add("```")
        add("")
    if args.appendix and args.appendix.exists():
        add(args.appendix.read_text(encoding="utf-8").rstrip())
        add("")
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    with (args.output / "per_case.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        columns = ["case_id", "group", "wave", "frequency_hz", "noise_pp_ratio", "recorded_verdict"]
        for name in names:
            columns += [f"{name}_verdict", f"{name}_class", f"{name}_reasons",
                        f"{name}_elapsed_s"] + [f"{name}_{f}" for f in ERROR_FIELDS]
        writer.writerow(columns)
        for c in cases:
            row = [c["case_id"], c["group"], c["wave"], c["frequency_hz"],
                   c["noise_pp_ratio"], c["recorded_verdict"]]
            for name in names:
                v = c["verdicts"][name]
                row += [v["verdict"], v["classification"], ";".join(v["reasons"]),
                        v["elapsed_s"]] + [v["errors"][f] for f in ERROR_FIELDS]
            writer.writerow(row)
    print(json.dumps(dict(report=str(args.output / "REPORT.md"),
                          csv=str(args.output / "per_case.csv"),
                          versions=labels, selected=data["selected_cases"]),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
