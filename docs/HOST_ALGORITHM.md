# 板端原始帧与上位机三级算法

## 一次测量

上位机向 115200 8-N-1 UART 发送一个 `D`。固件用 TIMA0 以标称 20 kS/s 触发 ADC0.CH2，DMA_CH0 采集 4096 个右对齐 12 位样点。采集约 204.8 ms；固件只计算传输校验值，不执行波形分类或物理量估算。

```text
CAPTURING\r\n
DUMP,proto=2,bytes=8192,fnv1a=...,seq=...,fs_millihz=20000000,vref_uv=3300000,gain_ppm=1000000,adc_max=4095,flags=1\r\n
<8192 bytes: 4096 个小端 uint16 ADC 样点，没有前后分隔符>
DUMP_END\r\n
READY\r\n
```

FNV-1a 初值 `2166136261`，依次对实际发送的低字节、高字节执行 `(hash ^ byte) * 16777619`，以 32 位无符号整数回绕。`seq` 每帧递增；`flags` 位 0 表示样点连续、位 1 表示采样时钟已校准、位 2 表示参考电压已校准。`fs_millihz` 的单位是 mHz，`vref_uv` 的单位是 µV，`gain_ppm` 表示 ADC 引脚幅度与输入幅度之比。`R` 和 PB13 也触发相同二进制帧。

Python 端先检查协议版本、长度、帧尾、校验值和 ABI 版本及结构体大小，随后把样点及帧头元数据传给 `waveform_analyze_frame()`。算法的 L1 快速相位模型和 L2 频域模型一致时采用共识；拒识或不一致时运行 L3 鲁棒时域模型。只有算法返回 OK 才输出测量值。

## 构建与调用

```powershell
.\tools\build_host_waveform.ps1
python -m pip install pyserial
python tools\host_waveform.py --port COM6 --loop
```

```bash
./tools/build_host_waveform.sh
python3 -m pip install pyserial
python3 tools/host_waveform.py --port /dev/ttyACM0 --loop
```

`--loop` 每按一次回车请求一帧；省略它则采集一次。主机库变更后重新构建。输出列依次为 `type,frequency_hz,vpp_v,duty_pct,dc_offset_v`。需要保存结果可重定向标准输出；诊断信息写在标准错误输出。检查失败不会把损坏数据交给算法。
