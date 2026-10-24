# 方波小数占空比

工作分支 `codex/square-duty-fractional` 从 `origin/main` 的 `40fb7659` 创建。
测试工作目录为 `D:\github\TI-Game-duty`，原工作目录中的未提交修改保持原样。

## 原因和实现

固定 20 kS/s 对 200 Hz 等频率会反复采到同一组相位。方波的跳变落在采样间隙内时，30% 和 30.1% 可以产生完全相同的 ADC 数据。仅修改显示精度或插值无法恢复这部分信息。把采样率固定换到另一数值，也会把盲区移动到新的整数分频频率。

固件为每个采集块保持均匀采样，提供 R/S/T 三种计时器周期：1600/1597/1613 个 32 MHz 时钟，以及 `Udddd\n` 指定的 1500–1700 周期。帧头 bit 3 声明该能力，每帧报告自身采样率。上位机先用首帧的确定平台比例和周期边沿检查识别方波，再采 S 帧，利用 ADC 平台跳变约束测得频率，为第三帧选择相位覆盖更好的周期。其他波形和旧固件仍使用 R/R/R。各帧独立分析，UART 间隙不拼接为连续采样。

方波的上升和下降边沿由前后两个确定平台样本约束。线性规划对全部帧拟合共同频率和占空比，每帧保留独立相位。异步 DAC 更新和 ADC 边沿响应会引入很小的时序偏差，因此先求使全部约束成立的最小时序容差；容差超过 0.02 个采样间隔时拒绝该模型。在此模型下求占空比区间，区间足够窄时取中点。在默认三帧仍无法收窄区间且总时限允许时，补采第四帧。补采后区间仍较宽时保留中点作为估计值，继续报告未收窄状态及区间宽度，避免改用有偏差的单帧频谱估计。只有周期边沿模型不成立时才退回按最大未覆盖相位间隙加权的独立估计，并记录拒绝原因。相位覆盖用多帧测得的频率共同检查，避免单帧频率拟合偏差掩盖盲区。

信号源设置不传入测量算法。时基校准按计时器周期的精确比例转换，原始 ADC 字节和电压校准保持原样。另修复了方波频谱边沿细化中的相位符号。区间依赖平台状态、稳定周期及所拟合的时序容差，不是包含信号源、模拟通路和时钟系统误差的绝对精度置信区间；最终是否达到 0.1% 以独立实机设置和回放真值核验。

## 复现

```powershell
Set-Location D:\github\TI-Game-duty
./tools/build_acquisition.ps1
& C:/ti/ccs2101/ccs/ccs_base/DebugServer/bin/DSLite.exe flash --config=targetConfigs/MSPM0G3507.ccxml --flash --verify --run --timeout=30 tmp/firmware-duty-1600/acquisition.out
python server.py --port COM10 --algorithm-parameters data/duty_current_generator_parameters.json
```

本机信号源为 `USB0::0xF4EC::0x1101::SDG6XFCC900304::INSTR`。仓库原默认值指向另一台设备，运行 RO/RS 时需要显式传入 `--ro-resource`。

专项实测同时记录百分点误差及更严格的相对误差：`abs(测量值-设置值)/设置值*100 < 0.1%`。

```powershell
python -m tools.duty_usb --frequencies 100 200 200.36916 400 --duties 20.1 30.1 79.9 --repeat 2 --parameters data/duty_current_generator_parameters.json
python -m unittest discover -s tests -p test_duty_diversity.py
```

## 全矩阵回放

`tools/replay_matrix.py` 将归档的全部三帧原始 ADC 数据送入公开 `measure_batch` 路径，并核验 SHA-256 和 FNV-1a，不修改原归档。必须有完整的 296 项数据。

```powershell
python -m tools.replay_matrix <RO目录> --output data/replay_candidate
python -m tools.replay_matrix <RO目录> --project <main代码目录> --output data/replay_baseline
python -m tools.compare_replays data/replay_baseline data/replay_candidate --output data/comparison.json
```

旧固件的归档保留其原采样时基。它可检验算法兼容性和其他参数的变化，但无法补回当时没有采到的方波边沿。新固件的效果需由新的 USB 采集验证。

完整测试记录和统计见同目录的 `fractional-duty-validation.md`。

`data/duty_runtime_parameters.json` 的电压参数沿用原工作目录的校准，仅时基采用本机独立参考。两部分来源和哈希见 `data/duty_runtime_provenance.json`。完整动态幅值 RS 曾失败，不能把该组合参数当作对当前信号源的新幅值认证。仓库原 `algorithm_parameters.json` 保持主分支内容。

## 回退

本次烧录前的完整 flash 备份保存在 `tmp/original-acquisition.out`。可用上述 DSLite flash 命令将文件改为此路径恢复。主分支和原工作目录没有合并本次代码。构建产物、日志及原始采集位于本工作目录的 `tmp/`、`data/`。
