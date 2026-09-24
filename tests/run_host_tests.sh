#!/usr/bin/env bash
# 主机算法回归：把同一份 waveform_analyzer.c 编译给电脑 CPU 执行。
# 这验证平台无关的算法逻辑，不等同于 CCS 目标板构建和真实 ADC 校准。
set -euo pipefail

# 用脚本位置推导绝对路径，使用户从任意目录启动脚本都能找到文件。
script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

# 路径形式适配：MSYS/Git-Bash 的 pwd 给出 /c/xxx，而 MSYS2 自带的 gcc 只认
# C:/xxx，直接传 /c/... 会报 "No such file or directory"。这里在 Windows 的
# MSYS/Cygwin 环境下把盘符路径转成原生形式；真正的 Linux 上原样返回。
to_native() {
  case "${OSTYPE:-}" in
    msys* | cygwin* | mingw*)
      case "$1" in
        /[a-zA-Z]/*) printf '%s:/%s' "${1:1:1}" "${1:3}"; return 0 ;;
      esac
      ;;
  esac
  printf '%s' "$1"
}
script_dir="$(to_native "$script_dir")"
project_dir="$(to_native "$project_dir")"

# 常规随机/边界回归。测试源后缀是 .c.txt，所以用“-x c”强制按 C 解析；
# -lm 链接测试数据生成所需的 sin/log/sqrt 等数学函数。
cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -x c "$script_dir/host_test.c.txt" \
  -lm -o "$script_dir/host_test"

"$script_dir/host_test"

# 高精度元数据回归：采样率、参考电压、前端增益和噪声带宽都会变化。
cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -x c "$script_dir/precision_validation.c.txt" \
  -lm -o "$script_dir/precision_validation"

"$script_dir/precision_validation"

# 把核心算法编译为位置无关的共享库，供 Python ctypes 直接调用。
# 这样 Python 压力套件测的是实际 C 核心，不是另写一套近似算法。
cc -std=c99 -O2 -Wall -Wextra -Werror -fPIC -shared \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -lm -o "$script_dir/waveform_analyzer_host.so"

# -B 禁止产生 __pycache__；JSON 参数保存完整可审计报告。
mkdir -p "$script_dir/results"
python3 -B "$project_dir/tools/competition_stress_suite.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --algorithm 1 \
  --json "$script_dir/results/competition_stress_algorithm1.json"

python3 -B "$project_dir/tools/competition_stress_suite.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --algorithm 2 \
  --json "$script_dir/results/competition_stress_algorithm2.json"

python3 -B "$project_dir/tools/competition_stress_suite.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --algorithm 3 --require-challenge \
  --json "$script_dir/results/competition_stress_algorithm3.json"
