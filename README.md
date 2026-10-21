# TI-game 最新算法

安装 USB 运行依赖：

```powershell
python -m pip install -r requirements-usb.txt
python server.py --port COM15
```

启动时加载 `algorithm_parameters.json` 中的校准数据。进入交互界面后输入命令并回车：

| 命令 | 功能 |
| --- | --- |
| R | 采集 3 帧，使用 3 个进程并行分析，输出一条 RESULT |
| RO | 使用已保存的独立 ADC/ARB 校准，USB 信号源执行最新 296 项决赛矩阵 |
| RT | 上位机合成信号自激测试，不需要连接信号源 |
| RS | 仅执行独立 ADC、幅值传递和 ARB 校准并保存，不运行矩阵 |
| Q | 退出 |

也可以直接执行，例如 `python server.py RS --port COM15`。

RO 矩阵位于 `matrices/finals_296.json`，版本 `finals-official-identifiable-v2-296`，种子 154660146，共 296 项，弱强各 148 项。这是扩展覆盖用决赛矩阵；参数 PDF 规定的正式测试每组为 5 项。样本按正式参数范围设置：100–400 Hz、0.8–1.6 Vpp、1.4–1.8 V 直流偏置；弱组高斯噪声为 10%–20%、带宽 5f–8f，强组为 35%–50%、带宽 1.5f–3f。RO 默认 32 倍回放采样。

高斯噪声标准差按决赛参数说明设为 `σ = noise_ratio * vpp / 6`（`Vn,pp ≈ 6σ`）。噪声在 `Bn` 以内生成，但在直流和每个源谐波周围留出 ±0.18f 保护带；32 倍回放时保留标准差，有限记录的实际峰峰值由噪声样本决定。矩阵审计验证所有保护频点的噪声系数为零。

默认信号源使用 USB VISA：`USB0::0xF4EC::0x1101::SDG6XFCQ900843::INSTR`。可用 `--ro-resource` 指定设备，或 `--ro-transport vxi11 --ro-host 地址` 使用网络。

USB 会话发生 VISA 系统或 I/O 错误时，普通设置和查询会重新打开连接、清除残留响应并最多重试两次；恢复次数写入 `summary.json`，详情保存在 `scpi.jsonl`。超时、波形上传失败和校验失败仍会中止本次任务。

RO 中断后可用 `python server.py RO --port COM15 --ro-resume-from data/<session>/ro-<report>` 续跑。程序校验原矩阵和连续结果记录，默认从最后一项之后继续；已记录的 FAIL 仍视为完成项，避免每次中断都回退重测。需要重测指定项时，使用 `--ro-resume-from-case final_weak-0038`；该项及之后的旧记录会保存在 `resume-history.jsonl`。

R 的每次采集、分析和归档合计最长 15 秒，RO 每项最长 14 秒。默认 3 帧、3 并行，数值库每进程单线程。用 `--r-workers 1` 可串行运行。

RS 需要 ADC 串口和信号源。成功后立即应用新校准；失败保留此前参数。RO 缺少校准时会提示先运行 RS，也可显式传入 `--ro-calibrate auto` 先校准再测试。`--ro-calibrate off` 仅用于调试。

ARB 拟合和独立验证使用同一套未取整的 ADC 电压换算。RS 通过表示这些独立参考通过验证；含噪声矩阵的幅值误差仍需由 RO 实测判定。单路 ADC 的单次观测若包含与干净源基波（或其他源谐波）同频的噪声，无法唯一拆分两者系数：源幅值增加一项、同频噪声反向减少同一项，会产生完全相同的采样序列。因此矩阵必须在源谐波处保护频谱；新矩阵采用 ±0.18f 缺口，不把这类理论上不可辨识的样本当成幅值算法错误。

最新算法在 `tools/unified_solver.py`，幅值估计在 `tools/vpp_spectral.py`，入口为 `server.py`。运行记录写入 `data/`，初始校准参考保留在 `calibration/latest/`。

历史文件已移至项目同级归档目录，位置见 `CLEANUP_ARCHIVE.txt`。当前固件源码和 CCS 工程配置保留，可正常继续开发。

快速检查：

```powershell
python -m unittest discover -s tests -p test_server_commands.py
python -m unittest discover -s tests -p test_identifiable_finals.py
```
