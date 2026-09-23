# 2026 TI 杯赛道一：高精度波形识别算法分支

交付内容：RT 自激 200 道自编题 + 96 道 challenge 题集
算法接口版本：2.0（0x00020000）

本工程面向 MSPM0G3507，处理采集侧交付的无符号 ADC 数组，识别正弦波、方波、三角波和下降锯齿波，并输出频率、Vpp；方波额外输出占空比。

第一次接触单片机或信号处理，请先读 [波形识别工程零基础导读](BEGINNER_GUIDE.md)。它从采样点、ADC 码值、频率和峰峰值讲起，再按实际调用顺序解释全部算法步骤、定点数单位、串口协议、测试方法和文件地图；源码中也已按同样思路补充逐函数注释。

## 已完成

- 新增版本化采集-算法接口 waveform_frame_t；
- 用 AMDF 基波搜索与滞回交越回归联合估计频率；
- 修复原 1-2-1 FIR 的原地覆写问题；
- 用原始 ADC 点做 128 相位多周期截尾折叠；
- 对全部循环相位做生成模型拟合；
- 方波模型联合搜索占空比和独立 0–10% 上升/下降边沿；
- 按最佳类别模型做仿射最小二乘 Vpp；
- 频率、参考电压和前端增益均由采集元数据传入；
- 保留旧 waveform_analyze 兼容入口；
- 串口 RESULT 增加高精度整数结果、算法版本、帧号和质量标志；
- 培训 PDF、算法研读和对接协议已归档在 docs。

## 文档

- [零基础导读：从 ADC 采样到四类波形识别](BEGINNER_GUIDE.md)
- [算法培训研读、替代方案与最终取舍](docs/ALGORITHM_RESEARCH_AND_DECISIONS.md)
- [ADC 采集侧与算法侧对接协议](docs/ACQUISITION_ALGORITHM_INTERFACE.md)
- [算法分支设计说明](docs/DESIGN.md)
- [权威赛题迁移与初赛压力测试设计](docs/COMPETITION_DERIVED_STRESS_TESTS.md)
- [接线、保护和赛前硬件检查](docs/HARDWARE.md)
- [赛事手册](docs/2026TI杯赛道一赛事手册.pdf)
- [TI 培训——算法部分](docs/TI培训-算法部分.pdf)

## 默认采集

| 项目 | 默认值 |
| --- | ---: |
| ADC | ADC0.CH2 / PA25 |
| 采样率 | 20 kS/s |
| 样本数 | 4096 |
| ADC 格式 | 12 bit unsigned，右对齐 |
| 缓冲大小 | 8192 B |
| 串口 | 115200, 8-N-1 |
| 触发 | R=ADC 对外测量，RT=自激环路自检，W=数字注入，T=片上自测，D=导出样点 |
| 板载波形输出 | DAC12 / PA15（自激时用杜邦线短接到 PA25） |

输入必须全程位于 ADC 合法范围。赛事只保证无负电压，并未保证不会超过 VDDA；接线前要用示波器确认，建议至少串联 1 kΩ 限流。

## 自激环路（RT）

没有信号发生器时，板子自己产生测试波形：DAC12 在 **PA15** 上按 20 kHz
回放 4096 点的已知波形，用一根杜邦线短接到 ADC 输入 **PA25**，同一帧再走
完整的采集 + 识别链路。烧录后在串口发送一条 `RT`，固件会自动跑完
**200 道自编题 + 96 道 challenge，共 296 题**。题集覆盖四类波形、
100–400 Hz、幅度/直流位置/噪声/方波占空比和边沿；challenge 额外加入
削顶、交越、谐波、慢调幅和相干干扰。每个用例上报：

```text
RT-START,cases=296,custom=200,challenge=96,fs_hz=20000,n=4096,dac=PA15,adc=PA25,dump=0
RT-CASE,index=1/296,group=custom,profile=boundary,type=sine,freq_millihz=...,vpp_millivolts=...,...
CAPTURING
PROCESSING
RESULT,status=OK,type=sine,freq_hz=200.195,vpp_v=1.997,...
RT-CHECK,verdict=PASS,type_ok=1,freq_ok=1,vpp_ok=1,duty_ok=1,freq_err_millihz=8,...
...
RT-DONE,pass=...,fail=...,custom_pass=...,custom_fail=...,challenge_pass=...,challenge_fail=...
READY
```

