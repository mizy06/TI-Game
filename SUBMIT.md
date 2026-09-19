# 分支提交说明

交付分支：algorithm/precision-waveform-analysis

解压后检查：

    git status
    git branch --show-current
    git log --oneline --decorate -3

预期工作区干净，当前分支为 algorithm/precision-waveform-analysis。

添加你们的远端并推送：

    git remote add origin <你们的仓库地址>
    git push -u origin algorithm/precision-waveform-analysis

如果 origin 已存在：

    git remote set-url origin <你们的仓库地址>
    git push -u origin algorithm/precision-waveform-analysis

合并前建议在有 CCS/MSPM0 SDK 的电脑上执行：

1. 导入工程并 Clean；
2. Release/O2 构建；
3. 检查 map 文件的 SRAM 和 Flash；
4. 烧录并确认启动横幅含 FW=algorithm-v2、API=0x00020000；
5. 按 docs/ACQUISITION_ALGORITHM_INTERFACE.md 完成真实信号源校准和扫参；
6. 测量新版算法的板上最坏处理时间。

主机回归：

    bash tests/run_all_checks.sh

不要把旧的 tools/waveform_analyzer.dll 或历史 tests/results 当作新版算法证据；它们属于原工程审计留档。新版权威源是 waveform_analyzer.c/.h 和当前分支的测试输出。
