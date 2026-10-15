# 研究路线及采用范围

本轮查询和阅读日期：2026-10-13。以下均指论文作者、作者所属机构、论文出版社或官方软件文档；没有把论坛回答当成技术依据。全文、作者讲义、仓库说明与摘要的阅读层级分别注明。没有运行作者的 MATLAB / C++ 基准，也没有声称复现其论文精度。

| 路线 / 一手来源 | 阅读范围与主要启发 | 本包的选择及限制 |
| --- | --- | --- |
| [O'Leary / Rust, Variable Projection for Nonlinear Least Squares Problems](https://www.cs.umd.edu/users/oleary/software/varpro.pdf) | 正文中的变量消去、约化雅可比公式与附录接口：线性变量可消去，频率改变时其子空间也应改变 | 自行实现有界源幅值、直流、干扰系数的投影，以及两项完整导数；未复制论文代码 |
| [Vetterli / Marziliano / Blu, Sampling Signals of Finite Rate of Innovation](https://www.epfl.ch/labs/lcav/wp-content/uploads/2019/01/finite_rate_inov.pdf) | 作者讲义，重点是分段多项式与带限分量的分离，尤其第 43-45 页 | 利用边沿、重置以及噪声子空间外的波形信息；原文的采样核条件不能直接用于本工程原始 ADC，未宣称满足其精确重建定理 |
| [Hirabayashi / Hironaga / Condat, FRI Recovery by Maximum Likelihood Estimation](https://globals.ieice.org/en_transactions/fundamentals/10.1587/transfun.E96.A.1972/_p) | 出版社摘要：比较消去滤波、Cadzow 与非凸 ML / PSO | 说明更换优化方法仍须处理非凸性。本题参数很少，选择确定性相位剖面、多频率起点和约束精化；没有实现或验证 PSO 全局保证 |
| [Quinn / Nielsen / Christensen, fastF0ArMl 作者仓库](https://github.com/jkjaer/fastF0ArMl) | README、使用限制及对应论文摘要；AR 相关噪声下的谐波与 AR 阶数选择 | 参考联合估计与阶数选择思路。循环固定干扰不等于每次独立的 AR 创新；没有直接套用 AR 模型或移植作者 MATLAB 代码 |
| [Nielsen 等, fastF0Nls 作者仓库](https://github.com/jkjaer/fastF0Nls) | 作者仓库说明；周期谐波模型的快速非线性频率估计 | 本包以谐波回归精化初值，但最终频率仍由源波形与干扰联合优化，FFT 不输出最终参数 |
| [Carbone / Schoukens / Moschitta, Dynamic Signal Measurements Based on Quantized Data](https://arxiv.org/html/1804.10027v1) | 量化偏差讨论、概率模型和迭代方法：、已校准转移阈值、QBE 概率估计、频率未知时的迭代方法 | 采用“利用码字区间”的原则，自行设计局部区间一致性细化；循环噪声不能当成独立重复观测来估计 QBE 概率。不是论文 QBE 的直接复刻 |
| [Stoica / Shang / Cheng, CRB for Quantized Data](https://arxiv.org/html/2209.13182v1) | 区间概率与信息矩阵推导：量化区间概率和 Fisher 信息，独立高斯观测的前提 | 用于核查信息与精度论证的前提；没有把独立高斯 CRB 用作循环干扰的绝对误差证书 |
| [Tibshirani, Adaptive Piecewise Polynomial Estimation via Trend Filtering](https://stat.cmu.edu/~ryantibs/papers/trendfilter.pdf) | 论文摘要及方法定义：以稀疏差分表示分段多项式 | 评估为边沿检测 baseline。自由结点和平滑正则可能改变幅值与边沿；最终保留题目已知波形形状的低维模型，没有加入任意去噪后再分类路径 |
| [Starck 等, Morphological Component Analysis](https://www.math.ucdavis.edu/~saito/data/focussing/starck-etal_morph-comp-anal.pdf) | 方法部分：不同成分用不同表示分离 | 启发“平滑干扰 + 波形几何”的共同建模。最终没有通用冗余字典，也没有照搬图像 MCA 实现 |
| [SciPy least_squares](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html) / [linprog](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linprog.html) | 官方 API 文档：有界优化、线性规划与 HiGHS 求解选项 | 使用安装环境中的 SciPy 1.17.0；有限迭代和有限起点不保证全局最优，LP 只在局部线性化问题中求解 |

## 本轮针对赛题的设计贡献

不是宣称学术首次提出的通用算法。基础工具和信号分离原则均有前人工作，本轮贡献是为本采集链设计、实现并检验以下组合：

1. 四类源波形、显式谐波干扰和干扰阶数在共同评分中竞争，不再让低阶权重凭经验吞掉幅值信息。
2. 源幅值、直流与干扰系数分离；干扰基随频率精化而更新，导数包含子空间变化项。
3. 幅值与噪声包络约束阻止“几乎全部观测都是噪声”的伪解；干扰准入以四类共同的基准残差判定。
4. 对被强噪声盖住基波的窄脉冲，用规律的大幅样本变化补充周期初值。这只提供频率，不提供类型或答案。
5. 使用 ADC 码区间做局部可行中心估计，最后重新计算非线性波形逐点核对码区间；同时保留不可辨识标志。

未采用的路线不等于已通过实现证明它们无效。真正运行的对照、失败和修正依据见算法报告及 CSV；本文只说明资料支持的方法选择。

## 6.0 新增调研与取舍

- Davenport / Wakin, *Compressive Sensing of Analog Signals Using Discrete Prolate Spheroidal Sequences*，2012 修订：https://arxiv.org/html/1109.3649v2 。阅读了有限观测、离格频率问题、带限子空间表示及数值稳定处理部分。作者相关会议摘要：https://mdav.ece.gatech.edu/publications/dw-spars-2011.pdf 。采用“观测和模板共同排除干扰子空间”的原则；**本包没有实现 DPSS 字典或复现压缩采样实验**，而是采用固定窗口和固定频带掩码的 FFT 投影。DPSS 的有限窗边界处理更充分，但大带宽下约 2NW 个模式的构建和分解会消耗当前时间预算，因此未直接引入。
- Shi / Yin, *Ramanujan subspace pursuit for signal periodic decomposition*：https://arxiv.org/abs/1512.08112 。本轮查看了作者摘要和周期分解定义；不宣称全文实现或作者代码复现。整数离散周期字典有利于隐藏周期搜索，但真实时钟与信号频率产生非整数样本周期，因此没有用整数周期表作为最终推断限制。
- Fu 等，*Innovation rate sampling of non-ideal piecewise polynomials*，2018：https://ietresearch.onlinelibrary.wiley.com/doi/10.1049/el.2018.5485 。阅读了发布方的模型、摘要和非理想过渡区讨论；未把特定采样核的精确重建结论用于现有 ADC。它强化了对真实边沿过渡和采样时序的独立建模需求；保留 5.0 的边沿不确定性标记。
- SciPy 官方 Blackman–Harris 窗文档：https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.windows.blackmanharris.html 。新实现使用 `sym=False` 的周期四项窗，对数据、模板和模板导数施加完全相同的变换；没有把窗旁瓣较低等同于严格带限消除。

本轮新增设计是：从非载波谐波附近的残差功率估计保守截止频率；用带外源形状约束幅值；通过两段独立时间窗检查幅值一致性；在锯齿斜率落到重置约束边界时选择可行区间中点。它是面向本项目的组合与推导，不宣称学术首创或全局可辨识证明。
