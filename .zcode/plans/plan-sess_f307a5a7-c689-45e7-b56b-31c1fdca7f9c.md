# start.sh 双栈（AgentScope / LangGraph）兼容方案

## 已核实的关键事实
- 两分支 requirements 共享包（fastapi/pydantic/sqlalchemy/uvicorn 等）版本**完全一致**；唯一冲突是 `openai`：main pin 1.59.9，feature/langgraph pin 3.26.1；各自独占包（agentscope+mcp vs langgraph+langchain*）互不干扰
- 两个分支**从不同时运行**，所以单个 `.venv` + "每次启动按当前分支 requirements 幂等安装" 即可服务双栈：切分支时 pip 自动把 openai 升/降到对应版本
- stop.sh 已正确处理进程树，无需改动；前端两分支无差异，无需改动
- feature/langgraph 没改过 start.sh/stop.sh，改动无合并冲突风险
- `app/main.py` 导入无副作用（DB 初始化在 lifespan），可安全做导入预检

## 改动（全部在 main 分支）

### 1. start.sh 后端段重构（129–175 行附近）
- **依赖同步替代弱检查**：删掉「`.venv/bin/uvicorn` 存在即视为依赖完整」的逻辑，改为哈希戳记幂等安装：
  - 戳记文件 `backend/.venv/.req.stamp` 存 requirements.txt 的 shasum（放 venv 内，venv 重建自动失效）
  - 戳记不匹配（首次/切分支/requirements 变更）→ `.venv/bin/python -m pip install -q -r requirements.txt` 并更新戳记
  - 戳记匹配（同分支重启）→ 跳过安装，秒级通过
- **启动前导入预检**：`.venv/bin/python -c "import app.main"`，失败则输出 traceback 并立即 fail（不再等到 90s 超时）
- **健康循环崩溃快速失败**：`--reload` 模式下 reloader 父进程在应用崩溃后仍存活（现有 `kill -0` 检测不到），新增检测——等待 ≥10s 后若父进程活着但 `pgrep -P` 找不到 worker 子进程 → tail 日志立即 fail
- 环境清理顺序微调：venv 创建 → .env 检查 → 端口占用检查 → 依赖同步 + 导入预检 → 启动（端口已被占用时跳过同步，不打扰运行中的后端）

### 2. README.md 一键启动小节
加一句说明：切换 main / feature/langgraph 分支后直接重跑 `./start.sh`，脚本会自动把 venv 依赖同步到当前分支（两分支 openai 版本不同，pip 会自动升降级）。

## 验证
1. `./stop.sh` 清掉当前残留（含 PID 41182 僵尸 reloader）
2. main 分支 `./start.sh` → /health 返回 200；确认 venv 中 agentscope 2.0.8 + openai 1.59.9 就位
3. `git checkout feature/langgraph && ./start.sh` → /health 200；openai 自动升到 3.26.1（pip 缓存命中，耗时可控）
4. 切回 main `./start.sh` → openai 降回 1.59.9，/health 200；再次重启确认戳记跳过生效
5. 结束后停在用户需要的状态（默认留在 main、服务常驻，与 start.sh 交付习惯一致）