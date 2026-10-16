# 运行单元与数据

网页工作台运行在浏览器中，通过 JSON / HTTP 访问 FastAPI 应用。认证、素材、模板、字形、训练、生成、审核与导出是同一后端中的域模块。生成工作线程也在这个后端内运行，没有独立的 Redis、Celery 或消息中间件。

![运行单元与数据](../../assets/diagrams/containers.png)

SQLite 保存账号、任务状态、审核和产物索引；文件系统保存底图、字形、模型及 PNG / ZIP。系统字体可以在后端直接渲染。个人模型需要合法执行身份和可用资源时，才启动受控 PyTorch 子进程；子进程产生的字形仍由后端负责最终排版、审核和导出。

| 来源 | 目标 | 行为与协议 | 实现依据 | 状态 |
| --- | --- | --- | --- | --- |
| 签图制作者 | 网页工作台 | 浏览器操作 | Vue Router 页面入口 | 已实现 |
| 网页工作台 | 业务与生成后端 | 提交任务并回读状态，JSON / HTTP | P5_Batch.vue、P5_Review.vue；render/router.py | 已实现 |
| 后端 | 业务数据库 | 读写任务与审核，SQL | core/db.py；render/models.py、service.py | 已实现 |
| 后端 | 文件存储 | 读写素材、模型和成品，文件 I/O | core/storage.py；render/service.py | 已实现 |
| 后端 | 模型执行进程 | 执行身份有效时启动，子进程 / JSON 与文件 | styles/m2_jobs.py；render/glyph_provider.py | 受资格控制；新模型尚未发布 |

人物不在软件系统边界内；其余盒子是应用、运行进程或持久化存储，不把 Python 模块画成微服务。图中没有将可选模型进程的存在当成任何模型的发布许可。

[交互图（下载后本地打开）](../../assets/diagrams/containers.html) · [可编辑 JSON](../../assets/diagrams/containers.json) · [自动适配明暗主题的 SVG](../../assets/diagrams/containers.svg) · [C4 Mermaid 源](../../assets/diagrams/c4-containers.mmd) · [C4 兼容预览](../../assets/diagrams/c4-containers.svg)

