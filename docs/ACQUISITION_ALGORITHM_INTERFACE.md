# ADC 采集侧与算法侧对接协议

协议版本：2.1  
对应头文件：waveform_analyzer.h  
同步入口：waveform_analyze_frame / waveform_analyze_frame_with_algorithm

## 1. 责任边界

采集侧负责：

- 以硬件定时器等间隔触发 ADC；
- 交付连续、无缺样、顺序不乱的右对齐无符号 ADC 码；
- 在调用算法前停止写入目标缓冲；
- 提供实际采样率、ADC 满量程码、参考电压和前端增益；
- 保证输入位于 ADC 合法电压范围，且本帧来自稳定后的周期信号。

算法侧负责：

- 校验帧元数据、ADC 码范围和削顶；
- 分类 sine、square、triangle、下降 sawtooth；
- 输出频率、Vpp，方波额外输出占空比；
- 对不可置信结果拒识并给出状态码；
- 调用期间只读输入缓冲，不持有调用返回后的指针。

## 2. 推荐采集参数

| 项目 | 推荐值 | 允许范围/要求 |
| --- | ---: | --- |
| 采样率 | 20,000.000 Hz | 接口接受 10–50 kS/s；必须传实际校准值 |
| 样本数 | 4096 | 接口接受 1024–4096；本赛题推荐固定 4096 |
| ADC 格式 | 12 bit unsigned | 用 uint16_t 承载，右对齐，0–4095 |
| 触发 | 定时器事件 | 禁止软件 delay 形成非均匀采样 |
| 缓冲 | 连续 uint16_t 数组 | 2 字节对齐；算法调用期间不可写 |
| 推荐模拟带宽 | 约 5–8 kHz | 不得把 400 Hz 方波/锯齿的必要谐波过度滤除 |
| 硬件平均 | 可选 4/8/16 次 | 必须保证一次输出对应一个等间隔时刻，且总采样孔径不会显著钝化边沿 |

固定 20 kS/s、4096 点时，ADC 原始缓冲为 8192 字节，采集约 204.8 ms。算法静态工作区约 13.8 KiB，和采集缓冲合计约 21.8 KiB，给 32 KiB SRAM 留有栈和外设状态空间。

## 3. 缓冲所有权与状态机

| 状态 | 缓冲所有者 | 允许动作 |
| --- | --- | --- |
| IDLE | 采集侧 | 配置元数据、等待触发 |
| CAPTURING | ADC ISR/DMA | 仅写下一个样本；算法不得读取 |
| FRAME_READY | 主循环 | 停止 ADC/DMA 写入，构造 waveform_frame_t |
| ANALYZING | 算法侧只读 | 同步调用；ISR/DMA 不得复用或修改此缓冲 |
| CONSUMED | 采集侧 | 算法返回后可清空或复用缓冲 |

单缓冲方案必须先停定时器和 ADC 写入再分析。双缓冲方案可在算法处理 A 缓冲时采集 B 缓冲，但两个缓冲各自仍必须遵守所有权，且当前工程 32 KiB SRAM 不建议同时放两个 4096 点缓冲和完整算法工作区。

## 4. 输入结构

| 字段 | 类型 | 单位 | 约束与含义 |
| --- | --- | --- | --- |
| samples | const uint16_t * | ADC code | 非空；右对齐无符号码；调用期间保持不变 |
| sample_count | uint16_t | sample | 1024–4096；推荐 4096 |
| adc_full_scale | uint16_t | ADC code | 12 bit ADC 传 4095，不是 4096 |
| sample_rate_millihz | uint32_t | mHz | 20 kS/s 传 20000000；必须是有效输出样本率 |
| adc_reference_microvolts | uint32_t | µV | VDD 作参考时传实测 VDDA；3.300 V 传 3300000 |
| frontend_gain_ppm | uint32_t | ppm | 定义为“ADC 引脚交流幅值 / 被测输入交流幅值”；直通为 1000000 |
| sequence | uint32_t | 无 | 采集侧单调递增帧号，结果原样回传 |
| flags | uint32_t | bit mask | 至少置 CONTIGUOUS；其余按事实置位 |

frontend_gain_ppm 示例：

- 直通或单位增益缓冲：1000000；
- 1/2 分压：500000；
- 2 倍放大：2000000。

算法输出的 Vpp 指被测输入端 Vpp：

\[
V_{in,pp}=\frac{V_{ADC,pp}}{G}
\]

不要把“衰减倍数 2”误传成增益 2000000；1/2 分压的 G 是 0.5，即 500000 ppm。

## 5. 输入标志

| 标志 | 含义 |
| --- | --- |
| WAVEFORM_FRAME_FLAG_CONTIGUOUS | 样本连续、无丢点、无重排；必需 |
| WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED | sample_rate_millihz 来自实测/校准，而非只采用标称值 |
| WAVEFORM_FRAME_FLAG_VREF_CALIBRATED | adc_reference_microvolts 来自实测/校准 |
| WAVEFORM_FRAME_FLAG_HARDWARE_AVERAGED | ADC 已启用硬件平均，供追踪配置使用 |

没有置校准标志不会阻止分类，但结果 quality_flags 会提示绝对频率或绝对电压仍包含比例不确定度。

## 6. 结果结构

兼容字段：

