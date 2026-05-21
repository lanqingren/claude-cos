# Claude Code COS Sync

自动检测 Claude Code 新版本，下载后同步上传到腾讯云 COS。

## 工作原理

每天定时触发 4 次，流程如下：

```
拉取远端最新版本号
       ↓
与 .last-synced-version 对比
       ↓ 有新版本
下载各平台二进制文件（SHA256 校验）
       ↓
上传到腾讯云 COS
       ↓
更新 .last-synced-version 并提交回仓库
```

版本相同时跳过，不产生任何下载或上传。

## 同步的平台

| 平台 | COS 路径 |
|---|---|
| Windows x64 | `claude-code-win-x64/claude.exe` |
| macOS Apple Silicon | `claude-code-mac-arm64/claude` |
| macOS Intel | `claude-code-mac-x64/claude` |

每次同步覆盖同一路径，COS 中始终只保留最新版本。

## 配置步骤

### 1. Fork 或克隆本仓库到你的 GitHub 账号

### 2. 配置 GitHub Secrets

进入仓库页面 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加以下 4 个：

| Secret 名称 | 说明 |
|---|---|
| `TENCENT_COS_BUCKET` | 存储桶名称，如 `my-bucket-1250000000` |
| `TENCENT_COS_REGION` | 存储桶地域，如 `ap-guangzhou` |
| `TENCENT_COS_SECRET_ID` | 腾讯云 API 密钥 SecretId |
| `TENCENT_COS_SECRET_KEY` | 腾讯云 API 密钥 SecretKey |

> 腾讯云 API 密钥在 [访问管理控制台](https://console.cloud.tencent.com/cam/capi) 中获取。建议新建一个只有 COS 写权限的子账号密钥，不要使用主账号密钥。

### 3. 按需修改同步目标

编辑 `sync_claude.toml`，调整 `[[targets]]` 中的 `cos_key` 为你希望存放的 COS 路径：

```toml
[[targets]]
name = "claude-code-mac-arm64"
platform = "darwin-arm64"
local_path = "./claude-code-mac-arm64/claude"
metadata_path = "./claude-code-mac-arm64/claude.meta.json"
cos_key = "claude-code-mac-arm64/claude"   # ← 修改这里
```

不需要的平台直接删掉对应的 `[[targets]]` 段即可。

### 4. 启用 Actions

首次 fork 后 GitHub 会禁用 Actions，进入仓库 → **Actions** → 点击启用按钮。

### 5. 触发首次同步

进入 **Actions** → **Sync Claude Code to Tencent COS** → **Run workflow**，手动触发一次，确认配置正确。

## 触发方式

**自动**：每天 UTC 0/6/12/18 点（北京时间 8/14/20/2 点）各检查一次。

**手动**：Actions 页面点击 Run workflow，支持以下参数：

| 参数 | 说明 |
|---|---|
| 强制重新下载 | 即使本地已是最新版本也重新下载 |
| 强制重新上传 | 即使未发生更新也重新上传到 COS |
| 仅检查版本 | 只打印版本号，不执行下载或上传 |

## 额度消耗

- **Actions 分钟数**：每次运行约 2–5 分钟（主要是下载时间），每天 4 次，月均约 120 次运行。公开仓库免费不限；私有仓库每月有 2000 分钟免费额度。
- **Artifact / Cache / Packages**：均不使用，不消耗相关额度。

## sync_claude.toml 配置说明

```toml
[claude]
channel = "latest"
```

| 字段 | 说明 |
|---|---|
| `channel` | 同步的发布渠道。`latest` 表示最新版，`stable` 表示稳定版，也可以填具体版本号如 `2.1.126` |
| `download_base_url` | Claude Code 官方下载地址，无需修改 |
| `gcs_bucket` / `gcs_prefix` | Anthropic 用于存放历史版本列表的 GCS 路径，无需修改 |
| `timeout_seconds` | 网络请求超时时间（秒），默认 30 |
| `upload_when_current` | 本地已是最新版时是否仍然上传。默认 `false`，设为 `true` 可在每次触发时强制上传 |

---

```toml
[[targets]]
name = "claude-code-mac-arm64"
platform = "darwin-arm64"
local_path = "./claude-code-mac-arm64/claude"
metadata_path = "./claude-code-mac-arm64/claude.meta.json"
cos_key = "claude-code-mac-arm64/claude"
```

每个 `[[targets]]` 对应一个需要同步的平台，可以有多个。

| 字段 | 说明 |
|---|---|
| `name` | 目标名称，仅用于日志显示和 `--target` 参数过滤 |
| `platform` | 平台标识，必须与官方 manifest 中的键名一致，可选值见下表 |
| `local_path` | 下载到本地的路径（相对于 toml 文件所在目录） |
| `metadata_path` | 本地元数据文件路径，记录版本号和 checksum，供下次快速判断是否需要更新 |
| `cos_key` | 上传到 COS 的对象路径，不含 bucket 名称，不以 `/` 开头 |

**platform 可选值**

| 值 | 对应平台 |
|---|---|
| `win32-x64` | Windows x64 |
| `darwin-arm64` | macOS Apple Silicon |
| `darwin-x64` | macOS Intel |

---

`[cos]` 段无需在文件中配置，bucket、region、密钥均通过 GitHub Secrets 以环境变量方式注入。

## 文件说明

| 文件 | 说明 |
|---|---|
| `sync_claude_to_cos.py` | 同步脚本主程序 |
| `sync_claude.toml` | 同步配置（平台列表，无凭据） |
| `requirements.txt` | Python 依赖（腾讯云 COS SDK） |
| `.github/workflows/sync-claude-to-cos.yml` | GitHub Actions workflow |
| `.last-synced-version` | 记录上次成功同步的版本号（自动维护） |
