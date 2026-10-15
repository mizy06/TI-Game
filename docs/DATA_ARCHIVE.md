# 原始输入自动存档

默认数据根为工程 `data`，用 `--data-dir` 可以指定另一目录。每次启动创建 UTC 时间 + 随机后缀会话，已有目录不会被复用。所有数据留在本地电脑，不自动删除。

| 路径 | 内容 |
| --- | --- |
| `<会话>/session.json` | 保存格式、创建时间、逐样点 CSV 开关 |
| `<会话>/server.log` | 启动、RT 进度/结论、命令/传输/算法错误 |
| `<会话>/uart_rx.bin` | 完整收到的 UART 字节，包括横幅、帧头、载荷、帧尾、错误行和坏字节 |
| `<会话>/uart/<编号>/samples.adc.bin` | 实际接收的 ADC 载荷，uint16 小端；部分接收也原样保留，包括奇数字节尾 |
| `<会话>/uart/<编号>/capture.json` | 线上帧头、元数据、校验状态、样本长度、算法全部字段、内部五列 output、比赛格式 display_output |
| `<会话>/rt/<编号>/samples.adc.bin` | 合成题目的 ADC 原始输入，同样完整落盘 |
| `<会话>/rt/<编号>/capture.json` | 生成参数/真值、实际噪声、元数据、结果、误差及通过判定 |
| `<会话>/rt-<运行编号>/matrix.json` | 本次所选题目完整参数清单 |
| `<会话>/rt-<运行编号>/results.csv` | 每个 case 的原输出、状态、误差、输入参数和原始帧位置 |
| `<会话>/rt-<运行编号>/results.jsonl` | 每例完整参数、生成真值、元数据、算法字段及判定，逐条 flush/fsync |
| `<会话>/rt-<运行编号>/results.json` | 全部完整结果，包含通过、失败和异常 |
| `<会话>/rt-<运行编号>/summary.json` | 是否跑完、完整默认覆盖标记、分组结果、最坏误差、失败列表 |

接收字节先写 UART 原始文件并 flush/fsync，然后交给解析器；分块 ADC 载荷也落盘并同步，再验证协议和调用算法。JSON 使用临时文件 + 原子替换。进程突然终止时，可从原始字节恢复；capture.json 的 validation 若仍为 receiving，应按未完成处理，实际长度以 binary 文件为准。无法解析的帧头可能没有可拆分的 ADC 载荷，但整段字节仍保存在 uart_rx.bin。

协议有效帧 validation 为 valid，传输损坏或截断为 invalid。算法拒识仍是传输有效输入：validation=valid，algorithm.ok=false，内部 output 为 unknown，display_output 为空。合成坏元数据题的原始生成帧完整保存，由算法进行元数据拒识。

没有 reset_input_buffer 丢弃数据的操作；后台接收线程处理主动按键上传及 RT 期间上传。帧序号重启重复不会覆盖文件。收到错误数据不会调用算法；异常时也不会以一行伪测量替代错误。

## 转为 CSV

```bash
python server.py --port COM6 --sample-csv
python tools/export_capture.py data/<会话>/uart/000001
```

第一条每帧同步保存 samples.csv，第二条随时将某个已有帧转换为同目录 samples.csv。列为 index、time_s、adc_counts、adc_voltage_v、input_voltage_v；时间用线上采样率，电压用线上 Vref/adc_max/gain。没有完整元数据时仍保留码值，无法换算的列留空。坏帧 CSV 仅导出已收到的完整 uint16，不代表有效测量；奇数字节尾仍在二进制中。

默认不生成逐样点 CSV 是为了控制完整 RT 的空间，二进制和 JSON 已保存全部样本及换算信息。296 项仅样本二进制约 2.3 MiB；逐点 CSV 会大得多。磁盘写入失败会报错并停止该次测量，不能悄悄输出没有存档的结果。

## Python 分析

```python
import json
from pathlib import Path
import numpy as np

frame = Path('data/<会话>/uart/000001')
info = json.loads((frame / 'capture.json').read_text(encoding='utf-8'))
raw = (frame / 'samples.adc.bin').read_bytes()
adc = np.frombuffer(raw[:len(raw)//2*2], dtype='<u2')
meta = info['metadata']
time_s = np.arange(len(adc)) / (meta['sample_rate_millihz'] / 1000)
adc_v = adc.astype(float) * meta['reference_microvolts'] / 1e6 / meta['adc_full_scale']
input_v = adc_v / (meta['gain_ppm'] / 1e6)
```

