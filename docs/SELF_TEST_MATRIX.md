# 296 项主机矩阵与噪声级联矩阵

`tools/self_test.py` 保留原有固定 296 项矩阵，由主机生成 ADC 帧，经与实采 `R` 相同的完整主机算法处理；RO 回放同一矩阵并通过 R 实采。板端只负责采样、DMA 和 UART 上传。

## 主机自激矩阵

默认共 **296 项**，题目 ID 为 `competition-000001` 至 `competition-000296`，顺序和种子保持稳定。

| 分组 | 项数 | 语义 |
| --- | ---: | --- |
| `preliminary` | 200 | 初赛门禁，四类波形各 50 项，`measure` 必须通过 |
| `final_strong` | 96 | 8 个失真/干扰 profile，每组 12 项，全部 `measure`，失败计入 RT 返回码 |

误差按内部 Hz/uV/ppm/ADC counts 计算，终端舍入值不参与验收。每例记录带符号误差、绝对误差、门限、判定和失败原因。

四类波形为 `sine`、`square`、`triangle`、`sawtooth`。矩阵覆盖固定频率、幅值、偏置、相位、方波 duty、采样率、参考电压、增益和强弱噪声 profile。挑战项覆盖削顶、交越失真、独立二/三次谐波、调幅、相干干扰、五次谐波和 INL 参数，全部给出 PASS / FAIL。Vpp 真值针对加入失真后的无噪声信号，DC 真值严格等于 `Case.dc_v` 设置偏置；定义保存在每帧 generated.truth 中。

RT 判定门限：频率 1%，Vpp 初赛 8% / 挑战 10%，方波占空比 2.5 个百分点，偏置 max(20 mV, 5% Vpp)，算法耗时初赛 10 s / 挑战 15 s。白噪声保留全带宽；低通与高通使用各自明确的频带。

```bash
python server.py RT
python server.py RT --suite competition
python server.py RT --case-id competition-000101 --case-id competition-000263
python server.py --list-matrix
```

`--random-count` 为旧命令兼容参数，矩阵固定为 296 项。`--max-cases` 只适合短冒烟运行，不能替代完整矩阵。

## 噪声级联矩阵

`tools/noise_cascade_matrix.py` 保留板载算法时期的三级级联检查，共 **1296 项**：

`12 个 noise profile × 2 个噪声比例 × 4 类波形 × 3 个频率 × 3 个幅值`，方波再乘 `3 个 duty`。

其中六个 Gaussian profile（`white`、`lowpass_5f`、`lowpass_2f`、`bandpass`、`highband`、`looped`）按 20% 噪声比例作为门禁；`spikes`、`narrowband`、`drift`、`harmonics`、`am`、`clipping` 是 50% 强噪声挑战，结果只用于观察。运行前先构建当前主机库：

```bash
powershell -ExecutionPolicy Bypass -File tools/build_host_waveform.ps1
python tools/noise_cascade_matrix.py --library tools/waveform_analyzer_host.dll
```

每一项仍保存确定性 seed、波形真值、级联各阶段结果和失败详情，便于定位算法回归。阈值均为项目工程门限。
