# 2.1.2 验证

本次结果、全部误差和偏差对照见 ../FIXES.md 与 ../validation/audit_summary.json。

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

