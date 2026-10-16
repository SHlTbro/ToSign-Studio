# 用户与系统边界

ToSign Studio 为签图制作者提供底图与文字排版、批量生成、逐张审核和 PNG / ZIP 导出。这里的“用户”是操作页面的人；ToSign Studio 是完整软件系统。当前没有抖音发布或外部云端服务接入。

![用户与系统边界](../../assets/diagrams/context.png)

| 来源 | 目标 | 行为与协议 | 实现依据 | 状态 |
| --- | --- | --- | --- | --- |
| 签图制作者 | ToSign Studio | 通过浏览器 / HTTP 制作、审核、导出签图 | 前端 P5_Batch、P5_Review；后端 render/router.py | 已实现，系统字体两图链路已验证 |

颜色图例区分人员与软件系统；箭头表示发起操作的方向。新训练模型仍须完成效果评估和正式发布，这张图没有把训练完成等同于可用。

[交互图（下载后本地打开）](../../assets/diagrams/context.html) · [可编辑 JSON](../../assets/diagrams/context.json) · [自动适配明暗主题的 SVG](../../assets/diagrams/context.svg) · [C4 Mermaid 源](../../assets/diagrams/c4-context.mmd) · [C4 兼容预览](../../assets/diagrams/c4-context.svg)

