# 从构建到 R / RT

## 构建固件

在 CCS 中打开工程，执行 Clean 和 Build，再烧录新生成的 `Debug/TI-game.out`。板端只负责 TIMER/ADC/DMA 采样和 UART DUMP v2 上传；波形分类和 RT 自测由电脑端完成。

Windows 的 CCS 构建使用 `tools/ticlang_wrapper.ps1` 和 `tools/sysconfig_wrapper.ps1`。主机 DLL 使用 `tools/build_host_waveform.ps1`；Linux/macOS 使用 `bash tools/build_host_waveform.sh`。

## 启动 server

```bash
python -m pip install -r requirements.txt
python server.py --port COM6
```

把 `COM6` 换成 XDS110 Application/User UART。输入 `R` 采集一帧，输入 `Q` 退出；`RT` 在没有硬件时运行主机合成自测。非方波输出三项，方波追加占空比：

```text
waveform,frequency_hz,vpp_v
square,frequency_hz,vpp_v,duty_pct
```

诊断和原始 UART/ADC 数据保存在 `data/<session>/`。旧 DLL 若缺少当前 ABI 导出，server 会先用子进程探测并自动重建，避免 Windows 文件锁导致启动失败。使用 `--dll` 指定外部库时不会覆盖它。

输入 R 没有输出时，先看终端 stderr：缺少 `--port` 会立即显示启动方法；ADC/DMA 错误、串口错误和 10 s 超时也直接显示，并保存至 server.log。只有完整有效帧的算法拒识才输出 `unknown,0,0`。无外部波形不影响固定采样，实际浮空输入可能拾取噪声，不能保证总是平直。错误后主命令循环继续等待下一条命令，输入 Q 才退出服务。

## 软件验证

```bash
python server.py RT --max-cases 8
python -m unittest discover -s tests -p 'test*.py'
```

完整矩阵可直接运行 `python server.py RT`；实际 CCS 构建、烧录、串口和模拟前端仍需在目标硬件上验收。

## 查看全部 RT 结果

运行 `python server.py RT`，在最新 `data/<会话>/rt-<运行编号>/` 打开 summary.json 看总判定、results.csv 看全部明细、results.json 看全部内部字段。原始 ADC 在同一会话的 rt/<编号>/。全部用例参与判定，失败时直接命令返回 1；单项算法异常也保存并继续。

包内本次验证在 validation/，原 data/ 保留的是旧版输入和旧版结果。

2.1.2 的 DC 真值为设置偏置；R/RT 均包含 NumPy 包络重建。无噪声偏差对照可运行 `python tools/audit_bias_controls.py`，全部对照数据另存新会话。
