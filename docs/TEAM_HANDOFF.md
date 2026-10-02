# TI-Game 工作对接文档

本文档用于算法组在另一台 Windows 电脑上安装、运行和修改当前硬件 + 上位机三级算法链路。

## 当前版本

仓库：`https://github.com/mizy06/TI-Game.git`

分支：`hardware-serial-dma`

当前最新提交：`5fd44ba`

相关提交：

- `3f2ed32`：接入三级级联算法和 CSV 输出。
- `81298d4`：增加 `D` 命令，导出真实 ADC + DMA 采样帧。
- `5fd44ba`：增加上位机持续检测模式。

队友算法分支：`algorithm/precision-waveform-analysis`。硬件分支保留 DMA 采样和 UART 链路，算法源文件与队友接口对齐。

## 已完成工作

### 开发板固件

- TIMA0 以 20 kHz 触发 ADC。
- DMA_CH0 采集 4096 个 12 位 ADC 点到单缓冲区。
- UART0：115200，8-N-1。
- `R/r`：真实 ADC + DMA 采样，并在 MCU 上运行三级算法。
- `W/w`：接收 4096 个小端 ADC 点并在 MCU 上分析。
- `T/t`：片上生成测试波形并分析。
- `D/d`：真实 DMA 采样，只导出数据，由上位机分析。

### 三级算法

统一入口是 `waveform_analyze_frame()`，默认调用 `waveform_analyze_cascade()`：

1. L1：`FAST_PHASE_MODEL`
2. L2：独立频域模型
3. L1/L2 拒识或结果不一致时运行 L3 鲁棒时域模型

共同源文件：

```text
waveform_analyzer.c
waveform_analyzer.h
waveform_cascade.c
```

### 输出格式

五列 CSV：

```text
波形,频率Hz,峰峰值V,占空比%,直流偏置V
```

示例：

```text
sine,200.013,1.998,0.0,1.650
square,200.013,1.998,50.0,1.650
```

非方波占空比为 `0.0`；失败为 `unknown,0,0,0,0`。

## 队友电脑安装

安装：

1. Git
2. Python 3.10+
3. LLVM/Clang
4. Visual Studio 2022 Build Tools 的 **Desktop development with C++** 工作负载，包含 MSVC v143 和 Windows 10/11 SDK

命令：

```powershell
winget install LLVM.LLVM
python -m pip install pyserial
```

Visual Studio Installer 中必须勾选 C++ 工作负载。如果出现 `string.h file not found`，说明 MSVC 或 Windows SDK 未安装完整。

获取代码：

```powershell
git clone https://github.com/mizy06/TI-Game.git
cd TI-Game
git switch hardware-serial-dma
git pull --ff-only origin hardware-serial-dma
```

已有仓库则执行：

```powershell
cd D:\github\TI-Game
git fetch origin
git switch hardware-serial-dma
git pull --ff-only origin hardware-serial-dma
```

## 构建上位机 DLL

在工程根目录执行：

```powershell
.\tools\build_host_waveform.ps1
```

成功生成：

```text
tools\waveform_analyzer_host.dll
```

验证接口：

```powershell
python -c "import ctypes; d=ctypes.CDLL(r'tools\waveform_analyzer_host.dll'); print(hasattr(d, 'ti_waveform_analyze_frame'))"
```

应输出 `True`。若找不到 clang，重新打开 PowerShell，或执行：

```powershell
$env:Path += ';C:\Program Files\LLVM\bin'
```

## 固件构建和烧录

需要 CCS、MSPM0 SDK `2.11.00.07` 和兼容 SysConfig。CCS 中执行 Clean、Build，生成：

```text
Debug\TI-game.out
```

关闭串口终端后，右键工程选择 **Flash Project**，烧录该 `.out`。复位后应看到：

```text
FW=hardware-dma-cascade-v2.1,API=0x00020100
ALGO=cascade_rt3_spectral_yin,OUTPUT=CSV5
READY: send R=ADC, D=Dump, W=Inject, T=Simulate
```

## 上位机真实检测

PA25 输入必须保持在 0~3.3 V。关闭 CCS 串口终端，查 XDS110 Application/User UART 的 COM 号：

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

一次检测：

```powershell
python tools\host_waveform.py --port COM6
```

持续检测（启动一次，以后每次按回车采样）：

```powershell
.\tools\run_host_waveform.ps1 -Port COM6
```

按 `Ctrl+C` 退出。COM6 只是示例，必须替换为队友电脑实际的 XDS110 Application/User UART 端口。

每次检测流程：发送 `D`，采集 4096 点，校验 `DUMP,bytes=8192,fnv1a=...`，接收 8192 个小端字节，运行上位机三级算法，输出五列 CSV。

## 如何修改算法

统一修改入口：

```text
waveform_analyzer.c
waveform_analyzer.h
waveform_cascade.c
```

修改后同时验证两端：

```powershell
.\tools\build_host_waveform.ps1
python -m py_compile tools\host_waveform.py
```

MCU 端在 CCS 中重新 Clean、Build、Flash。不要直接编辑 `waveform_analyzer_host.dll`，它是构建产物。

如果修改采样率、ADC 满量程、参考电压或前端增益，必须同步修改 `empty.c` 和 `tools\host_waveform.py` 的 Frame 元数据。

## 协议细节

```text
CAPTURING
DUMP,bytes=8192,fnv1a=十进制校验值
8192 个二进制字节（4096 个 little-endian uint16）
DUMP_END
READY
```

FNV-1a 用于检测串口传输损坏，不是安全认证。二进制段不要用普通串口终端查看。

## 修改提交流程

```powershell
git diff --check
git status
git add <实际修改文件>
git commit -m "describe the change"
git push origin hardware-serial-dma
```

不要提交：`Debug/`、`tools/__pycache__/`、`tools/waveform_analyzer_host.dll`、`tools/waveform_analyzer_host.lib`、`.theia/`、`.tmp_sysconfig_class/`。

算法大改建议先在 `algorithm/precision-waveform-analysis` 测试，再通过提交或 Pull Request 合并到硬件分支。

## 常见问题

| 现象 | 处理 |
|---|---|
| COM 不存在 | 查设备管理器中的 XDS110 Application/User UART |
| PermissionError(13) | 关闭 CCS 串口终端和其他串口助手 |
| `string.h file not found` | 补装 VS C++ 工作负载和 Windows SDK |
| `function ... not found` | 重新运行 DLL 构建脚本，确认导出 `ti_waveform_analyze_frame` |
| `unknown,0,0,0,0` | 检查输入电压、噪声、削顶、频率和接线 |
| 串口无响应 | 确认烧录最新 `Debug\\TI-game.out` 后复位开发板 |

