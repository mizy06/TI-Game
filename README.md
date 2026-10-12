# TI 杯决赛统一求解器 4.0.0

MSPM0G3507 负责 ADC / DMA / UART，电脑用独立的多起点变量投影最小二乘完成分类和测量。R、RO、RT 默认均使用 `tools/unified_solver.py`，**不调用原 C 分类器、不从三级算法取得类别或初值**。旧 C 源码和 `analyze_legacy()` 只保留作历史验证与对照。默认分析一帧全部 8192 点，兼容旧 4096 点 DUMP v2。

此版本没有证实全部结果达到 1 ppm；同周期循环噪声、锁相方波边沿和未知时基存在不可辨识反例。完整实测软件结果、遗漏检查和失败案例见 [决赛报告](docs/FINALS_UNIFIED_REPORT.md)。工程不是已经验收的单片机独立参赛程序：手册要求单片机输出结果，尚无电脑计算可获准的明确条款。

## 使用

```powershell
python -m pip install -r requirements.txt
python server.py R --port COM6
python server.py RT
python -m tools.finals_matrix --compare
```

交互模式：`python server.py --port COM6`，键入 R / RO / RT / Q。R 默认一帧，总期限 15 秒，保留原始 ADC、校准输入和完整诊断。RO 沿用原独立硬件校准与发生器回放流程，RT 保留原 296 项矩阵。新决赛矩阵用上面的 `finals_matrix` 命令单独运行；它不会连接发生器。

新的可选参数：

```powershell
python server.py R --port COM6 --solver-starts 8 --solver-seconds 10
python server.py R --port COM6 --solver-fmin 100 --solver-fmax 400
python server.py R --port COM6 --r-frames 2
```

频率 100–400 Hz 是手册初赛范围。应在最终范围公布后再设置 `--solver-fmin` / `--solver-fmax`。未指定时，默认下限为帧内至少四周期，上限为采样率的十二分之一；这描述实现范围，不代表决赛承诺。多帧分别拟合，绝不跨 UART 间隙拼接；同相位循环噪声不随帧数自动消失。超时拒绝输出，不提前提交部分结果。

输出格式沿用：

```text
RESULT,sine,237.193000000,1.500000000,0
RESULT,square,237.193000000,1.500000000,63.1730000
```

以上仅为格式示例。幅值定义为拟合的基本波形在噪声叠加前的峰峰值；不会将拟合残差里的任意谐波自动算入源幅值。方波 duty 定义为上升和下降边沿中点之间的高电平时间占比；手册没有详细定义非零边沿的 duty，赛前需确认。

## 固件与校准

采集固件未修改。仍为 32 MHz、20 kS/s、8192 点、115200 8-N-1；导入原 CCS 项目，使用对应 MSPM0 SDK 和 SysConfig 编译。没有新增实板 `.out`，没有实际烧录或重新校准。上传包中的 `algorithm_parameters.json` 原样保留，只适用于原板卡与前端。更换板卡或采集配置后重新 RO 标定；无校准诊断可指定 `--algorithm-parameters algorithm_parameters.uncalibrated.json`。

没有通过 PC 软件测试推断真实 ADC 的 INL、参考电压、时钟温漂、串口驱动时延或 SRAM / DMA 实板行为。`docs/ENGINEERING.md` 为原 3.0 历史说明；当前实现以本文件和 `FINALS_UNIFIED_REPORT.md` 为准。

## 验证

```powershell
python -m unittest discover -s tests -p "test*.py" -v
python -m tools.finals_matrix --compare
```

Linux 完整检查：`bash tests/run_all_checks.sh`，包含 C 警告检查、历史 C 单元测试、库重建、Python 回归和语法检查。重建历史 C 对照库需要 C 编译器；正常 R 不需要 DLL 或 C 编译器。历史 envelope 测试明确调用旧路径，不代表新路径能够恢复任意 AM、削顶或谐波失真。

新增文件：`tools/unified_solver.py`、`tools/finals_matrix.py`、`tests/test_unified_solver.py`、`docs/FINALS_UNIFIED_REPORT.md`。`validation/finals_4.0/` 保存新矩阵、两两覆盖审计、逐项结果、旧算法对照和不可辨识反例；`validation/finals_4.0_original_matrix/` 保存原矩阵的新算法完整回放证据。
