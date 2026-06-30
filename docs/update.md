# 更新文档 - 2026-06-30

本项目新增了对 macOS 和 Linux 系统的启动支持，并解决了默认端口被占用时的启动冲突问题。

## 新增功能与优化

1. **新增 macOS/Linux 启动脚本 ([run.sh](../run.sh))**：
   - 提供了一键启动脚本，支持自动检测 `python3` 或 `python` 命令。
   - 支持参数透传（如端口、主机地址等）。

2. **更新项目说明文档 ([README.md](../README.md))**：
   - 补充了 macOS/Linux 的依赖安装说明 (`pip3 install openpyxl`)。
   - 补充了 macOS/Linux 环境下使用 `export` 命令配置环境变量（如 API Key、并发最大线程数等）的说明。
   - 补充了端口占用时的处理方法。

3. **全新的拖拽上传与视觉重构 (Drag & Drop + UI Redesign)**：
   - **拖拽支持**：重构了文件上传组件，新增了支持拖拽（Drag and Drop）和点击浏览的交互区域。添加了对文件类型（仅 `.xlsx` / `.xlsm`）和文件大小（小于 25 MB）的拖拽前校验。
   - **交互反馈**：文件选中后能显示文件名、文件大小、重新选择等提示，并伴有颜色转换；任务处理的状态展示增加了明确的 `info`/`error`/`success` 背景框配色。
   - **视觉美化**：使用更现代感的无衬线字体族、柔和阴影、呼吸过渡效果与渐变标题，显著提升了工具的专业感与质感。

4. **代码中文注释规范化**：
   - 按照项目开发规范，对全量核心 Python 模块进行了详细的中文注释补充，覆盖了任务管理、Excel 处理、HTTP 路由、分类器和大模型客户端、网页检索等所有主要业务。

5. **本地 SQLite 数据库持久化与历史任务看板（SQLite Integration & History Dashboard）**：
   - **历史看板**：新建了本地数据库模块 [app/db.py](file:///Users/fridafeng/Documents/sunxin/work/Account-/app/db.py)。在网页端新增了“历史任务记录”面板，实时从 SQLite 数据库拉取并显示历史上传文件的解析进度、结果状态、成功/待复核/失败计数，支持随时重新下载历史文件或调出复核界面。所有任务和结果均能跨服务器重启持久留存。

6. **本地分类缓存机制（Local Classification Cache）**：
   - **运行优化**：当对账号进行联网清洗分类成功后，分类结果会被自动记录进本地 SQLite 数据库缓存中（缓存效期 30 天）。后续任务中若上传相同账号，直接从缓存读取，免去重复发起联网检索和大模型请求，大幅提升了批量清洗的速度，并为用户极大地节省了 API 成本。

7. **在线结果预览与人工交互式复核表（Interactive Review Table）**：
   - **交互优化**：上传文件解析完成（或在历史记录中点击“查看与复核”）后，网页端会自动加载并渲染出一个数据行明细交互表，允许用户按账号搜索、过滤仅显示 `Needs Review` 账号、直接在网页下拉选择修改清洗分类、编辑判定理由或备注，并支持一键取消 `Needs Review` 标记。
   - **Excel 重构**：当用户在网页点击“保存”修改某行后，后端会自动更新数据库，并基于原 Excel 文件实时重新拼装填入最新的修正数据并保存。用户下载得到的 Excel 将无缝体现所有在线手工调整。

## 问题修复

1. **解决端口冲突问题 (Address already in use)**：
   - 在 macOS/Linux 上，默认的 `8000` 端口可能会被其他本地服务（如 Docker 等）占用。
   - **优化方案**：在 [run.sh](file:///Users/fridafeng/Documents/sunxin/work/Account-/run.sh) 中支持了命令行参数透传，允许用户通过以下命令指定空闲端口启动：
     ```bash
     ./run.sh --port 8080
     ```
   - 并在 [README.md](file:///Users/fridafeng/Documents/sunxin/work/Account-/README.md) 中添加了详细的端口指定说明（同时覆盖 macOS/Linux 和 Windows 平台）。

2. **修复分类失败被错误计入成功计数的问题**：
   - **问题现象**：此前，当某行账号因为 API 超时、网络请求错误等原因导致分类任务失败时，系统虽会在后台记录 `failure_count` 增加且将单元格标记为 `Needs Review`，但其兜底分类类型会被设为 `"Other"`。因为 `"Other"` 为非空，导致该失败行在 `_update_progress` 中会被错误地再次触发 `success_count += 1`，从而在前端状态栏出现“成功”和“失败”计数同时上涨的逻辑冲突。
   - **优化方案**：修改了 [app/jobs.py](file:///Users/fridafeng/Documents/sunxin/work/Account-/app/jobs.py) 的 `_update_progress` 进度累加逻辑，增加了对 `result.failed` 的过滤校验（即 `if result and result.account_type_text and not result.failed:` 时才计入成功）。
   - **单测覆盖**：在 [tests/test_jobs.py](file:///Users/fridafeng/Documents/sunxin/work/Account-/tests/test_jobs.py) 中新增了 `test_classification_failure_counts_as_failure_but_not_success` 单元测试，确保异常分类下的计数逻辑准确无误。
