"""P6 路由/覆盖离线评测包。

回答的问题只有一个：**在同一份冻结的合成联邦上，fast 与 exhaustive_scope
各自找到了哪些目标证据、付出了多少探测/预算成本、覆盖账本有没有说了不实话**。

这里不调用 corpus-api 应用（不起 DB / HTTP），但**不重写覆盖数学**：
目标枚举、候选排序、根预算、覆盖记录与合取判定全部走
`ddp_core.application.{routing,coverage,probe}` 与协调者
`ddp_corpus.federation_tasks` 的同一批函数；被替换掉的只有"检索执行"本身
（夹具预言机代替真实索引）。设计取舍见 `docs/refactor/P6-ROUTING-EVAL-v3.md`。
"""
