# 上位机三级算法链路

## 协议

开发板仍负责真实 ADC + DMA 采样。串口发送单个 `D` 后，固件执行一次 4096 点采样并返回：

```text
CAPTURING
DUMP,bytes=8192,fnv1a=123456789
<8192 bytes: little-endian uint16 ADC codes>
DUMP_END
READY
```

二进制数据不能用普通串口终端查看，应使用 `tools/host_waveform.py`。脚本先检查字节数和 FNV-1a，再交给算法，避免传输损坏后仍输出结果。

## 构建上位机 DLL

需要安装主机版 LLVM/Clang 或 MinGW GCC。在 PowerShell 中运行：

```powershell
.\tools\build_host_waveform.ps1
```

它会把与固件相同的 `waveform_analyzer.c`、`waveform_cascade.c` 编译为：

```text
tools/waveform_analyzer_host.dll
```

`waveform_analyze_frame()` 默认进入三级级联：L1 快速相位模型与 L2 独立频域模型一致时直接采用；不一致或拒识时运行 L3 鲁棒时域模型。

## 运行

关闭 CCS 串口终端，确认开发板已烧录最新固件，再运行：

```powershell
python tools/host_waveform.py --port COM15
```

输出仍为五列 CSV：

```text
type,frequency_hz,vpp_v,duty_pct,dc_offset_v
```

例如：

```text
sine,200.013,1.998,0,1.650
```

方波第四列为占空比，其他波形为 `0`；识别失败为 `unknown,0,0,0,0`。
