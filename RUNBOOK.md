# 从构建到 R / RT

## 构建固件

在 CCS 中打开工程，执行 Clean 和 Build，再烧录新生成的 `Debug/TI-game.out`。板端只负责 TIMER/ADC/DMA 采样和 UART DUMP v2 上传；波形分类和 RT 自测由电脑端完成。

Windows 的 CCS 构建使用 `tools/ticlang_wrapper.ps1` 和 `tools/sysconfig_wrapper.ps1`。主机 DLL 使用 `tools/build_host_waveform.ps1`；Linux/macOS 使用 `bash tools/build_host_waveform.sh`。

## 启动 server

```bash
python -m pip install -r requirements.txt
python server.py --port COM6
```

把 `COM6` 换成 XDS110 Application/User UART。输入 `R` 采集一帧，输入 `Q` 退出；`RT` 在没有硬件时运行主机合成自测。输出保持为五列：

```text
waveform,frequency_hz,vpp_v,duty_pct,dc_offset_v
```

诊断和原始 UART/ADC 数据保存在 `data/<session>/`。旧 DLL 若缺少当前 ABI 导出，server 会先用子进程探测并自动重建，避免 Windows 文件锁导致启动失败。使用 `--dll` 指定外部库时不会覆盖它。

## 软件验证

```bash
python server.py RT --max-cases 8
python -m unittest discover -s tests -p 'test_host_integration.py'
python -m unittest discover -s tests -p 'test_server.py'
```

完整矩阵可直接运行 `python server.py RT`；实际 CCS 构建、烧录、串口和模拟前端仍需在目标硬件上验收。
