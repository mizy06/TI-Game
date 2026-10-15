# 当前验证与保留数据

2026-10-11 整理完成后，使用 Python 3.11.9、NumPy 2.4.4、pyserial 3.5 和本机 GCC 验证主工程。固件、C/Python 算法、原工具、回归测试与实机校准参数按 SHA256 核对，共 47 个文件保持不变；原 296 项矩阵的全部参数、ID 和顺序一致。

主工程说明从 19 份合并为 README 和本目录 4 份文档。重复源码包、旧验证轮次、修复前快照、临时目录和编译产物已移出工程；当前接口、操作、校准与测量口径集中保留。

## 本次软件验证

| 检查 | 结果 | 记录 |
| --- | --- | --- |
| 固件 C99 语法，-Wall/-Wextra/-Werror | 通过 | [c_checks.log](../validation/c_checks.log) |
| 两组 C 回归 | 4800 帧，分类/测量/契约失败均为 0 | 同上 |
| 现有 Python 回归 | 65/65 通过，覆盖 R、RO、RT、校准、串口恢复和输出 | [python_tests.log](../validation/python_tests.log) |
| 完整默认 RT | 296/296 PASS，200 初赛 + 96 挑战 | [summary.json](../validation/rt/20261011T134118.444768Z-6ee0dbfa/rt-1791726079093608600/summary.json) |
| RT 原始 ADC、顺序、逐帧判定及校验和 | 296 份全部通过 | [rt_archive_check.json](../validation/rt_archive_check.json) |
| stdout 格式 | 296 行均符合 RESULT,类别,频率,峰峰值,占空比 | [rt_stdout.txt](../validation/rt_stdout.txt) |
| Python 语法与独立算法示例 | 25 个源文件通过；ABI=0x00020102 | [example_stdout.txt](../validation/example_stdout.txt) |

完整 RT 的内部精度最大误差：

| 分组 | 通过数 | 频率 | Vpp | 方波占空比 | DC |
| --- | ---: | ---: | ---: | ---: | ---: |
| preliminary | 200 | 0.028563% | 1.341866% | 0.1918 pp | 8.5933 mV |
| final_strong | 96 | 0.068906% | 2.581567% | 0.4247 pp | 17.3262 mV |

Vpp 真值为加入失真后的无噪声完整摆幅，DC 真值为设置偏置；原频率、Vpp、占空比、DC 和耗时门限均保留。RT 不应用真实板卡的保存校准。

本次固件检查为主机语法检查；没有执行新的 CCS 烧录或实体开发板/发生器复测。实际端到端时间与模拟前端性能以硬件验收为准。

## 保留的完整实机 RO

[latest_ro.zip](../validation/latest_ro.zip) 保存最近完整会话 `20261009T121452.486977Z-44c8caac`，报告 `ro-1791548097871169800`：原记录完成 296 项，296 PASS。2150 个文件与压缩前逐字节一致，包含 UART 原始字节、每帧原始及校准 ADC、全部矩阵和 DAC 编码、两种独立校准与完整结果。

已逐项核对原始/校准载荷长度与校验和，重建原矩阵 ADC/DAC 字节并比较，独立重算内部测量误差与原门限。当前 `algorithm_parameters.json` 与该轮成功 ADC 校准一致。归档中的历史显示行保留原样，当前 RESULT 输出由本次 RT 和回归验证。

| 指标 | 本轮最大误差 | 平均有符号偏差 |
| --- | ---: | ---: |
| 频率 | 0.103000% | +0.014255% |
| Vpp | 7.889010% | -0.242643% |
| 方波占空比（59 项） | 0.5811 pp | -0.000408 pp |
| DC | 29.4142 mV | +2.1228 mV |

这些误差相对于发生器回放真值，包含信号源、采集链路与算法的综合影响；全 PASS 表示满足原门限。发生器固件的具名波形字节回读不受支持，上传应答、文件列表与播放配置已核验，报告保留这一限制。

完整保护文件哈希、矩阵核对、RO 字节与判定核验结果见 [cleanup_audit.json](../validation/cleanup_audit.json)。解压与数据布局见 [DATA_ARCHIVE.md](DATA_ARCHIVE.md)。
