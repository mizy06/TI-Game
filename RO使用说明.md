# 新增 RO：发生器回放原 RT 矩阵，再通过 R 实采

发生器连接与回放在 `tools/ro_test.py`，UART 连接入口在 `tools/waveform_server.py`，独立采集校准在 `tools/acquisition_calibration.py`，ARB 回放电压校准在 `tools/generator_calibration.py`。
固件、波形算法、RT 矩阵、DAC 编码和判定门限保持不变；测试覆盖校准和连接失败路径。

## 使用

发生器使用图片中的 Siglent SDG6022X-E，默认连接 `10.11.13.220:111` 的 VXI-11 `inst0`，默认输出 CH1。
二进制上传以 VXI-11 消息 END 结束，保留数据中的换行字节；不要沿用 `--ro-port 5025`。
电脑以太网设置和接线沿用原工程及图片；CH1 接原 ADC 输入通路，发生器与开发板共地。

在工程根目录启动（将 COM6 换成实际串口）：

```bash
python server.py --port COM6
```

然后在 server 终端输入 `RO` 并回车。也可直接执行：

```bash
python server.py RO --port COM6
```

## 无板卡：只跑发生器编码

板卡串口用于采集校准和 `R` 实采，连接层把串口做成可选：不接板卡、不写 `--port`，或 `--port` 打不开（例如 `COM6` 不存在、被占用），server 都会继续运行，RO 只做“生成 → 编码 → 上传 → 回读校验”，不发送 `R`。

```bash
# 只跑发生器编码，验证原矩阵能被发生器正确接收
python server.py RO
python server.py RO --max-cases 2          # 先小样核对接线与仪器
python server.py RO --case-id competition-000101
```

此时 `summary.json` 的 `mode` 为 `encode-only`、`board` 为 `false`，`counts` 只有 `encoded`，`results.jsonl` 每例的 `result.verdict` 为 `ENCODED`；`matrix.json`、`instrument.json`、`<题目 ID>/rt.adc.bin`、`generator.i16.bin`、`encoding.json` 与整机模式完全一致。`R` 仍然必须有串口，`RT` 仍然纯主机运行。串口打开失败时 stderr 会先打印 `UART <端口> unavailable: ...; continuing without the board`。

当前实机固件 `6.01.01.37R6` 会把 `WVDT? USER,...` 当成内置波形索引，返回空文件名和默认正弦数据。连接层识别这项兼容问题后，每例核验 VXI-11 写入字节数应答、`*OPC?`、`STL? USER` 文件列表及下述播放配置；报告明确记录 `readback.waveform.verified=false`、`byte_readback_supported=false`、`configuration_verified=true`。这表示编码上传和配置核验完成，不能据此声称发生器存储数据逐字节一致。支持具名波形回读的固件仍要求字节完全一致；具名数据不符、文件缺失或配置不符都会停止。

执行顺序：

1. 按原 RT 的选择条件和顺序生成、编码矩阵，保留原始 ADC 样本以及原矩阵参数。
2. 连接发生器，关闭所选通道输出并设为 Hi-Z、正常极性；关闭所选通道的调制、扫频、突发和附加噪声。
3. 有板卡且使用默认 `--ro-calibrate auto` 时，先采集板端元数据，再输出独立 DC 和正弦参考信号，校准和验证采集链路；随后用独立 ARB 参考验证并校准回放电压。任一验证失败时停止，不开始矩阵。
4. 保留该例全部原始样本，并在编码末尾补足载波周期，避免有限记录首尾拼接造成大的载波跳变。上传后选择任意波形，以 TrueArb、零阶保持、原例采样率回放。核对波形名、模式、采样率、幅度、偏置、负载、极性与输出状态。
5. 首例配置成功并开启输出后，终端显示 **开始测试**。
6. 等待输出稳定，然后调用 `SerialService.measure()`，向板端发送原来的单字节 `R`。
7. 等待真实 ADC 数据，在 UART 入口应用已验证的校准，运行原主机算法、按 `RESULT,<type>,<frequency>,<Vpp>,<duty>` 输出一行；保存该例原门限的 PASS/FAIL 判定，再进行下一例。
8. 完成、异常或中断时关闭所选通道输出；重复输入 RO 会创建新的报告并重新校准。

“开始测试”、逐例判定和报告位置显示在 stderr，并同时进入原 `server.log`；FAIL 行附具体失败原因和 ADC 饱和样点数。成功识别的 stdout 格式为 `RESULT,<type>,<frequency>,<Vpp>,<duty>`，type 只允许小写 sine、square、triangle、sawtooth。Hz/V/% 均只写普通十进制数值，不附单位、不使用科学计数法；半角逗号分隔，非方波 duty 固定为 `0`。完整结果行写出后立即刷新终端。不打印直流偏置。拒识显示诊断，不输出 RESULT 行。原内部结果、DC 估计及门限判定继续完整存档，`display_output` 保存实际显示行，拒识时为空。
仪器连接失败或配置核对失败时，不发送 R；UART 超时、坏校验或中断会停止该轮矩阵，记录尚未完成的题目。
正常传输但算法判定 FAIL 时继续后续题目。直接运行返回 0 表示全部通过，1 表示存在测试失败，2 表示运行错误，130 表示中断。

