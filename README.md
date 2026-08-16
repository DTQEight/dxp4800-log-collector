# 绿联 DXP4800 Docker 日志收集工具

一个专门为绿联 DXP4800 NAS 设计的轻量级 Docker 容器，用于集中收集 NAS 上所有 Docker 容器的 stdout/stderr 日志，提供 Web 界面浏览、检索、导出。

## ✨ 功能特性

- 🔍 **自动发现**：自动扫描 NAS 上所有运行中的 Docker 容器
- 📡 **双通道收集**：增量全量拉取 + 实时流式监听，确保日志不丢
- 🗂️ **双存储形态**：
  - 按「容器名/日期.log」分文件归档（便于直接下载浏览）
  - SQLite 数据库索引存储（支持关键词、时间范围搜索）
- 🌐 **Web 控制台**：容器列表、文件浏览、关键字检索、CSV/JSON 导出、实时 tail
- 🧹 **自动清理**：按保留天数自动清理过期日志文件与数据库记录
- 🔐 **登录鉴权**：简单的用户名/密码登录保护
- ⚡ **低资源占用**：Python Flask + APScheduler，内存占用约 80~150MB

## 📁 项目结构

```
dxp4800-log-collector/
├── app/                       # 核心代码
│   ├── __init__.py
│   ├── main.py                # 启动入口（Web+收集器+定时清理）
│   ├── config.py              # 环境变量配置
│   ├── docker_client.py       # Docker API 封装
│   ├── collector.py           # 日志收集器（增量+实时流）
│   ├── storage.py             # 按日期文件存储
│   ├── models.py              # SQLite 数据层
│   └── web.py                 # Flask Web API + 页面
├── templates/
│   ├── login.html             # 登录页
│   └── index.html             # 主界面
├── static/                    # 静态资源
├── logs/                      # 日志输出目录（挂载到NAS）
├── data/                      # SQLite 数据目录（挂载到NAS）
├── requirements.txt
├── Dockerfile                 # 镜像构建
├── docker-compose.yml         # 推荐部署方式
├── .env.example               # 环境变量示例
└── .gitignore
```

## 🚀 在绿联 DXP4800 上部署

推荐采用 **GitHub Actions CI 构建 → GHCR 镜像仓库 → NAS 拉取镜像** 的方式，不用在 NAS 本地编译，省 CPU、启动快、多架构兼容（x86_64 / arm64 通用）。

### 方式一：直接拉取 GHCR 镜像（⭐NAS 上推荐）

