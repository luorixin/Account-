# Account Type 清洗工具

本地网页工具，用于上传 Account 基本信息 Excel，联网检索 `Public Entity`
账号的公开信息，并调用大模型重新判断 Account Type。

## 使用方式

1. 启动工具：

   ```powershell
   .\run.ps1
   ```

2. 打开 `http://127.0.0.1:8000`，在页面输入 DeepSeek API Key 并上传 `.xlsx`
   或 `.xlsm` 文件。API Key 只随本次请求发送给本地服务，不保存到项目文件。
   页面也支持可选输入 Tavily Search API Key，用于更稳定的联网检索。上传后页面会显示
   `已处理 x/y`、当前账号和成功/失败计数，完成后显示下载按钮。

3. 可选：也可以在 PowerShell 中提前设置 DeepSeek API Key：

   ```powershell
   $env:DEEPSEEK_API_KEY="你的 API Key"
   ```

4. 可选：设置模型和 OpenAI 兼容接口地址。默认使用 DeepSeek：

   ```powershell
   $env:LLM_MODEL="deepseek-v4-flash"
   $env:LLM_BASE_URL="https://api.deepseek.com"
   ```

5. 可选：设置搜索 API。优先级为 Tavily、SerpAPI、Bing。未配置搜索 API 时，
   工具会跳过网页搜索并直接使用 DeepSeek fallback 分类，证据列为空。
   稳定批量检索证据链接时建议配置 Tavily Search API Key。

   ```powershell
   $env:TAVILY_API_KEY="你的 Tavily Key"
   # 或
   $env:SERPAPI_API_KEY="你的 SerpAPI Key"
   # 或
   $env:BING_SEARCH_API_KEY="你的 Bing Search Key"
   ```

## 输出列

工具会在第一个 Sheet 最后追加：

- `New Account Type`
- `Classification Confidence`
- `Classification Reason`
- `Evidence URLs`

非 `Public Entity` 行会复制原 `Account Type` 到 `New Account Type`。
