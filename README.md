# Account Type 清洗工具

本地网页工具，用于上传 Account 基本信息 Excel。工具会联网检索 `Public Entity`、
`Private Equity`、`Private Equity Investee` 或 `Account Type` 为空白的账号公开信息，并调用大模型重新判断 Account Type；
其他已有 `Account Type` 会按内置规则本地标准化，不额外联网。

## 使用方式

1. 启动工具：

   ```powershell
   .\run.ps1
   ```

2. 打开 `http://127.0.0.1:8000`，在页面输入 DeepSeek API Key 并上传 `.xlsx`
   或 `.xlsm` 文件。API Key 只随本次请求发送给本地服务，不保存到项目文件。
   页面也支持可选输入 Tavily Search API Key，用于更稳定的联网检索；Tavily 失败时会继续尝试国内公开搜索。
   上传后页面会显示 `已处理 x/y`、当前账号和成功/失败计数，完成后显示下载按钮。

3. 可选：也可以在 PowerShell 中提前设置 DeepSeek API Key：

   ```powershell
   $env:DEEPSEEK_API_KEY="你的 API Key"
   ```

4. 可选：设置模型和 OpenAI 兼容接口地址。默认使用 DeepSeek：

   ```powershell
   $env:LLM_MODEL="deepseek-v4-flash"
   $env:LLM_BASE_URL="https://api.deepseek.com"
   ```

5. 可选：设置搜索 API。优先级为 Tavily、SerpAPI、Bing；API 未配置、返回空结果或请求失败时，
   工具会继续尝试百度、搜狗、360 的公开网页搜索。公开网页搜索可能受网络、反爬和页面结构变化影响，
   稳定批量检索证据链接时仍建议配置 Tavily Search API Key。

   ```powershell
   $env:TAVILY_API_KEY="你的 Tavily Key"
   # 或
   $env:SERPAPI_API_KEY="你的 SerpAPI Key"
   # 或
   $env:BING_SEARCH_API_KEY="你的 Bing Search Key"
   ```

6. 可选：设置并发分类数量。默认同时处理 4 个待分类账号；如果遇到 API 限流，
   可以调低到 1 恢复串行处理。

   ```powershell
   $env:CLASSIFICATION_MAX_WORKERS="4"
   ```

## 输出列

工具会在第一个 Sheet 最后追加：

- `New Account Type`
- `Account Type Short`
- `Review Status`
- `Classification Confidence`
- `Classification Reason`
- `Evidence URLs`

非上述联网重判目标且 `Account Type` 非空白的行会按内置规则本地标准化后写入
`New Account Type`；`Account Type Short` 会输出 `SOE`、`POE`、`MNC` 或 `Other`。
需要人工复核的行会在 `Review Status` 写入 `Needs Review`；无法确认但仍需给出推荐时，
`New Account Type` 默认写入 `Other`。
