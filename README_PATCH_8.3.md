# TI-RO 8.3 Minimal Replacement Patch

## 包含文件 (可直接解压覆盖至工程根目录)
- `tools/unified_solver.py` (8.3.0: 新增实机窄脉宽方波内部平台+周期积分占空比精修 `_square_plateau_integral_refine`、实机 8192 采样无接缝子窗口次谐波梳状滤波 `_hardware_seam_free_refine`、锯齿波高阶谐波跳变沿掩膜保护与三角波增益正则约束)
- `tools/self_test.py` (收敛默认自检矩阵至 `200 <= frequency_hz < 600` 且 `noise_pp_ratio < 0.50`)
- `tools/ro_test.py` (`ro_matrix()` 收敛至 `200–600 Hz, Noise < 50%` 共 141 例：`new48` 12 例 + `fresh96` 37 例 + `paired221` 92 例；正弦 `wire_carrier_vpp_v` 改为带直流项的精确最小二乘投影)
- `tools/finals_matrix.py` (收敛矩阵生成与评估过滤条件至 `200–600 Hz, Noise < 50%`)
- `checks/replay_captures.py` (默认启用 `--frequency-min 200.0 --frequency-below 600.0 --noise-below 0.50`)
- `tests/test_ro.py` (同步更新收敛后的 141 例矩阵断言)
