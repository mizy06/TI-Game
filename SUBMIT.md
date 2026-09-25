# 源码包提交说明

本压缩包含 CCS 工程、源码、文档、测试代码、新版主机结果及用于离线对照的采样二进制；保留原工程的 `.git` 历史和串口日志，便于追溯；新增严格矩阵结果。

解压后检查：

    bash tests/run_all_checks_strict.sh

若需要提交到你们的仓库，可在解压目录初始化并推送：

    git init
    git add .
    git commit -m "RT self-test: 200 custom plus 96 challenge cases"
    git remote add origin <你们的仓库地址>
    git push -u origin main

合并前建议在有 CCS/MSPM0 SDK 的电脑上执行：

1. 导入工程并 Clean；
2. Release/O2 构建；
3. 检查 map 文件的 SRAM 和 Flash；
4. 烧录并确认启动横幅含 `FW=cascade-v2.2`、`API=0x00020100`、
   `CPU_HZ=80000000`；
5. PA15 短接 PA25，发送 `RT4` 验证正式级联；`RT1`、`RT2`、`RT3` 与 `RT` 保留历史对照；
6. 按 docs/ACQUISITION_ALGORITHM_INTERFACE.md 完成真实信号源校准和扫参；
7. 分别测量三种算法的板上最坏处理时间，检查新版 map 的栈余量。

主机回归：

    bash tests/run_all_checks_strict.sh

新版权威源是 waveform_analyzer.c/.h 和当前主机测试输出；录制的旧 ADC 二进制只用于路径对照，不能代替标定真值。
