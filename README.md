# Account Type 清洗工具

本地网页工具，用于上传 Account 基本信息 Excel。工具会联网检索 `Public Entity`、
`Private Equity`、`Private Equity Investee` 或 `Account Type` 为空白的账号公开信息，并调用大模型重新判断 Account Type；
其他已有 `Account Type` 会按内置规则本地标准化，不额外联网。

它的核心工作流是：

1. 接收一张包含混乱或缺失客户数据的 Excel 表（核心是  Account Name ）。
2. 根据预设规则，过滤掉已经有明确高质量标签的客户。
3. 对那些缺失标签，或标签模糊（如 JV合资、PE私募等）的客户，自动调用搜索引擎（Tavily/Bing/Baidu 等）抓取全网最新信息。
4. 将搜到的信息喂给 LLM（如 DeepSeek）进行推理和信息抽取。
5. 将复杂的真实世界企业身份，强制归一化（Normalize）为 B2B 销售与市场运营最关心的标准分类：国企 (SOE)、外企/跨国公司
(MNC)、民企 (POE) 或 其他 (Other)。
6. 提供可视化的复核界面并导出干净的数据。

## 使用方式

### 1. 安装依赖（仅首次启动需要）

本工具依赖 `openpyxl` 库来读取和写入 Excel。请根据你的操作系统在终端运行以下命令：

**macOS / Linux:**
```bash
pip3 install -r requirements.txt
```

**Windows:**
```powershell
pip install -r requirements.txt
```

### 2. 启动工具

**macOS / Linux:**
可以使用提供的 `run.sh` 脚本启动：
```bash
chmod +x run.sh
./run.sh
```
如果默认的 `8000` 端口已被占用（例如被 Docker 占用，提示 `Address already in use`），可以通过 `--port` 参数指定其他端口启动（例如 `8080`）：
```bash
./run.sh --port 8080
```
或者直接使用 Python 运行：
```bash
python3 -m app.server --port 8080
```

**Windows:**
```powershell
.\run.ps1
```
如果端口被占用，也可以直接使用 Python 运行并指定端口：
```powershell
python -m app.server --port 8080
```

### 3. 使用 Web 界面

打开浏览器访问 `http://127.0.0.1:8000`，在页面输入 DeepSeek API Key 并上传 `.xlsx` 或 `.xlsm` 文件。
API Key 只随本次请求发送给本地服务，不保存到项目文件。
页面也支持可选输入 Tavily Search API Key，用于更稳定的联网检索；Tavily 失败时会继续尝试国内公开网页搜索。
上传后页面会显示 `已处理 x/y`、当前账号和成功/失败/待复核计数，完成后显示下载按钮。

### 4. 环境变量配置（可选）

如果不想每次都在网页中手动输入 API Key，或者需要自定义高级配置，可以在终端启动前设置以下环境变量：

#### macOS / Linux (Bash/Zsh)
```bash
# 设置 DeepSeek API Key
export DEEPSEEK_API_KEY="你的 API Key"

# 可选：设置模型和接口地址（默认使用 deepseek-v4-flash 和 https://api.deepseek.com）
export LLM_MODEL="deepseek-v4-flash"
export LLM_BASE_URL="https://api.deepseek.com"

# 可选：设置第三方搜索 API Key（按优先级：Tavily -> SerpAPI -> Bing，均未配置则使用公开搜索引擎）
export TAVILY_API_KEY="你的 Tavily Key"
# 或 export SERPAPI_API_KEY="你的 SerpAPI Key"
# 或 export BING_SEARCH_API_KEY="你的 Bing Search Key"

# 可选：设置并发分类最大线程数（默认 4，范围 1..8）
export CLASSIFICATION_MAX_WORKERS="4"
```

#### Windows (PowerShell)
```powershell
# 设置 DeepSeek API Key
$env:DEEPSEEK_API_KEY="你的 API Key"

# 可选：设置模型和接口地址
$env:LLM_MODEL="deepseek-v4-flash"
$env:LLM_BASE_URL="https://api.deepseek.com"

# 可选：设置第三方搜索 API Key
$env:TAVILY_API_KEY="你的 Tavily Key"
# 或 $env:SERPAPI_API_KEY="你的 SerpAPI Key"
# 或 $env:BING_SEARCH_API_KEY="你的 Bing Search Key"

# 可选：设置并发分类最大线程数
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