## 独立采集校准（默认）

2026-10-09 的第二轮记录中，120 项均因频率误差超过原 1% 门限而 FAIL，频率整体偏低约 1.2%，另有 26 项 DC 偏差超限。板端上传的是标称 20 kHz、3.3 V、增益 1，flags=1，尚未校准。第一轮 7 个 unknown 的 ADC 电平均值基本不随发生器设定变化，需要检查输入链路是否响应。

默认在矩阵前使用发生器内置参考信号校准，参考信号与所选题目无关：

- 三个 DC 电平（输入满量程的 15%、50%、85%）拟合 ADC 增益和零偏。
- 100 Hz、400 Hz 两个正弦按采样点坐标估算实际采样率。
- 另外两个 DC 电平（30%、70%）和 250 Hz 正弦独立验证电压、频率及 AC 幅度。

参考配置会读回核验；参考捕获不运行分类算法、不输出测量行。平直、削顶、不稳定、输入不跟随参考，或独立验证失败，均拒绝校准并停止该轮，退出时执行发生器输出关闭。发生器或网络故障导致关闭未被确认时，错误保存在 `summary.json` 的 `shutdown_error`。

每次 RO 独立参考校准成功后，将采样率、ADC 增益/零偏和校准来源原子写入工程根目录的 `algorithm_parameters.json`。比赛时启动 server 后直接输入 `R`，或直接执行下列命令，就会使用历史上最近一次成功的 RO 校准参数；重启进程后也有效，R 无需连接发生器：

```bash
python server.py R --port COM15
```

server 启动和每次 R 帧分析前读取参数文件，因此已运行的 R server 也会使用另一个 RO 进程刚保存的新校准。参数来源时间和路径显示在 stderr，并保存在测量归档中。参数文件不依赖历史原始归档仍在原路径；更改 `--data-dir` 不改变默认参数位置，需要独立参数文件时指定 `--algorithm-parameters <文件路径>`。

只有校准验证成功才更新参数；新一轮校准失败或中断、无板卡的 encode-only RO 都不覆盖上次成功校准。若校准成功而后续矩阵存在 FAIL 或中断，成功校准仍保留。每轮完整参考和验证记录继续保存在 `calibration.json`。

首次没有成功校准时，参数为空，R 沿用标称元数据；当前初始文件为此状态，需接回板卡执行一次 RO 获取实际校准。已保存文件损坏、参数校验不符或板端采样率、电压、增益、ADC 满量程元数据变化时，R 拒绝输出测量并报告原因；重新 RO 可生成有效参数。更换板卡或模拟前端后应重新校准。

需要诊断复现原标称元数据时，可仅对该轮 RO 临时关闭校准：

```bash
python server.py RO --port COM15 --ro-calibrate off --max-cases 2
```

`off` 不改写持久参数，该轮 RO 结束后比赛 R 继续使用最近成功校准；off 模式不能消除未校准引起的偏差。校准参数只来自专用参考捕获，不从矩阵测量或 PASS/FAIL 结果拟合。电压校准以发生器为参考，是端到端电压响应校准，不能当作独立测得 VDDA；发生器自身的误差仍会影响结果。原始 ADC 的 0/4095 饱和样点保留在原轨，校准无法恢复已削顶的信息。

## ARB 回放电压校准

2026-10-09 14:50 的重测中，296 项全部应用了成功 ADC 校准，260 个正常识别题目的频率误差均小于 0.13%；仍有 55 项仅 DC 超限，以及 36 项因原始 ADC 高端饱和被拒识。ARB 实采峰峰值整体比回放真值偏高约 2.7%，说明内置参考校准并未验证 ARB 路径的电压响应。

默认 auto 会在 ADC 校准后追加 8 个专用 ARB 参考捕获。前三个常量波形使用满量程 15%、50%、85% 电平，OFST 均保持矩阵工作点 50%；另两个使用中心码并将 OFST 在工作点附近调整至 48%、52%（当前 3.3 V 满量程为 1.584 V、1.716 V）。分开拟合 AMP 增益和局部 OFST 响应，再使用两个独立电平和 250 Hz 正弦验证补偿后的 DC、幅度和频率。每个参考的配置、实际电压和捕获路径均记录在报告中，终端显示 `RO ARB calibration 1/8` 等进度。

2026-10-09 17:06 的实机失败发现：固定 OFST=1.65 V 的三点最大拟合残差仅 0.45 mV，而原 0.99 V、2.31 V 远距离 OFST 参考的中点偏离约 28.8 mV。将不同工作点强行拟合为一条全局直线会误判整体不可校准。因此只使用局部窗口；请求和补偿后的 OFST 均必须落在已校准范围内，超出就停止，不外推。原 6.6 mV DC 拟合/验证门限不放宽，工作点内真实非线性仍拒绝。

