# 采集、算法与 server 接口说明

DUMP v2，C/Python ABI 2.1.2（`0x00020102`）。server 是本地命令行程序，通过串口接入采集，通过 Python/ctypes 接入算法。操作、接线与 RO 校准见 [README](../README.md)。

## 1. 调用链与职责

```text
R 或 PB13 按键
  → empty.c: TIMA0 → ADC0 → DMA_CH0 → UART DUMP v2
  → SerialService + RecordingSerial: 原始字节先存档
  → host_waveform.receive_frame: 校验帧头、8192 字节、FNV 和帧尾
  → acquisition_calibration.apply: 若启用原板匹配的保存校准
  → Engine.process（互斥锁）
  → host_waveform.analyze
  → ti_waveform_analyze_frame: C 三级级联 + 主机三角波校核
  → envelope_measurement.refine: 重建最终 Vpp / DC
  → CaptureRecord.result + RESULT 输出
```

RT 生成 ADC 帧后调用同一个 `Engine.process`，不调用串口，不应用真实板卡的保存校准。RO 负责真实信号源联调和校准；正常 R 不需要信号源连接。

采集负责等间隔、连续、无缺样的原始 ADC 码和真实元数据；算法负责分类、测量及拒识；server 负责传输校验、校准顺序、互斥、存档和输出。板端只负责采集上传。

算法采用 L1 快速相位模板、L2 谐波分析、L3 鲁棒周期/时域模板。L1/L2 同意时采用共识，否则运行 L3；频域后备需要高置信谐波证据、周期一致及明确的时域拒识原因。可靠的 L1/L3 同类与三层周期共识可保留 L1 长记录的频率精度。各阶段结果仍可单独查询。

周期搜索可使用轻滤波，形状与幅值拟合使用原始 ADC。时域路径通过 AMDF、交越回归、相位折叠和模板评分识别类别；方波模型搜索占空比与独立的两种边沿，锯齿幅值在避开跳变的斜坡上外推。低模型分数或类别余量导致拒识。

C 先给出载波分类、频率与占空比，Python 包络阶段再重建周期形状、失真谐波及有相干证据支持的调幅/附加音调。估计器仅接收 ADC、元数据和 C 结果，不接收题号、失真名称或真值。两半记录一致性抑制随机谐波与随机包络峰。

最终 Vpp 是重建干净信号的完整摆幅，DC 是包络中点，对应信号源设置偏置；周期信号覆盖完整周期，非周期信号覆盖当前记录。严格锁相的锯齿跳变存在亚采样位置不确定性，诊断记录 `dc_sampling_uncertainty_microvolts`，不用于放宽门限。模拟削顶丢失的信息和单通道不可分的相干分量限制实际测量能力。

## 2. 固件 ↔ server：串口契约

UART：115200 baud，8 数据位、无校验、1 停止位。server 发出单字节 ASCII `R`（`0x52`），不附换行。固件也接受 `r`、`D`、`d`；PB13 可以主动产生同样的采集帧。`RT`、`RO`、`Q` 在 server 本地处理，不向固件发送这些字符串。

成功响应的字节顺序：

```text
CAPTURING\r\n
DUMP,proto=2,bytes=8192,fnv1a=<十进制uint32>,seq=<十进制uint32>,fs_millihz=20000000,vref_uv=3300000,gain_ppm=1000000,adc_max=4095,flags=1\r\n
<紧接帧头的8192字节二进制载荷>
DUMP_END\r\n
READY\r\n
```

上面的载荷占位行不代表额外换行：载荷前只有帧头的 CRLF，载荷结束后**立即**发送 `DUMP_END`，中间不再插入 CRLF。载荷是 4096 个小端 `uint16_t`，12 位无符号码右对齐、按采样时间递增；每点先低字节再高字节。只能按长度读取载荷，不能按换行切分它。

FNV-1a 只校验二进制载荷，不包括文本帧头和帧尾：

```python
value = 2166136261
for byte in payload:
    value = ((value ^ byte) * 16777619) & 0xffffffff
```

