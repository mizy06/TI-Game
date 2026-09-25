#!/usr/bin/env bash
# 总回归入口。任何命令失败都会立即停止并向调用者返回非零退出码。
# 从工程根目录执行：bash tests/run_all_checks.sh
set -euo pipefail

# 不依赖当前工作目录：先找到脚本自身目录，再得到工程根目录。
script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

# 路径形式适配：MSYS/Git-Bash 的 pwd 给出 /c/xxx，而 MSYS2 自带的 gcc 只认
# C:/xxx，直接传 /c/... 会报 "No such file or directory"。这里在 Windows 的
# MSYS/Cygwin 环境下把盘符路径转成原生形式；真正的 Linux 上原样返回，
# 不做任何转换，避免误改形如 /a/b 的合法路径。
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

# 只编译 empty.c、不链接也不运行。tests/stubs 中的假 DriverLib 头文件让
# 普通电脑编译器也能检查板端应用的 C99 语法、类型和警告。
cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$script_dir/stubs" -I"$project_dir" \
  -c "$project_dir/empty.c" -o "$script_dir/empty_host_syntax.o"

# 临时目标文件验证完即删除；-f 表示即使文件不存在也不报错。
rm -f "$script_dir/empty_host_syntax.o"

# 边界频率、相位步长、13 s 重试期限使用同一份板端逻辑验证。
cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$script_dir/stubs" -I"$project_dir" \
  -x c "$script_dir/rt_firmware_logic.c.txt" -x none \
  "$project_dir/waveform_analyzer.c" "$project_dir/waveform_cascade.c" \
  -lm -o "$script_dir/rt_firmware_logic"
"$script_dir/rt_firmware_logic"
rm -f "$script_dir/rt_firmware_logic"

# 编译并运行算法的 C 回归、精度测试和赛事压力测试。
bash "$script_dir/run_host_tests.sh"

# discover 自动寻找 tests 下所有 test_*.py；-B 禁止生成 __pycache__，
# -v 打印每个测试名，失败时更容易定位。
python3 -B -m unittest discover -s "$script_dir" -p 'test_*.py' -v

# 直接调用固件所用级联入口，记录三条独立路径和 L3 的真实触发数量。
python3 -B "$project_dir/tools/evaluate_cascade.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --output "$script_dir/results/cascade_296.json"
python3 -B "$project_dir/tools/noise_cascade_matrix.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --output "$script_dir/results/noise_cascade_matrix.json"
