# TI 算法替换包 6.0

适用于本次上传的原 4.0 工程，或已完整应用此前 5.0 替换包的工程。仅更新三个算法相关文件，附安装、备份、回滚和可复现验证。**全输出百万分之一仍未实现**，请先阅读 `docs/ALGORITHM_REPORT.md` 的完整数值与失败记录。

解压后执行，路径指向含 `tools`、`server.py`、`algorithm_parameters.json` 的原工程根目录：

```bash
python install_patch.py --project "C:/your/path/TI-game-finals" --check
python install_patch.py --project "C:/your/path/TI-game-finals"
```

安装器核对全部原文件身份，只替换 `tools/unified_solver.py`、`tools/host_waveform.py`、`tools/waveform_server.py`。原文件保存在工程的 `algorithm-backup-before-6.0`；原固件、协议和校准参数不被修改。请先停止服务器。若原文件已被自行编辑，安装器拒绝覆盖，请在与对应已交付版本一致的工程副本中应用。

```bash
python install_patch.py --project "C:/your/path/TI-game-finals" --rollback
```

回滚恢复安装前的 4.0 或 5.0 三个文件，也先检查哈希。重复安装不再写入。若已有备份来自另一源版本，安装器拒绝复用该备份，避免覆盖以前的回滚记录。

运行沿用原依赖 NumPy、SciPy、threadpoolctl、pyserial；未增加第三方依赖：

```bash
python server.py --port COM6 --solver-starts 2 --solver-seconds 10 --r-frames 1 --r-timeout 15
```

COM6 换为实际串口。默认两起点、单帧计算预算 10 秒；R 的采集、计算和存档仍总计 15 秒，超时不提交 RESULT。软件测量不能替代实板验收；目前仍是电脑计算，单片机输出要求的参赛许可尚未确认。

新版本以 5.0 为模型选择基线，增加无需知道回放周期的带外投影参数估计、两段时间窗验证和锯齿重置区间处理。窄带证据不足、白噪声、正弦同频幅值重合或边沿时序不确定时，这个额外观测模型不被接受。`SolverConfig(bandlimited_refine=False)` 可单独关闭它，用于对照。

在补丁目录中验证：

```bash
python checks/run_checks.py --project "C:/your/path/TI-game-finals"
python checks/verify_update.py --project "C:/your/path/TI-game-finals" --scope paired
python checks/verify_update.py --project "C:/your/path/TI-game-finals" --scope fresh
python checks/verify_update.py --project "C:/your/path/TI-game-finals" --scope regression296
python checks/counterexamples.py
python checks/test_installer.py --zip "C:/your/path/TI-game-finals-unified-4.0.zip"
```

历史矩阵和本轮冻结后的新矩阵、各项误差、输入指纹、覆盖审计、测试结果及研究依据均在包中。原始 ADC 和完整工程不重复打包；固定种子可重新生成 ADC。`checks/baseline5.py` 仅供验证脚本对照，生产求解器不调用它。
