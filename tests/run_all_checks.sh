#!/usr/bin/env bash
# 总回归入口。任何命令失败都会立即停止并向调用者返回非零退出码。
# 从工程根目录执行：bash tests/run_all_checks.sh
set -euo pipefail

# 不依赖当前工作目录：先找到脚本自身目录，再得到工程根目录。
script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

# 只编译 empty.c、不链接也不运行。tests/stubs 中的假 DriverLib 头文件让
# 普通电脑编译器也能检查板端应用的 C99 语法、类型和警告。
cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$script_dir/stubs" -I"$project_dir" \
  -c "$project_dir/empty.c" -o "$script_dir/empty_host_syntax.o"

# 临时目标文件验证完即删除；-f 表示即使文件不存在也不报错。
rm -f "$script_dir/empty_host_syntax.o"

# 编译并运行算法的 C 回归、精度测试和赛事压力测试。
bash "$script_dir/run_host_tests.sh"

# discover 自动寻找 tests 下所有 test_*.py；-B 禁止生成 __pycache__，
# -v 打印每个测试名，失败时更容易定位。
python3 -B -m unittest discover -s "$script_dir" -p 'test_*.py' -v
