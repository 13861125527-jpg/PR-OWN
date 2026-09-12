# L3 跨文件 30 题数据集验收

## 数据集构成

| 项目 | 数量 |
|---|---:|
| 总样本 | 30 |
| 跨文件缺陷样本 | 24 |
| 跨文件负样本 | 6 |
| Expected findings | 24 |

缺陷类别分布：correctness 13、security 6、edge_case 3、performance 1、concurrency 1。

每题包含一个发生变更的 `src/case_XX.py` 和一个未修改的 `lib/contract_XX.py`。变更行使用从关联模块导入的 `check` 符号；判断是否存在缺陷需要结合 `check` 的返回值、异常、单位、安全或并发契约。

## L3 运行时验收

使用生产路径 `prepare_l3_snapshot` 和 `ContextAssembler(symbol_retrieval=True)` 逐题验证：

| 指标 | 结果 |
|---|---:|
| 产生 L3 chunk 的样本 | 30/30 |
| L3 chunks | 30 |
| 读取变更文件和关联文件 | 30/30 |
| 单题最短 L3 内容 | 157 characters |
| 未命中样本 | 0 |

相关 L3 单元测试：22 项通过。

## 对照实验口径

为了单独测量 L3 效果，两组必须使用同一模型、同一温度、同一 MultiRole 配置、同一门控、同一 Pipeline 阈值和同一 30 题，只改变：

- L3-off：`context.symbol_retrieval=false`
- L3-on：`context.symbol_retrieval=true`

主要指标使用缺陷级严格召回、精确位置召回、±5 行召回和负样本噪声；macro recall 会把无误报负样本记为 1，不应单独作为结论。
