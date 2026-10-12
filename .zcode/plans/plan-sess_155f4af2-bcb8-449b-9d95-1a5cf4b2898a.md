# 抽取共享日志模块 shared/，统一 logs/ 目录

## 新建 `shared/`（根目录，跨 venv 共享）

```
shared/
├── pyproject.toml              # name="friday-shared"，纯 stdlib 零依赖，支持 py3.12/3.14
└── friday_shared/
    ├── __init__.py             # 导出 setup_service_logging
    └── logging_setup.py
```

`setup_service_logging(service: str, logger_name: str, console_level=logging.INFO, file_level=logging.DEBUG)`：
- 日志目录自动定位仓库根 `logs/`（按 `__file__` 上溯，与 CWD 无关——mcp-server stdio 模式被任意 cwd 的客户端拉起也能落对位置），`mkdir(exist_ok=True)`
- 配置指定业务 logger（backend 传 `"friday"`、mcp-server 传 `"mcp-server"`，与现状对称：业务日志进滚动文件，uvicorn/fastmcp 框架日志只走控制台镜像）
- console：`StreamHandler(sys.stderr)`（stdio 模式 stdout 保持 JSON-RPC 纯净）+ RotatingFileHandler `logs/{service}.log`（5MB×3 备份，utf-8）
- 格式统一沿用 backend 现格式 `%(asctime)s %(levelname)-5s [%(name)s] %(message)s`；幂等（已有 handlers 直接返回，防 --reload 热载/重复调用重复添加，同 backend 现逻辑）

## backend 改造

- 删除 `backend/app/logging_config.py`；`main.py` lifespan 改调 `setup_service_logging(service="backend", logger_name="friday")`
- `requirements.txt` 加一行 `-e ../shared`（pip 相对 requirements 文件解析，两个 venv 各装一份 editable）
- `agent_logging.py:81` 注释中的 `logs/backend.log` 措辞更新为根 `logs/backend.log`
- 历史迁移：`mv backend/logs/backend.log logs/backend.log`（保留历史，RotatingFileHandler 续写 append）；`eval_*.json` 留在 `backend/logs/` 不动

## mcp-server 改造（顺带补上它缺失的文件日志）

- `app/main.py` 的 `_setup_logging()` 改调共享模块：`setup_service_logging(service="mcp-server", logger_name="mcp-server", console_level=DEBUG if stdio else INFO)`——http/stdio 两种模式从此都有持久滚动日志（现在 stdio 模式日志全被客户端吞掉）
- `requirements.txt` 加 `-e ../shared`

## start.sh / .gitignore / README

**start.sh**（stop.sh 不涉及日志路径，不改）：
- 开头 `mkdir -p "$ROOT/logs"`（`RUN_DIR` 继续负责 pid）
- 三处 nohup 重定向及对应 tail/fail 提示路径：`.run/backend.log`→`logs/backend.console.log`、`.run/mcp-server.log`→`logs/mcp-server.console.log`、`.run/frontend.log`→`logs/frontend.console.log`（console 镜像 = uvicorn access/banner/崩溃 traceback/next dev 输出，重启截断语义不变）
- 汇总文案改为：日志 `logs/`（滚动业务日志 `*.log` + 控制台镜像 `*.console.log`）、PID `.run/`
- 清理孤儿：旧 `.run/*.log` 三份删除

**.gitignore**：加 `logs/`（`backend/logs/` 行保留，eval json 仍在该目录）

**README**：根 README 一键启动段落日志说明更新；mcp-server README「运行」段日志去向说明更新。

## 最终目录布局

```
logs/
├── backend.log             # 滚动 DEBUG 业务日志（friday.*，含历史迁移内容）
├── mcp-server.log          # 滚动 DEBUG 业务日志（mcp-server.*，新增能力）
├── backend.console.log     # start.sh 重定向，重启截断
├── mcp-server.console.log
└── frontend.console.log
.run/
├── backend.pid / frontend.pid / mcp-server.pid
└── import_check.err / mcp_import_check.err
```

## 验证

1. 两个 venv 手动 `pip install -e ../shared`（backend 端口占用时 start.sh 会跳过依赖同步，先手动装避免时序问题），backend `import app.main` 预检 + mcp-server 冒烟测试
2. `./stop.sh` → `./start.sh` 全量冷启动：`logs/` 五个文件齐全；`logs/backend.log` 出现「服务启动完成」、`logs/mcp-server.log` 出现「启动 http 模式」；重跑 start.sh 幂等
3. 前端发起一次联网搜索：`logs/mcp-server.log` 出现「节点[联网搜索开始/完成]」（不再只在 console 镜像里）、`logs/backend.log` 出现「节点[MCP工具注册/工具调用]」
4. `./stop.sh` 后 `.run/` 只剩 pid 清理后的状态，`logs/` 文件保留