#!/usr/bin/env bash
# 一键启动 Friday Agent 全套本地环境：
#   1. Docker 容器：postgres（compose）+ med-es / med-kibana（独立容器）
#   2. 后端：FastAPI uvicorn (http://localhost:8000)
#   3. 前端：Next.js dev (http://localhost:3000)
#
# 用法：
#   ./start.sh              # 全部启动
#   ./start.sh --no-es      # 跳过 Elasticsearch（省内存，仅医学 RAG 不可用）
#   ./start.sh --no-kibana  # 跳过 Kibana
#
# 日志与 PID 在 .run/ 目录，停止用 ./stop.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

BACKEND_PORT=8000
FRONTEND_PORT=3000
ES_IMAGE="docker.elastic.co/elasticsearch/elasticsearch:9.5.4"
IK_PLUGIN_URL="https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-9.5.4.zip"

START_ES=true
START_KIBANA=true
for arg in "$@"; do
  case "$arg" in
    --no-es) START_ES=false ;;
    --no-kibana) START_KIBANA=false ;;
    *) echo "未知参数: $arg（支持 --no-es / --no-kibana）"; exit 1 ;;
  esac
done

# ---------- 输出工具 ----------
info()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; exit 1; }

section() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

port_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# 等待端口就绪：$1=端口 $2=超时秒 $3=名称
wait_port() {
  local port=$1 timeout=$2 name=$3 waited=0
  printf '等待 %s (:%s) 就绪' "$name" "$port"
  while ! port_listening "$port"; do
    printf '.'
    sleep 2
    waited=$((waited + 2))
    if [ "$waited" -ge "$timeout" ]; then
      printf ' 超时\n'
      return 1
    fi
  done
  printf ' 完成\n'
}

# ========== 0. 前置检查 ==========
section "环境检查"
command -v docker >/dev/null 2>&1 || fail "未安装 docker，请先安装 Docker Desktop"
docker info >/dev/null 2>&1 || fail "Docker 未运行，请先启动 Docker Desktop"
command -v python3 >/dev/null 2>&1 || fail "未安装 python3"
command -v npm >/dev/null 2>&1 || fail "未安装 npm（Node.js）"
command -v lsof >/dev/null 2>&1 || fail "未安装 lsof"
ok "docker / python3 / npm 均可用"

# ========== 1. PostgreSQL（docker compose） ==========
section "启动 PostgreSQL 容器"
docker compose -f "$ROOT/docker-compose.yml" up -d postgres
printf '等待 postgres 健康检查'
i=0
while [ "$(docker inspect -f '{{.State.Health.Status}}' friday-agent-postgres 2>/dev/null || true)" != "healthy" ]; do
  printf '.'
  sleep 2
  i=$((i + 2))
  [ "$i" -ge 60 ] && { printf ' 超时\n'; fail "postgres 未在 60s 内变健康，查看日志：docker logs friday-agent-postgres"; }
done
printf ' 完成\n'
ok "postgres 运行中 (localhost:5432)"

# ========== 2. Elasticsearch（医学 RAG） ==========
if $START_ES; then
  section "启动 Elasticsearch 容器 (med-es)"
  if docker container inspect med-es >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' med-es)" != "true" ]; then
      docker start med-es >/dev/null
    fi
    ok "med-es 已启动（复用已有容器）"
  else
    warn "容器 med-es 不存在，首次创建（含 IK 插件安装，需要几分钟）"
    docker run -d --name med-es -p 9200:9200 \
      -e discovery.type=single-node -e ES_JAVA_OPTS=-Xms1g -Xmx1g \
      -v med-es-data:/usr/share/elasticsearch/data \
      -v med-es-config:/usr/share/elasticsearch/config \
      "$ES_IMAGE"
    # IK 中文分词插件随容器镜像走，重建后需重装（BM25 稀疏检索依赖）
    sleep 10
    if docker exec med-es bin/elasticsearch-plugin install --batch "$IK_PLUGIN_URL"; then
      docker restart med-es
      ok "IK 插件已安装并重启容器"
    else
      warn "IK 插件安装失败，可稍后手动执行：docker exec med-es bin/elasticsearch-plugin install --batch $IK_PLUGIN_URL && docker restart med-es"
    fi
  fi
  wait_port 9200 180 "Elasticsearch" || fail "ES 未在 180s 内就绪，查看日志：docker logs med-es"
  ok "med-es 运行中 (https://localhost:9200)"
else
  section "跳过 Elasticsearch（--no-es）"
  warn "医学 RAG 文献检索将不可用，其余功能正常"
fi

# ========== 3. Kibana（可选，存在才启动） ==========
if $START_KIBANA; then
  section "Kibana 容器 (med-kibana)"
  if docker container inspect med-kibana >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' med-kibana)" != "true" ]; then
      docker start med-kibana >/dev/null
    fi
    ok "med-kibana 已启动 (http://localhost:5601)"
  else
    warn "容器 med-kibana 不存在，跳过（非必需组件）"
  fi
fi

# ========== 4. 后端 ==========
section "启动后端 (uvicorn :$BACKEND_PORT)"
cd "$ROOT/backend"

if [ ! -d .venv ]; then
  info "创建虚拟环境（首次较慢）"
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
fi
[ -f .env ] || { cp .env.example .env; warn "已从 .env.example 生成 backend/.env，请按需修改"; }

