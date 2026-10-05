# TI 杯波形识别：单片机采样 + 上位机 server

单片机只执行 TIMA0 → ADC → DMA 连续采样和 UART 上传；波形分类、频率、峰峰值、占空比、偏置估计及自激测试全部在电脑端运行。板端保留 20 kS/s、4096 点、12 位 ADC、115200 8-N-1 的 DUMP v2 协议。算法修改后只需重新构建电脑端库，无需重新烧录单片机。

## 使用

1. 在 CCS 导入此工程，Clean / Build / Flash。板端源文件为 `empty.c`、`empty.syscfg`、`acquisition_config.h`，CCS 已排除算法和 tools/tests。必须保留 `tools` 中的 CCS 构建 wrapper。
2. ADC 输入 PA25，与信号源共地；XDS110 UART 为 PA10/PA11。确认输入在 ADC 电压范围内，关闭占用串口的 CCS 终端。
3. 电脑安装 Python 3.10+、GCC/Clang（Windows 可用 MinGW），在工程根目录安装依赖：

```bash
python -m pip install -r requirements.txt
python server.py --port COM6
```

Windows 把 COM6 换成实际 XDS110 Application/User UART；Linux 可用 `/dev/ttyACM0`。库缺失或源码较新时 server 自动调用现有构建脚本，Windows 使用 PowerShell，Linux/macOS 使用 bash。

在 server 的 IDE 终端输入命令并回车：

| 命令 | 行为 |
| --- | --- |
| `R` | 单片机实采外部输入、串口上传、电脑运行算法并输出一行 |
| `RT` | 电脑生成恢复的 296 项板载算法时期矩阵，每项调用与 R 相同的主机算法 |
| `Q` | 退出 |

默认 IDE 输出仅为原来的五列，不打印表头、菜单、日志或测试总结：

```text
sine,200.013,1.998,0.0,1.650
```

顺序固定为 **波形名,频率 Hz,峰峰值 V,占空比 %,直流偏置 V**。非方波占空比为 `0.0`，算法拒识仍为 `unknown,0,0,0,0`。传输错误不产生虚假的测量结果。进度与错误写入 `data/<会话>/server.log`；需要临时显示诊断可加 `--verbose`（stderr）。

无需硬件也可自测；也支持直接运行一条命令：

```bash
python server.py RT
python server.py R --port COM6
python server.py RT --suite competition
python server.py RT --case-id competition-000101
python server.py RT --max-cases 8
```

`--max-cases` 仅用于短冒烟测试，默认不截断矩阵。`--list-matrix` 只保存题目清单。自测是电脑端合成 ADC 数字样本，不驱动 DAC，也不替代信号发生器、模拟前端和实际 ADC 的测量。

## 自动保存

所有接收到的串口字节在解析前落盘；每帧原始 LE16 样本、采样元数据、完整算法结果独立保存。校验失败、半帧、算法拒识也保留。server 持续接收 PB13 按键上传，包括运行 RT 期间，不清空串口接收缓冲。不同启动、重复帧号和重复 RT 都不会覆盖文件。

默认保存至工程 `data` 目录，与当前启动目录无关；可用 `--data-dir 路径` 改位置。加 `--sample-csv` 可同时生成逐样点时间/ADC 码/电压 CSV。默认二进制样本已经完整保存，方便节省完整矩阵的空间；随时可用 `python tools/export_capture.py data/<会话>/uart/000001` 转换。

## 验证和详细说明

```bash
bash tests/run_all_checks.sh
python server.py RT
```

前者执行固件语法检查、原有两组各 2400 帧 C 回归和 server/串口/存档自动化测试；后者执行完整矩阵。RT 直接命令成功返回 0，断言失败返回 1，运行错误返回 2，中断返回 130。实际 CCS 编译烧录和硬件端到端时间需在开发板上验证。

- [操作步骤](RUNBOOK.md)
- [串口与主机算法](docs/HOST_ALGORITHM.md)
- [296 项测试矩阵与 1296 项噪声矩阵](docs/SELF_TEST_MATRIX.md)
- [数据存档与后续分析](docs/DATA_ARCHIVE.md)
- [最新验证报告](docs/VALIDATION.md)

矩阵参数、工程门限和验证口径记录在 `docs/SELF_TEST_MATRIX.md` 与 `docs/VALIDATION.md`。赛事尚未公布决赛最终幅值、偏置范围和评分阈值，本矩阵使用明确列出的安全输入范围和工程误差门限；连续参数无法被有限题集逐一穷举。