| 字段 | 单位 | 说明 |
| --- | --- | --- |
| type | enum | sine/square/triangle/sawtooth/unknown |
| status | enum | OK 或明确失败原因 |
| frequency_millihz | mHz | 频率主结果 |
| vpp_millivolts | mV | 四舍五入后的兼容结果 |
| duty_permille | 0.1% | 仅方波有效 |
| best_score_permille | 0.001 | 最佳模型相关系数平方 |
| second_score_permille | 0.001 | 次佳类别分数 |
| accepted_cycles | cycle | 用于折叠的完整周期数 |
| high/low_clip_samples | sample | 高/低端削顶计数 |
| dc_offset_counts | ADC code | 稳健上下分位中点，仅作诊断 |
| invalid_samples | sample | 超过 adc_full_scale 的输入码数 |

精度与追踪字段：

| 字段 | 单位 | 说明 |
| --- | --- | --- |
| algorithm_version | packed version | 当前为 0x00020100 |
| sequence | 无 | 回传输入帧号 |
| vpp_microvolts | µV | 推荐使用的 Vpp 结果 |
| duty_ppm | ppm of period | 方波推荐使用的占空比结果 |
| period_q16_samples | sample Q16 | 估计周期，诊断/复核用 |
| vpp_counts_q16 | ADC code Q16 | 校准前幅值，便于区分算法误差和电压标定误差 |
| quality_flags | bit mask | 非致命质量提示 |
| model_margin_permille | 0.001 | 最佳类别与次佳类别分数差 |

非方波的 duty_permille 和 duty_ppm 都为 0。

## 7. 状态码

| 状态 | 含义 | 采集侧动作 |
| --- | --- | --- |
| OK | 有效结果 | 输出并记录 sequence/quality_flags |
| SIGNAL_TOO_SMALL | 稳健幅度不足 | 检查接线、量程、前端增益 |
| ADC_HIGH_CLIPPED | 高端削顶 | 立即降低偏置/幅度或前端增益 |
| ADC_LOW_CLIPPED | 低端削顶 | 调高偏置或降低交流幅度 |
| ADC_SAMPLE_INVALID | 存在超满量程数字码 | 检查 DMA 宽度、端序、缓冲越界 |
| PERIOD_NOT_FOUND | 无稳定周期或周期数不足 | 等信号稳定后重采；检查丢样 |
| FREQUENCY_OUT_OF_RANGE | 估计频率不在赛事范围 | 核对发生器、采样率元数据 |
| CLASSIFICATION_AMBIGUOUS | 类别模型分数或余量不足 | 允许重测；记录分数，不得伪报类别 |
| BAD_ARGUMENT | 空指针等调用错误 | 修复软件，不要重测掩盖 |
| BAD_METADATA | 元数据或标志非法 | 修复对接字段 |
| INSUFFICIENT_SAMPLES | 样本数不在支持范围 | 按协议采满缓冲 |

## 8. 质量标志

| 标志 | 说明 |
| --- | --- |
| WAVEFORM_QUALITY_CLOCK_UNCALIBRATED | 频率还包含采样时钟比例误差 |
| WAVEFORM_QUALITY_VREF_UNCALIBRATED | Vpp 还包含参考电压比例误差 |
| WAVEFORM_QUALITY_FEW_CYCLES | 可用周期少于 8，统计置信度较低 |
| WAVEFORM_QUALITY_LOW_MODEL_MARGIN | 已分类，但最佳/次佳模型余量偏低 |

质量标志是提示，不等同于失败状态。现场输出应同时记录 status 和 quality_flags。

## 9. 默认工程接法

empty.c 已按本协议构造 waveform_frame_t。默认宏位于 waveform_analyzer.h：

- WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ；
- WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS；
- WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM；
- WAVEFORM_ACQUISITION_FLAGS。

采集侧完成校准后，应通过工程编译宏或统一配置头覆盖这些值，不要在结果打印处做“补偿”。校准必须作用于元数据，保证主机注入、板上 ADC 和未来 DMA 路径共用同一换算。

## 10. 串口测试协议

串口 W 注入只用于验证算法和传输：

1. 主机发送单字节 W；
2. MCU 回复 READY_FOR_DATA；
3. 主机发送 4096 个 little-endian uint16_t，共 8192 字节；
4. MCU 回复 DATA,bytes=8192,fnv1a=...；
5. MCU 输出 PROCESSING、RESULT、READY。

W 路径绕过 ADC 和模拟前端，不能证明板上模拟精度。R 路径才是 ADC 端到端测试。生产对接以 C 结构体协议为准，串口文本不是算法 ABI。

## 11. 上板验收

- 用频率计或校准信号源测 100、250、400 Hz，反算实际 sample_rate_millihz；
- 实测 VDDA 或外部 VREF，更新 adc_reference_microvolts；
- 用 1.000 Vpp、2.000 Vpp 标准输入检查线性度和 frontend_gain_ppm；
- 每类至少覆盖 100/250/400 Hz；
- 方波覆盖 20/50/80% 占空比和最快/最慢边沿；
- 覆盖噪声 0%、20%、50%；
- 每点至少重复 20 次，记录均值、标准差、最坏误差和拒识率；
- 独立测量新版固件从 PROCESSING 到 RESULT 的最坏时间；
- 确认 map 文件中 SRAM 总用量小于器件限制并保留足够栈空间。