帧头字段必须齐全，不能重复或增加未知字段；当前解析器要求恰好以下九项。顺序可以变化，数值使用无符号十进制。

| 线上字段 | Python Metadata 字段 | 默认值、单位与约束 |
| --- | --- | --- |
| `proto` | 不入 Metadata | 固定 2 |
| `bytes` | 不入 Metadata | 固定 8192；当前 server 固定 4096 点 |
| `fnv1a` | `checksum` | 0～4294967295，32 位载荷校验 |
| `seq` | `sequence` | uint32 帧号；重启后可重复，不作为全局唯一 ID |
| `fs_millihz` | `sample_rate_millihz` | 20000000 mHz = 20000 Hz；C 支持 10～50 kS/s |
| `vref_uv` | `reference_microvolts` | 3300000 µV = 3.3 V；C 支持 1.0～5.5 V |
| `gain_ppm` | `gain_ppm` | 1000000 = 1 倍；C 支持 0.1～10 倍 |
| `adc_max` | `adc_full_scale` | 固定 4095，不是 4096 |
| `flags` | `flags` | 0～15；C 必须有 bit0 |

帧头解析器的数值接收范围比 C 校验更宽，线上帧解析成功不等于算法元数据有效；不满足 C 范围会返回 `BAD_METADATA`。标志 bit0=连续，bit1=采样时钟已校准，bit2=Vref 已校准，bit3=硬件平均。缺少 bit1/bit2 时可测量，但结果有质量提示。增益定义是 ADC 引脚交流幅值 / 被测输入交流幅值；1/2 分压应填 500000。

固件约 500 ms 内 ADC/DMA 未完成或出现 DMA/ADC 错误时，输出 `ERR_ADC_DMA\r\n`，随后输出 `READY\r\n`，不发送 DUMP 帧。启动横幅、空行、`CAPTURING` 和 `READY` 由接收器识别；不要向协议混入任意调试文本。

默认单次 R 等待预算 10 s，串口底层 timeout/write_timeout 为 3 s。只有发令后仍无板卡响应时，server 才在原预算内重连并最多重发一次 R；已进入采集/接收/分析时不重发。后台持续接收并存档，10 s 等待结束不会强制终止 C 运算；迟到完整帧仍可能输出。始终只能有一个线程读取同一串口，不清空接收缓冲。

## 3. server ↔ Python 算法

实现文件：`tools/host_waveform.py`。

```python
@dataclass(frozen=True)
class Metadata:
    sequence: int
    sample_rate_millihz: int
    reference_microvolts: int
    gain_ppm: int
    adc_full_scale: int
    flags: int
    checksum: int

load_api(path: str)                     # 加载已构建库并检查 ABI
analyze(dll, payload: bytes, meta: Metadata) -> tuple[str, dict]
classify(dll, payload: bytes, meta: Metadata) -> str
receive_frame(port, *, first_line=None, store=None)  # 返回 payload, meta, record
```

`tools/waveform_server.py:build_api(path=None)` 负责默认库的自动构建及加载；指定 `--dll` 时直接加载指定库并检查 ABI，不自动重建它。修改 Python 算法不需要重编 C，但需要重启已经导入模块的 server。

`analyze` 输入载荷必须恰好 8192 字节，它将小端 ADC 转为本机 C 数组，调用 C，再调用包络重建。`checksum` 用于传输/追踪，直接调用 `analyze` 本身不重新校验 FNV；外部输入应先通过 `receive_frame`，或自行核对 `fnv1a(payload) == meta.checksum`。

成功返回：

```python
line = "sine,200.013,1.998,0.0,1.650"
details = {
    "ok": True, "type": 0, "status": 0,
    "frequency_millihz": ..., "vpp_microvolts": ..., "duty_ppm": ...,
    # 包含全部 C Result 字段，以及 carrier_result / envelope_measurement
}
```