镜像已由 GitHub Actions 自动构建并发布到 [GHCR Packages](https://github.com/DTQEight/dxp4800-log-collector/pkgs/container/dxp4800-log-collector)，NAS 只需三步：

```bash
# ① 在 NAS 上创建工作目录（建议放共享盘绝对路径）
mkdir -p /volume1/docker/dxp4800-log-collector
cd /volume1/docker/dxp4800-log-collector

# ② 把仓库里的 docker-compose.yml 和 .env.example 下载下来（或者 clone 整个仓库）
#    方式A：只拉必要文件（需要 curl）
curl -sSL https://raw.githubusercontent.com/DTQEight/dxp4800-log-collector/main/docker-compose.yml -o docker-compose.yml
curl -sSL https://raw.githubusercontent.com/DTQEight/dxp4800-log-collector/main/.env.example -o .env.example
cp .env.example .env && sed -i 's/ChangeMe123!/你自己的强密码/g' .env

# ③ 拉取镜像并启动（NAS 首次拉 GHCR 无需登录，公开仓库可直接拉）
docker compose pull
docker compose up -d
```

升级也只需要一行：
```bash
cd /volume1/docker/dxp4800-log-collector
docker compose pull && docker compose up -d
```

> 访问 Web：浏览器打开 `http://<NAS_IP>:5000`，默认账号 `admin` / 你在 `.env` 里设置的密码。

---

### 方式二：NAS 本地 build（无需 GitHub 网络）

1. **把项目目录上传到 NAS**
   例如放到：`/volume1/docker/dxp4800-log-collector/`

2. **修改 docker-compose.yml：把 `image:` 行注释掉，取消文件末尾 `build:` 块的注释**

3. **（可选）修改环境变量**
   ```bash
   cp .env.example .env
   vi .env   # 修改默认密码等
   ```

4. **启动容器**
   ```bash
   cd /volume1/docker/dxp4800-log-collector
   docker compose up -d --build
   ```

### 方式三：绿联「Docker管理器」UI 创建

1. 打开绿联 Docker 管理器 → 「镜像」→「拉取」
   - 镜像仓库：选 **GHCR / GitHub 容器仓库**（若下拉里没有，就手动输 `ghcr.io`）
   - 镜像名：`dtqeight/dxp4800-log-collector`
   - 标签：`latest`（或指定稳定版如 `1.0.0`）
2. 镜像 → 找到刚才拉下来的镜像 → 「创建容器」
3. 关键配置：
   - 端口映射：`5000` → `5000`
   - 卷挂载：
     - `/var/run/docker.sock` → `/var/run/docker.sock`（只读，**必须**）
     - `./logs` → `/app/logs`
     - `./data` → `/app/data`
   - 环境变量：按需设置 `WEB_PASSWORD` 等

---

## 🚀 镜像自动构建工作流（开发者看）

项目内置了 [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml)，满足以下任一条件就会触发 GitHub Actions：

| 触发条件 | 生成的镜像 tag |
| --- | --- |
| 推送到 `main` 分支 | `:latest`、`:sha-<短hash>` |
| 打 git tag `v1.2.3` | `:1.2.3`、`:1.2`、`:1`、`:latest` |
| Actions 页面手动 Run workflow | 可自定义后缀（如 `:beta`） |

构建产物：
- 多架构：`linux/amd64`（DXP4800 x86 版用）+ `linux/arm64`（未来换 ARM NAS 免重编）
- 已启用 SBOM + Provenance 签名，符合 GitHub supply-chain security
- 使用 `actions/cache` + `type=gha` 缓存，第二次构建 30s 左右即可完成

镜像地址：`ghcr.io/dtqeight/dxp4800-log-collector[:tag]`

## ⚙️ 可调参数（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOCKER_SOCKET` | `unix:///var/run/docker.sock` | Docker API 地址 |
| `LOG_STORAGE_PATH` | `/app/logs` | 日志文件存放路径 |
| `LOG_RETENTION_DAYS` | `30` | 日志保留天数 |
| `COLLECT_INTERVAL_SEC` | `60` | 扫描容器并拉取增量日志的间隔 |
| `EXCLUDE_CONTAINERS` | `dxp4800-log-collector` | 排除收集的容器名（逗号分隔） |
| `WEB_PORT` | `5000` | Web 端口 |
| `WEB_USERNAME` | `admin` | Web 登录用户名 |
| `WEB_PASSWORD` | `ChangeMe123!` | Web 登录密码 |
| `DB_PATH` | `/app/data/logs.db` | SQLite 数据库路径 |

## 🖥️ Web 界面使用

1. **左侧容器列表**：显示运行/历史容器，绿色圆点=运行中
2. **按日期文件** Tab：查看每天归档的 `.log` 文件，可点击下载
3. **数据库检索** Tab：支持关键词 + 时间范围组合搜索
4. **顶部按钮**：
   - 🔍 搜索：按当前条件检索数据库
   - 📡 实时日志：直接 `docker logs --tail` 该容器当前内容
   - ⬇ CSV / JSON：按当前筛选条件导出

## 🐛 常见问题

**Q: 启动后容器列表为空？**
A: 请确认已挂载 `/var/run/docker.sock`，并且有可读权限。若 DXP4800 上 docker 用户组不是常规 999，可用 SSH 登录 NAS 执行 `ls -la /var/run/docker.sock` 检查权限。

**Q: 日志量大时会不会把磁盘撑爆？**
A: 默认保留 30 天，通过 `LOG_RETENTION_DAYS` 调整。建议把 `logs/` 挂载到 NAS 大容量共享文件夹。

**Q: 如何停止或重启？**
```bash
docker compose restart
docker compose down
```

## 🔧 本地开发（非NAS环境）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 本机需先安装并启动 Docker Desktop
WEB_PASSWORD=test123 python -m app.main
# 访问 http://127.0.0.1:5000
```
