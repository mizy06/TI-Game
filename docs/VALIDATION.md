# 当前验证记录（2026-10-05）

验证目标是确认板端采样工程、Windows 主机库和 server 使用同一套接口。未连接 MSPM0G3507 时，RT 使用确定性合成 ADC 帧；真实 CCS 编译、烧录和硬件串口仍需在目标机上执行。

## 当前检查项

- `python -m py_compile` 覆盖 server、主机工具和测试脚本。
- `tools/build_host_waveform.ps1` 构建 `tools/waveform_analyzer_host.dll`。
- `test_host_integration.py` 覆盖 C ABI、元数据、错误帧和输出格式。
- `test_server.py` 覆盖串口存档、坏校验、主动按键帧、RT 离线运行和 296 项矩阵契约。
- `python server.py RT --max-cases 8` 作为快速 server 冒烟；完整 RT 默认运行 296 项。
- `tools/noise_cascade_matrix.py` 运行 1296 项三级级联噪声矩阵，六个 Gaussian profile 的 20% 比例作为门禁，其余挑战项只观察。

## 判定口径

初赛 200 项为 `measure` 门禁；强噪声 96 项为 `observe`，不会把超出板载范围的挑战结果混入 RT 返回码。频率、峰峰值、占空比和偏置阈值定义在 `tools/self_test.py`，属于项目工程回归门限，不是官方评分承诺。

每次运行的原始帧、矩阵清单、逐项结果和汇总报告保存到 `data/<会话>/`；验证目录和生成物不纳入源码树。