内部 `line` 是五列：`type,frequency_hz,vpp_v,duty_pct,dc_offset_v`，无 RESULT 前缀。分类失败返回 `("unknown,0,0,0,0", details)`，其中 `ok=False`，`status` 给出原因。载荷长度错、ABI 错或包络重建异常会抛出异常，由 server 保存错误并报告；不要伪造成功值。

`Engine.process(payload, meta, record)` 返回 `(line, details, elapsed_seconds)`，锁住完整分析和输出/记录。C 各路径有共享静态工作区，不能并发调用；独立应用也必须用同一锁串行调用，或使用独立进程。

`details` 必须保留 C 字段的类型和单位，成功结果须保持 `algorithm_version`、`sequence` 和枚举一致。完整独立调用示例见 `examples/analyze_frame.py`。

## 4. Python ↔ C ABI

头文件：`waveform_analyzer.h`；动态库导出：`tools/host_api.c`；Python 镜像结构：`Frame` / `Result`。

```c
bool ti_waveform_analyze_frame(const waveform_frame_t *frame,
                              waveform_result_t *result);
uint32_t ti_waveform_abi_version(void);
uint32_t ti_waveform_frame_size(void);
uint32_t ti_waveform_result_size(void);
```

`ti_waveform_analyze_frame` 是正式主机入口，包含 C 级联与主机三角波高次谐波校核。其他导出 `ti_waveform_analyze_frame_with_algorithm`、`ti_waveform_analyze_frequency`、`ti_waveform_analyze_robust`、`ti_waveform_analyze_cascade` 供阶段诊断；直接 C 调用得到的是载波阶段结果，最终 server 幅值还需要 Python `refine`。

`waveform_frame_t` 按如下顺序使用本机默认对齐，不使用 packed：

| 字段 | C 类型 | 单位/要求 |
| --- | --- | --- |
| samples | const uint16_t * | 调用期间有效且只读；按时间排列的 ADC 码 |
| sample_count | uint16_t | C 接受 1024～4096；当前 server 固定 4096 |
| adc_full_scale | uint16_t | 4095 |
| sample_rate_millihz | uint32_t | mHz |
| adc_reference_microvolts | uint32_t | µV；对应 Metadata.reference_microvolts |
| frontend_gain_ppm | uint32_t | ppm；对应 Metadata.gain_ppm |
| sequence | uint32_t | 回传到结果 |
| flags | uint32_t | 采集事实标志 |

样本指针只在同步调用期间使用，C 不释放、不持有返回后的指针。调用期间不可被 DMA 或其他线程改写。

`waveform_result_t` 的完整字段顺序、宽度和枚举以头文件为准；必须与 Python `Result._fields_` 一致。主要输出单位：

| 字段 | 单位/含义 |
| --- | --- |
| type | 0 sine、1 square、2 triangle、3 sawtooth、4 unknown |
| status / ok | status=0 且函数返回 true 才视为有效 |
| frequency_millihz | mHz；除以 1000 得 Hz |
| vpp_microvolts / vpp_millivolts | µV / mV，被测输入端摆幅 |
| duty_ppm / duty_permille | 周期 ppm / 千分比；除以 10000 / 10 得 %；非方波为 0 |
| period_q16_samples / vpp_counts_q16 | 采样点 / ADC 码的 Q16；除以 65536 |
| dc_offset_counts | ADC 码；Python refine 后为重建包络中点 |
| algorithm_version / sequence | 当前版本 / 输入帧号 |
| quality_flags | 非致命提示，不能直接当失败判断 |
| best_score_permille / second_score_permille / model_margin_permille | 模型分数与余量，千分比 |

Vpp 换算：`vpp_counts × reference_microvolts / adc_full_scale × 1000000 / gain_ppm`，结果为 µV。频率按实际采样率缩放。当前接口未提供独立模拟前端电平搬移参数；DC 换算只撤销增益。

ABI 探针要求版本=`0x00020102`，两个结构大小与 `ctypes.sizeof` 一致。Python 和动态库的架构/位数必须一致。改变字段、对齐、类型或枚举顺序时，必须同时更新 C 头文件、Python 结构和 ABI 版本，并重新构建库。仅修改实现、保持 ABI 时通常无需改结构。

