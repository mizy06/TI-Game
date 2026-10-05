# 交接清单

1. CCS Clean / Build / Flash；确认新固件与 DUMP v2 协议，板端只负责采样上传。
2. 确認 PA25 输入、共地、XDS110 UART 端口；关闭占用串口的 CCS 终端。
3. 安装 `requirements.txt` 和主机编译器；运行 `python server.py --port COM6`。
4. 输入 R 外测、RT 合成自测、Q 退出；IDE 默认只有波形名 + 四个数，详细状态看最新 data 会话的 server.log。
5. 每帧原始数据、元数据及结果均保存。不要只保留屏幕结果；失败/半帧及完整 UART 字节日志也需要保留。
6. 算法只在电脑执行。修改 C 算法会自动重建主机库；修改采样/参考电压/增益/校准标志需重新烧录。
7. `bash tests/run_all_checks.sh` 和 `python server.py RT` 做软件回归；实际 CCS 构建、发生器输入、标定和端到端时间在硬件上验收。
8. 范围与门限见 SELF_TEST_MATRIX.md；验证步骤见 VALIDATION.md。当前入口只有板端 `R` 采样和主机 `RT` 自激测试。
