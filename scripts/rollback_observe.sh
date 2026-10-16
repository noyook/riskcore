#!/usr/bin/env bash
# ============================================================================
# rollback_observe.sh — W-010 一键回滚：谓词切回旁路观察（只记不拦）
#
# 用法：scripts/rollback_observe.sh [--pid-file .tmp/serve.pid]
# 行为：把 RISKCORE_PREDICATE_MODE=observe 写入 .env（若存在），
#       杀掉在跑的服务进程；由看门狗/人工以 observe 模式重启。
# 时效目标：≤15 分钟（实测为秒级，重启即恢复）。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[rollback] $(date -u +%FT%TZ) 开始回滚 → observe（只记不拦）"

# 1. 持久化切回 observe（.env 存在则更新，不存在则提示）
if [ -f .env ]; then
    if grep -q '^RISKCORE_PREDICATE_MODE=' .env; then
        sed -i '' 's/^RISKCORE_PREDICATE_MODE=.*/RISKCORE_PREDICATE_MODE=observe/' .env
    else
        echo 'RISKCORE_PREDICATE_MODE=observe' >> .env
    fi
    echo "[rollback] .env 已更新为 RISKCORE_PREDICATE_MODE=observe"
else
    echo "[rollback] 未发现 .env：重启时请显式带 RISKCORE_PREDICATE_MODE=observe"
fi

# 2. 停掉在跑的服务（若有 pid 文件）
if [ -f .tmp/serve.pid ]; then
    PID="$(cat .tmp/serve.pid)"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" && echo "[rollback] 已停止服务进程 $PID"
    fi
    rm -f .tmp/serve.pid
fi

# 3. 声明式谓词文件兜底改回 observe（防止有人直接读 JSON 部署）
for f in areas/executor/predicates/*.json; do
    sed -i '' 's/"mode": "enforce"/"mode": "observe"/' "$f"
done
echo "[rollback] 谓词声明已兜底为 observe"

cat <<'EOF'
[rollback] 完成。以旁路模式重启：
    RISKCORE_PREDICATE_MODE=observe python3 scripts/serve.py --db .tmp/riskcore.db
[rollback] 回滚后旁路观察继续留痕，差异重新进入双跑语料；
           已出具决策不受影响（决策历史独立审计）。
EOF