## 5. 校准顺序与存档

正常 R 先保存线上原始字节及原始元数据，再加载 `algorithm_parameters.json`。文件不存在，或 `schema=1` 且校准及来源均为 null 时，使用标称元数据。成功 RO 更新根目录 `algorithm_parameters.json`；当前文件已经包含实机校准。无校准配置见 `algorithm_parameters.uncalibrated.json`，使用时指定 `--algorithm-parameters`。

应用校准时核对四个 nominal_metadata 字段；不匹配或文件损坏即报错，不悄悄回退。`apply` 按 `(raw - adc_offset_counts) / adc_scale` 修正码值，保留原始两端削顶码，并更新有效采样率、校准 flags、校验和；再把修正后的帧交给算法。不要在输出处再次乘系数，也不要同时在固件和主机重复补偿。

原始 `samples.adc.bin` / `metadata` 保留不变；有主机校准时另外保存 `samples.calibrated.adc.bin` / `analysis_metadata`。复算应选择成对的样本和元数据，已校准样本不得再次 apply。参数中的 `calibration_report` 是来源字符串，运行时不要求该归档文件存在。

算法结果、内部五列 `output`、比赛格式 `display_output`、耗时、校准来源均保存于 `capture.json`。详细布局见 [DATA_ARCHIVE.md](DATA_ARCHIVE.md)。

## 6. server 最终输出与状态

stdout 每次成功只输出一行并立即 flush：

```text
RESULT,sine,200.013,1.998,0
RESULT,square,200.013,1.998,30.0
```

正式格式为 `RESULT,type,frequency_hz,vpp_v,duty_pct`：仅四种小写类别；Hz/V 三位小数、方波占空比一位小数、非方波固定 0；半角逗号、英文小数点、不带单位或 DC、不使用科学计数法。日志与诊断在 stderr / `server.log`。

| status | 名称 | 含义 |
| ---: | --- | --- |
| 0 | OK | 有效测量 |
| 1 | SIGNAL_TOO_SMALL | 幅度不足 |
| 2 | ADC_HIGH_CLIPPED | 高端削顶 |
| 3 | PERIOD_NOT_FOUND | 无稳定周期 |
| 4 | FREQUENCY_OUT_OF_RANGE | 频率超出算法范围 |
| 5 | ADC_LOW_CLIPPED | 低端削顶 |
| 6 | ADC_SAMPLE_INVALID | ADC 码超量程 |
| 7 | CLASSIFICATION_AMBIGUOUS | 分类证据不足 |
| 8 | BAD_ARGUMENT | 指针等调用参数错误 |
| 9 | BAD_METADATA | 元数据或连续标志错误 |
| 10 | INSUFFICIENT_SAMPLES | C 点数范围不符 |

算法拒识时不输出 RESULT，stderr 为 `measurement rejected: status=...`；归档仍有 `unknown,0,0,0,0`。协议损坏/超时不进入算法。质量标志 bit0 时钟未校准、bit1 Vref 未校准、bit2 周期少、bit3 模型余量低、bit4 谐波幅值校核、bit5 斜坡外推、bit6 频率共识、bit7 主机包络测量。

## 7. 修改兼容性清单

保持串口 4096 点 / 8192 字节 / LE16 / DUMP v2，可以单独修改采集实现。C 核心虽支持其他点数，当前 server 并不支持改变线上点数；若必须更改，需同步固件、`SAMPLES` / `PAYLOAD_BYTES`、解析器、ctypes 数组、测试生成器和协议版本。

修改 C 时保留正式导出符号及结构契约；修改包络时保留 `analyze` 返回形态、状态、单位和诊断字段；新增串口帧头字段时必须同步解析器并制定版本兼容处理。server 的互斥、原始存档、校验和拒识输出规则应作为联调验收条件。

最低软件验收：构建主机库、现有 Python 回归、两组 C 回归和完整 296 项 RT。采集修改另外需要 CCS 编译/烧录、实际 R、校验、标定及端到端耗时验证。
