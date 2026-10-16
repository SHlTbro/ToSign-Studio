# 架构与工作流程

先看系统边界，再看运行单元，最后追踪一批图片怎样变成可导出的成品。这些图以本地当前源码为依据，没有假设云部署、远端仓库或不存在的微服务。

- [用户与系统边界](c4-context.md)：[PNG](../../assets/diagrams/context.png)、[SVG](../../assets/diagrams/context.svg)、[交互 HTML](../../assets/diagrams/context.html)、[JSON](../../assets/diagrams/context.json)、[C4 源](../../assets/diagrams/c4-context.mmd)。
- [运行单元与数据](c4-containers.md)：[PNG](../../assets/diagrams/containers.png)、[SVG](../../assets/diagrams/containers.svg)、[交互 HTML](../../assets/diagrams/containers.html)、[JSON](../../assets/diagrams/containers.json)、[C4 源](../../assets/diagrams/c4-containers.mmd)。
- [正常生成与失败恢复](workflow.md)：[PNG](../../assets/diagrams/workflow.png)、[SVG](../../assets/diagrams/workflow.svg)、[交互 HTML](../../assets/diagrams/workflow.html)、[JSON](../../assets/diagrams/workflow.json)。

交互图是自包含本地文件。在 GitHub 中查看 HTML 源文件不等于已上线的交互服务，请下载后打开。静态 PNG 用于 README 直接阅读；SVG 保留明暗主题适配。

[图形与嵌入字体的第三方许可证](../../assets/diagrams/licenses/NOTICE.md)

