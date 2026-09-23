# RT 自激 200+96 题测试方法

## 1. 烧录前

1. 用 CCS 打开工程并执行 **Project > Clean**、**Build Project**；
2. 烧录刚生成的固件；
3. 用一根杜邦线连接 **PA15（DAC 输出）→ PA25（ADC 输入）**；
4. 打开 XDS110 Application/User UART：**115200、8-N-1、无流控**。

自激连接不要串 1 kΩ，也不要在 PA25 对地加电容。PA15 与 PA25 必须共地。

## 2. 直接运行

串口发送：

```text
RT
```

建议让串口工具一次发送整行 `RT`（可带回车换行），不要缓慢逐字键入。正确
启动时首先看到：

```text
RT-START,cases=296,custom=200,challenge=96,fs_hz=20000,n=4096,dac=PA15,adc=PA25,dump=0
```

随后每题依次输出 `RT-CASE`、`CAPTURING`、`PROCESSING`、`RESULT` 和
`RT-CHECK`。运行中不要再向串口发送字符，也不要按 PB13；任一操作都会安全
中止矩阵。整轮通常约 1–3 分钟。

结束时会输出：

```text
RT-DONE,pass=...,fail=...,custom_pass=...,custom_fail=...,challenge_pass=...,challenge_fail=...
READY
```

`custom_fail=0` 表示 200 道自编门禁题全部通过。challenge 是超出初赛基本
承诺的压力题，应单独查看 `challenge_pass/challenge_fail`。

## 3. 自动保存结果（可选）

关闭其他占用串口的软件后运行：

```bash
python -m pip install pyserial
python tools/rt_self_test.py --port COM15 --timeout 600
```

把 `COM15` 换成实际的 XDS110 Application/User UART。脚本会保存逐题 JSONL、
元数据和最终汇总。该步骤是可选的；只用串口终端发送 `RT` 也能完成测试。

## 4. 查看失败帧

296 题默认只回传文本，避免约 2.4 MiB 二进制数据污染串口终端。需要检查
最近一题的 4096 个 ADC 样点时，先中止或等待矩阵结束，再发送：

```text
D
```

固件会输出 `SAMPLES,bytes=8192,fnv1a=...`，随后发送 8192 字节小端样点。
