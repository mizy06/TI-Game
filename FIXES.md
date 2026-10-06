# 修复记录：2.1.2（设置偏置与完整摆幅）

DC 验收真值严格等于 Case.dc_v 设置值。原均值修正真值已移除。R 和 RT 都从原始 ADC 帧重建干净信号，以完整摆幅表示 Vpp，以包络中点表示设置偏置。

## 改动

- 主机联合拟合周期形状、失真谐波与有相干证据支持的调幅/附加音调，测量完整摆幅。估计器不接收测试参数、失真名称或真值；只使用 ADC 和元数据以及 C 分类/周期。
- 两半记录一致性抑制随机谐波和随机调幅峰；方波分别拟合两种边沿；锯齿在所有回归中避开跳变并由斜坡外推，保留上版消除锯齿缩幅的修复。
- 三层周期一致且 L1/L3 类别一致时保留 L1 长记录频率估计。第 295 项从 99.995 Hz 改为 100.124 Hz，真值 100.137 Hz。
- 最终诊断保留 carrier_result、envelope_measurement、模型/谐波/相干性与残差，供审查 C 阶段和最终结果的差别。
- RT 全部 296 项继续保存 ADC、元数据、完整诊断、CSV/JSONL/JSON、布尔判定和失败原因；异常继续记 ERROR，中断保存 not_run；结束时从完整快照定稿逐帧文件并自动核对，存档错误使运行失败。验收门限保持原值。
- 算法版本 2.1.2，旧 DLL 自动重建。NumPy 已在原 requirements.txt 中，无新增依赖。板端采集固件无需因本次主机测量修改重新烧录。

## 验证

固件 C99 语法检查、两组 C 测试共 4800 帧和 25 项 Python 测试通过。默认及另一种子完整 RT 各 296/296 PASS；96 项关掉噪声和 INL 的偏差对照全部 PASS。完整 RT 全部文件、顺序、逐帧判定、设置 DC 真值、8192 字节载荷与 FNV 校验和已独立核对。

默认 RT：

| 分组 | 最大频率误差 | 最大 Vpp 误差 | 最大占空比误差 | 最大 DC 误差 |
| --- | ---: | ---: | ---: | ---: |
| preliminary | 0.028563% | 1.341866% | 0.1918 pp | 8.5933 mV |
| final_strong | 0.068906% | 2.581567% | 0.4247 pp | 17.3262 mV |

与上传 2.1.1 包的同一 296 份 ADC 输入比较（DC 两版都统一按设置偏置重算）：

| 失真组 | 旧平均 Vpp 偏差 | 新平均 Vpp 偏差 | 新最大 Vpp 误差 |
| --- | ---: | ---: | ---: |
| top_clip | +2.7352% | +0.4052% | 2.5816% |
| bottom_clip | +2.7144% | +0.4874% | 2.3498% |
| two_sided_clip | +4.4906% | +0.5249% | 1.4532% |
| crossover | -1.3454% | +0.1006% | 1.4365% |
| harmonic_2_3 | +5.3325% | +0.0794% | 0.8851% |
| slow_am | -5.0503% | -0.0361% | 1.6109% |
| coherent_interferer | -7.2480% | -0.0770% | 1.4597% |
| fifth_harmonic_interferer | -4.1254% | -0.4312% | 1.5314% |

比较明细在 validation/pipeline_comparison.csv，全部汇总在 validation/audit_summary.json。原版验证记录在 validation_previous_2.1.1/。

## 运行

```powershell
python -m pip install -r requirements.txt
python server.py RT
```

解压后进入 TI-game。首次运行需已有 LLVM/Clang 或 MinGW GCC；包内不带旧 DLL。新运行保存到 data/<会话>/，包内本次验证在 validation/rt/，另一种子在 validation/seed_20261006/，无噪声对照在 validation/bias_controls/。

```powershell
python tools/audit_bias_controls.py
python tools/verify_rt_archive.py <RT报告目录>
```

## 测量范围

Vpp 是重建后的无噪声信号摆幅，周期信号覆盖完整周期，调幅/非整数相干干扰覆盖当前采集记录；不是噪声叠加后的最大最小采样码。DC 以设置偏置验收，独立于失真均值。数字合成和主机测试已验证；实体板与模拟前端本次未实测。严格锁相的锯齿跳变仍有亚采样时刻不确定性，诊断保留半采样间隔对应的 DC 不确定度，它不用于放宽门限。直接历史 C 阶段入口是载波结果，server 的 R/RT 都执行完整包络流程。