此校准只修正该轮发生器的 AMP/OFST 命令；原 DAC 字节、矩阵参数、回放真值和门限不变。`requested_amplitude_v`、`requested_offset_v` 保存原请求，`amplitude_v`、`offset_v` 保存实际补偿命令。所有参考均与矩阵无关，不从 FAIL 数据拟合固定修正。

比赛 R 的 `algorithm_parameters.json` 仍只保存 ADC 校准；ARB 参数保存在该轮 `generator_calibration.json`，不加载到比赛 R。ARB 验证失败则停止该轮并关闭输出，已经成功的 ADC 校准仍保留。无板卡编码和 off 诊断模式不做 ARB 校准。

## 电压映射

原矩阵的不同参考电压、增益是软件 ADC 校准测试参数，无法逐例改变真实板子的供电和模拟前端。
默认 `--ro-level-mode adc` 从原 RT 生成的 ADC 码回放：

`发生器电压 = 原 ADC 码 / 4095 × 当前工程参考电压 / 当前工程前端增益`

当前工程的参考电压和增益读取自 `acquisition_config.h`，不改写该文件。
原样本顺序、噪声、失真、INL 与相对 ADC 电压位置保留；物理幅度和偏置按上述比例转换。
报告同时保存原始真值、映射比例和回放真值。实采结果使用板端元数据及独立参考获得的有效采样率和 ADC 响应，不用原矩阵的虚拟校准值替换它。回放真值保持不变。

若要保留原矩阵的物理电压，可指定 `--ro-level-mode source`；有任一所选题目超过当前板子输入范围时，在连接仪器前报错，必须选安全题目或使用默认 adc 模式。
没有为通过实测而放宽 RT 的门限；编码末尾补充的短段沿用原失真模型和噪声残差，并记录补充点数，不改写原样本。非周期的噪声、调幅和干扰仍是有限记录回放，ADC 与发生器没有硬件同步；实测结果也会受模拟前端、噪声和校准影响。

## 可选参数

```bash
# 只测少量题目，先检查接线；正式运行去掉 --max-cases
python server.py RO --port COM6 --max-cases 2

# 使用 CH2，并增加输出稳定等待
python server.py RO --port COM6 --ro-channel 2 --ro-settle 0.3

# 只复测指定题目；原 --suite/--seed/--case-id/--max-cases 均可用于 RO
python server.py RO --port COM6 --case-id competition-000101

# 修改仪器地址或每次 SCPI/R 等待预算
python server.py RO --port COM6 --ro-host 10.11.13.220 --ro-port 111 --ro-timeout 10
```

## 报告与验证

每轮保存至 `data/<会话>/ro-<编号>/`：

- `matrix.json`：未改写的原 RT 题目参数。
- `instrument.json`：身份读回、连接信息、当前工程电压映射设置。
- `calibration.json`：有板卡且使用 auto 时保存独立参考、原始捕获路径、校准参数、验证结果和失败原因。
- `generator_calibration.json`、`generator-references/`：独立 ARB 电压参数、验证结果、参考 DAC 字节、实际配置和原始捕获路径。
- `<题目 ID>/rt.adc.bin`：该例原 RT 样本；`generator.i16.bin`：上传的有符号小端 DAC 编码；`encoding.json`：编码反算、回放真值和配置读回。
- `results.csv`、`results.jsonl`、`results.json`：真实测量值、误差、判定以及 UART 原始帧路径。
- `summary.json`：完成状态、计数、错误和未完成题目。

真实 UART 字节和 ADC 帧仍由原存档通路保存。每个 `capture.json` 的 `metadata` 和 `samples.adc.bin` 始终对应原始线缆数据；启用校准时另存 `samples.calibrated.adc.bin`，以及 `analysis_metadata`、`analysis_payload_file`、`analysis_sha256` 和 `acquisition_calibration`。`acquisition_calibration_source`、`algorithm_parameters_file` 记录比赛 R 所用校准的来源和参数路径；无法加载或应用时记录 `calibration_error`。有效样本单独重算 checksum，不覆盖原始 checksum。结果行的 `metadata` 为算法使用的有效值，`wire_metadata` 为板端原始值。`--sample-csv` 和原导出工具仍导出原始样本与标称元数据。

运行 `bash tests/run_all_checks.sh` 可执行原回归和新增 RO 自动化检查。
新增测试用本地 VXI-11 RPC 模拟仪器及模拟 UART 接入真实主机 C 算法，并枚举原矩阵检查 DAC 编码反算。
发生器编码及 ADC 独立参考校准已有实机归档。新增 ARB 回放校准和三/四项输出通过软件测试；修改后的 ARB 补偿尚需新的实机 RO 验证。两次诊断见 `10.9FAIL修复记录.md` 和 `10.9RO重测诊断.md`。

协议依据为鼎阳官方《SDG 系列编程手册》的 WVDT、ARWV、SRATE、OUTP 和 *OPC 命令：
https://siglentna.com/wp-content/uploads/dlm_uploads/2024/06/SDG_Programming-Guide_PG02-E05C.pdf
