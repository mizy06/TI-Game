# 源码包提交说明

本压缩包是不含 `.git`、旧 Debug 产物和历史测试结果的干净源码包。

解压后检查：

    bash tests/run_all_checks.sh

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
4. 烧录并确认启动横幅含 `FW=algorithm-v2.1`、`API=0x00020100`、
   `CPU_HZ=80000000`；
5. PA15 短接 PA25，分别发送 `RT1`、`RT2`，或发送 `RT` 一键跑两套；
6. 按 docs/ACQUISITION_ALGORITHM_INTERFACE.md 完成真实信号源校准和扫参；
7. 分别测量两种算法的板上最坏处理时间。

主机回归：

    bash tests/run_all_checks.sh

不要把旧的 tools/waveform_analyzer.dll 或历史 tests/results 当作新版算法证据；它们属于原工程审计留档。新版权威源是 waveform_analyzer.c/.h 和当前分支的测试输出。
