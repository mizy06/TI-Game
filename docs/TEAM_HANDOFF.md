# 交接清单

1. CCS 导入项目，检查 MSPM0 SDK 2.11.00.07 和 SysConfig，Clean + Build + Flash。
2. 确认启动横幅 `TI-CUP ADC DMA HOST V2`，并确认实际 XDS110 Application/User UART 端口；关闭占用串口的 CCS 终端。
3. Windows 执行 `.\tools\build_host_waveform.ps1`，然后 `python tools\host_waveform.py --port COM6 --loop`。
4. 输出中每行仅包含波形、Hz、Vpp、占空比%、偏置 V。错误请看标准错误输出。
5. 修改算法只编辑 `waveform_analyzer.c/.h` 和 `waveform_cascade.c`，重新构建上位机库；修改采样时钟、参考电压或增益编辑 `acquisition_config.h` 并重烧录固件。
6. `./tests/run_all_checks.sh` 是无板端回归；真实电压输入、串口时序、CCS 固件构建还需在目标硬件上验证。