先检查 validation / 实际长度 / algorithm.ok，再分析频率、模型误差或异常样点。这里 gain 表示幅值比，未实现额外的模拟前端偏置扣除；若外接电平搬移电路，需要先标定并扩展协议中的前端偏置模型。

## RT 完整判定和恢复

296 项全部有布尔 passed 字段和 PASS / FAIL / ERROR verdict，不再有 observe。单项算法异常保存 ERROR 后继续；磁盘失败或用户中断则停止，complete=false、not_run 列出未完成用例。CSV / JSONL / JSON 均保存全部结果，summary.json 包含总判定、最坏误差和失败列表。

独立检查存档：

```bash
python tools/verify_rt_archive.py data/<会话>/rt-<运行编号>
```

锯齿波算法诊断含 dc_sampling_uncertainty_microvolts，RT 明细含 dc_sampling_uncertainty_v。这是跳变落在两个采样点之间时半采样间隔对应的偏置相位不确定度，不含噪声和标定误差，不是统计置信区间；该字段不用于改判通过。

## 2.1.2 测量口径

`generated.truth.dc_v` 严格等于 `context.case.dc_v` 设置偏置。`algorithm` 中 Vpp/DC 为 R/RT 完整流程的最终结果；`carrier_result` 是原 C 阶段数值，`envelope_measurement` 保留模型、相干性、残差及连续包络计数。CSV、JSONL、JSON 和逐帧 self_test 都使用最终内部精度值；独立检查工具同时检查设置偏置真值的一致性。

运行结束时逐帧 capture.json、CSV 和 JSONL 都从完整已完成快照原子定稿，再自动执行一致性检查；存档核对失败使运行失败。持续写入时每项仍 fsync，异常/中断继续保留已完成结果。聚合条目中的 capture_info 是可核对的完整逐帧快照，可在外部同步或读取导致文件缺失时恢复记录，而无需重算算法。

## 校准与 RO 报告

正常 R 先保存原始帧，再加载匹配的 ADC 校准。原 `samples.adc.bin` / `metadata` / checksum 不改写；有效数据另存 `samples.calibrated.adc.bin`、`analysis_metadata`、`analysis_payload_file` 与 `analysis_sha256`。`acquisition_calibration_source` 和 `algorithm_parameters_file` 记录采用的持久参数。复算使用成对的样本与元数据，已校准样本不能再次 apply。逐样点 CSV 仍导出原始样本和线上元数据。

RO 报告位于 `<会话>/ro-<编号>/`：

| 路径 | 内容 |
| --- | --- |
| `matrix.json` / `instrument.json` | 原矩阵、仪器身份、映射设置与校准来源 |
| `calibration.json` | ADC 独立参考、原始捕获路径、校准参数与验证 |
| `generator_calibration.json` / `generator-references/` | ARB 校准与验证、参考 DAC 编码和实际配置 |
| `<题目 ID>/rt.adc.bin` | 原 RT ADC 数字样本 |
| `<题目 ID>/generator.i16.bin` / `encoding.json` | 上传 DAC 编码、回放真值及配置回读 |
| `results.csv` / `results.jsonl` / `results.json` | 实采完整结果、误差、PASS/FAIL 和 UART 帧路径 |
| `summary.json` / `scpi.jsonl` | 完成状态、计数、未完成项、关闭输出错误及仪器通信诊断 |

RO 结果的 `metadata` 是算法使用的有效元数据，`wire_metadata` 是板端原值；逐帧 capture.json 的 `metadata` 始终对应原始载荷。无板卡模式的 verdict 为 ENCODED，不能作为真实测量通过的证据。

## 随工程保留的验证数据

`validation/rt/` 保存本次完整 296 项 RT 的会话、所有 ADC 与结果；`validation/latest_ro.zip` 原样压缩保存最近完整实机 RO 会话（2026-10-09 20:14:52，北京时间创建），包含全部 UART、原始及校准 ADC、矩阵、DAC 编码、两种校准和结果。

可在工程外解压 RO，例如 PowerShell：

```powershell
Expand-Archive -LiteralPath validation/latest_ro.zip -DestinationPath ../TI-game-ro-review
```

归档保持原会话目录和报告相对路径。部分诊断字段包含采集时的绝对路径，用于来源追踪；分析时按解压位置定位同名会话。`algorithm_parameters.json` 的 calibration_report 也是来源字符串，R 加载参数不要求该历史路径仍存在。

运行时仍保存全部数据，不设置自动裁剪。新采集会增大目录，长期实验可直接用 `--data-dir` 写到工程外。