if port_listening "$BACKEND_PORT"; then
  warn "端口 $BACKEND_PORT 已被占用，假定后端已在运行，跳过启动"
else
  # 依赖同步：requirements.txt 内容随分支变化（main=agentscope+openai 1.x，
  # feature/langgraph=langgraph+openai 3.x），以文件哈希为戳记幂等安装，
  # 切分支后重跑 ./start.sh 即可自动对齐 venv（pip 自动升降级冲突包）。
  # 戳记放在 .venv 内，venv 重建后自动失效。
  REQ_STAMP=".venv/.req.stamp"
  REQ_HASH="$(shasum requirements.txt | cut -d' ' -f1)"
  if [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" != "$REQ_HASH" ]; then
    info "同步依赖 requirements.txt（首次或切分支后较慢）"
    if ! .venv/bin/python -m pip install -q -r requirements.txt; then
      fail "依赖安装失败，请检查网络/镜像源后重试，或手动执行：cd backend && .venv/bin/python -m pip install -r requirements.txt"
    fi
    echo "$REQ_HASH" > "$REQ_STAMP"
  else
    info "依赖已同步（requirements.txt 未变化，跳过安装）"
  fi

  # 导入预检：依赖或代码问题在此即刻暴露，不必等到健康检查超时
  if ! .venv/bin/python -c "import app.main" 2> "$RUN_DIR/import_check.err"; then
    tail -n 25 "$RUN_DIR/import_check.err"
    fail "后端导入失败（依赖未装齐或代码报错），完整输出见 $RUN_DIR/import_check.err"
  fi

  nohup .venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$BACKEND_PORT" \
    > "$RUN_DIR/backend.log" 2>&1 &
  echo $! > "$RUN_DIR/backend.pid"
  info "后端 PID $(cat "$RUN_DIR/backend.pid")，日志 $RUN_DIR/backend.log"
  waited=0
  printf '等待健康检查 http://localhost:%s/health' "$BACKEND_PORT"
  while true; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$BACKEND_PORT/health" 2>/dev/null || true)
    if [ "$code" = "200" ]; then
      printf ' 通过\n'
      break
    fi
    if ! kill -0 "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null; then
      printf ' 进程退出\n'
      tail -n 30 "$RUN_DIR/backend.log" || true
      fail "后端启动失败，完整日志见 $RUN_DIR/backend.log"
    fi
    # --reload 下 reloader 父进程在应用崩溃后仍存活（kill -0 检测不到），
    # 以 multiprocessing worker 子进程（spawn_main）消失识别崩溃
    if [ "$waited" -ge 10 ] && ! pgrep -P "$(cat "$RUN_DIR/backend.pid")" -f spawn_main >/dev/null 2>&1; then
      printf ' 应用进程退出\n'
      tail -n 30 "$RUN_DIR/backend.log" || true
      fail "后端启动失败，完整日志见 $RUN_DIR/backend.log"
    fi
    printf '.'
    sleep 2
    waited=$((waited + 2))
    if [ "$waited" -ge 90 ]; then
      printf ' 超时\n'
      tail -n 30 "$RUN_DIR/backend.log" || true
      fail "后端 90s 内未就绪，完整日志见 $RUN_DIR/backend.log"
    fi
  done
fi
ok "后端就绪 http://localhost:$BACKEND_PORT/health （文档 /docs）"

# ========== 5. 前端 ==========
section "启动前端 (next dev :$FRONTEND_PORT)"
cd "$ROOT/frontend"

[ -d node_modules ] || { info "安装 npm 依赖（首次较慢）"; npm install; }
[ -f .env ] || { cp .env.example .env; warn "已从 .env.example 生成 frontend/.env"; }

if port_listening "$FRONTEND_PORT"; then
  warn "端口 $FRONTEND_PORT 已被占用，假定前端已在运行，跳过启动"
else
  nohup npm run dev > "$RUN_DIR/frontend.log" 2>&1 &
  echo $! > "$RUN_DIR/frontend.pid"
  info "前端 PID $(cat "$RUN_DIR/frontend.pid")，日志 $RUN_DIR/frontend.log"
  waited=0
  printf '等待前端编译'
  while true; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:$FRONTEND_PORT/" 2>/dev/null || true)
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      printf ' 完成 (HTTP %s)\n' "$code"
      break
    fi
    if ! kill -0 "$(cat "$RUN_DIR/frontend.pid")" 2>/dev/null; then
      printf ' 进程退出\n'
      tail -n 30 "$RUN_DIR/frontend.log" || true
      fail "前端启动失败，完整日志见 $RUN_DIR/frontend.log"
    fi
    printf '.'
    sleep 2
    waited=$((waited + 2))
    if [ "$waited" -ge 180 ]; then
      printf ' 超时\n'
      tail -n 30 "$RUN_DIR/frontend.log" || true
      fail "前端 180s 内未就绪（首次编译较慢），可稍后自行查看 $RUN_DIR/frontend.log"
    fi
  done
fi

# ========== 汇总 ==========
printf '\n'
ok "全部启动完成！"
cat <<EOF

  前端页面   http://localhost:$FRONTEND_PORT
  后端 API   http://localhost:$BACKEND_PORT/health
  API 文档   http://localhost:$BACKEND_PORT/docs
  Kibana     http://localhost:5601 （若已启动）

  日志目录   $RUN_DIR/（backend.log / frontend.log）
  一键停止   ./stop.sh
EOF