设计要点：

- 产生和采集共用 TIMER_0 的同一个 20 kHz 节拍，第 k 个 ADC 点恒等于第 k 个
  DAC 码，帧内不需要任何对齐；
- 用例频率取 `f = cycles × 20000 / 4096`（cycles 为整数），一帧内正好放下
  整数个周期，换帧无毛刺；
- DAC 与 ADC 都以 VDDA 为参考，参考误差在码域两端抵消；
- 非自激状态下 DAC 输出缓冲置为高阻，自检杜邦线可以一直挂着，不影响对外测量。

296 题默认只输出文本，不向串口终端倾倒二进制样点；需要查看失败帧时可中止
矩阵后发送 `D`，导出最近一帧。主机侧也可用 `tools/rt_self_test.py` 自动保存
逐题汇总。整轮通常需要约 1–3 分钟，取决于板上处理时间和串口终端。

PA15 是 DAC 模拟输出，PA25 是 ADC 输入；UART0（板载 USB-UART 使用
PA10/TX、PA11/RX）负责收命令并向电脑回传识别结果和采样点，不要把 PA15
接到 UART 引脚。外部实时观察可用示波器探测 PA15。

## 推荐调用

    waveform_frame_t frame;
    waveform_result_t result;

    frame.samples = adc_buffer;
    frame.sample_count = 4096;
    frame.adc_full_scale = 4095;
    frame.sample_rate_millihz = 20000000;
    frame.adc_reference_microvolts = 3300000;
    frame.frontend_gain_ppm = 1000000;
    frame.sequence = next_sequence;
    frame.flags = WAVEFORM_FRAME_FLAG_CONTIGUOUS;

    if (waveform_analyze_frame(&frame, &result)) {
        /* 仅在 status == OK 时使用测量值。 */
    }

完整的字段、所有权、校准和错误约定见接口协议。

## 串口输出

成功示例：

    RESULT,status=OK,type=square,freq_hz=250.000,vpp_v=2.000,duty_pct=30.0,score_pct=99.9,margin_pct=20.1,cycles=51,freq_millihz=250000,vpp_microvolts=2000123,duty_ppm=300042,sequence=7,algorithm_version=131072,quality_flags=3

quality_flags=3 表示默认采样时钟和参考电压仍使用标称值；完成校准并正确置标志后应消失。

## 回归测试

在 Linux/macOS 或兼容 shell 中运行：

    bash tests/run_all_checks.sh

当前确定性结果：

| 数据组 | 分类 | 最坏频率误差 | 最坏 Vpp 误差 | 最坏方波占空比误差 |
| --- | ---: | ---: | ---: | ---: |
| 初赛强度 2400 例 | 2400/2400 | 0.0678% | 4.233% | 1.666 pp |
| 强干扰 2400 例 | 2400/2400 | 0.2317% | 5.217% | 0.524 pp |
| 自编初赛门禁 200 例 | 200/200 | 0.0310% | 4.196% | 0.522 pp |

同时运行 96 个赛事迁移挑战例（当前 95/96，非初赛门禁）、29 个协议/题集解析单元测试和 empty.c 主机语法检查。唯一挑战失败包含初赛未承诺的额外相干窄带干扰，算法安全拒识而非误报；详见压力测试设计文档。

这些结果验证的是软件算法模型，不代表新版固件已完成真实 ADC 模拟链路校准。提交前仍需在板上完成真实信号源扫参、处理时间测量、VDDA/采样时钟校准和 Release 构建。

## CCS 构建

1. 安装 Code Composer Studio、MSPM0 SDK 2.11.00.07 和兼容 SysConfig；
2. 导入本目录；
3. Project > Clean；
4. Build Project；
5. 烧录后确认启动横幅包含 FW=algorithm-v2 和 API=0x00020000。

empty.syscfg 仍保持原工程不变；当前环境没有 SysConfig 图形配置与完整 MSPM0 工具链，因此没有伪造板级构建成功。算法和应用主机侧已用 C99、O2、Wall、Wextra、Werror 检查。
