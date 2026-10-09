#!/usr/bin/env bash
# 一键停止 Friday Agent 本地环境：前端/后端进程 + Docker 容器（postgres、med-es、med-kibana）。
# 只停止容器不删除数据卷；如需彻底清理请手动执行 docker compose down。
set -euo pipefail

ROOT="$(cd "$(dirname "$BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.run"

info()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }

# 递归杀掉进程及其全部子进程（uvicorn --reload / npm run dev 都有子进程）
kill_tree() {
  local pid=$1 child
  kill -0 "$pid" 2>/dev/null || return 0
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
}

stop_service() {
  local name=$1 pidfile="$RUN_DIR/$2"
  if [ -f "$pidfile" ]; then
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      info "停止$name (PID $pid)"
      kill_tree "$pid"
      sleep 2
      kill -0 "$pid" 2>/dev/null && { warn "$name 未退出，强制 kill -9"; kill -9 "$pid" 2>/dev/null || true; }
    else
      info "$name 未在运行（清理过期 PID 文件）"
    fi
    rm -f "$pidfile"
  else
    info "$name 无 PID 记录（非 ./start.sh 启动或已停止）"
  fi
}

stop_service "前端" "frontend.pid"
stop_service "后端" "backend.pid"

# Docker 容器：存在才停止
for c in med-kibana med-es; do
  if docker container inspect "$c" >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' "$c")" = "true" ]; then
      info "停止容器 $c"
      docker stop "$c" >/dev/null
    else
      info "容器 $c 已是停止状态"
    fi
  else
    info "容器 $c 不存在，跳过"
  fi
done

if docker compose -f "$ROOT/docker-compose.yml" ps -q postgres 2>/dev/null | grep -q .; then
  info "停止容器 friday-agent-postgres"
  docker compose -f "$ROOT/docker-compose.yml" stop postgres >/dev/null
else
  info "容器 friday-agent-postgres 不存在或已停止"
fi

printf '\n'
ok "已全部停止（数据卷保留，重启执行 ./start.sh）"
