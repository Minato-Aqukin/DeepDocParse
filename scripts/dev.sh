#!/usr/bin/env bash
# 本机开发的单一入口。
#
#   scripts/dev.sh up        # 起全栈（无 GPU 档位）
#   scripts/dev.sh up --gpu  # 叠加模型运行时
#   scripts/dev.sh down
#   scripts/dev.sh status
#   scripts/dev.sh logs corpus-api
#   scripts/dev.sh migrate   # 只跑两套迁移
#   scripts/dev.sh secrets   # 生成一份 dev.env
#
# 合仓前每个仓库各有一份 init.sh / start.sh，`local` 模式还硬依赖
# "两个仓库是同级目录"这个假设。现在只有这一份。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="infra/env/dev.env"
COMPOSE=(docker compose -f infra/compose/compose.dev.yml)

need_env() {
  if [ ! -f "$ENV_FILE" ]; then
    echo "::error::$ENV_FILE 不存在。先跑 scripts/dev.sh secrets 生成一份。" >&2
    exit 1
  fi
  # **三个必填项没有默认值是有意的**：占位密钥跑起来的话鉴权形同虚设，
  # 而运行时不会有任何报错
  for key in JWT_SECRET SERVICE_TOKEN OBJECT_SECRET_KEY; do
    if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
      echo "::error::$ENV_FILE 里 $key 是空的。跑 scripts/dev.sh secrets 填一份。" >&2
      exit 1
    fi
  done
}

cmd="${1:-up}"
shift || true

case "$cmd" in
  secrets)
    if [ -f "$ENV_FILE" ]; then
      echo "$ENV_FILE 已存在，不覆盖。要重来请先删掉它。" >&2
      exit 1
    fi
    mkdir -p "$(dirname "$ENV_FILE")"
    gen() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
    sed -e "s|^JWT_SECRET=$|JWT_SECRET=$(gen)|" \
        -e "s|^SERVICE_TOKEN=$|SERVICE_TOKEN=$(gen)|" \
        -e "s|^OBJECT_SECRET_KEY=$|OBJECT_SECRET_KEY=$(gen)|" \
        infra/env/dev.env.example > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "已生成 $ENV_FILE（权限 600）。它**不进 git**。"
    ;;

  up)
    need_env
    files=("${COMPOSE[@]}")
    for arg in "$@"; do
      [ "$arg" = "--gpu" ] && files+=(-f infra/compose/compose.gpu.yml)
    done
    "${files[@]}" --env-file "$ENV_FILE" up -d --build
    echo
    echo "入口   http://127.0.0.1:8080   （/healthz /readyz /metrics）"
    echo "MinIO  http://127.0.0.1:19001"
    echo "前端   cd apps/web && npm run dev   -> http://localhost:5173"
    ;;

  down)
    need_env
    "${COMPOSE[@]}" --env-file "$ENV_FILE" down "$@"
    ;;

  status)
    need_env
    "${COMPOSE[@]}" --env-file "$ENV_FILE" ps
    ;;

  logs)
    need_env
    "${COMPOSE[@]}" --env-file "$ENV_FILE" logs -f --tail=200 "$@"
    ;;

  migrate)
    need_env
    # 两套迁移各管各的 schema，没有跨 schema 外键，所以顺序无关
    "${COMPOSE[@]}" --env-file "$ENV_FILE" up -d postgres
    "${COMPOSE[@]}" --env-file "$ENV_FILE" run --rm corpus-migrate
    "${COMPOSE[@]}" --env-file "$ENV_FILE" run --rm --entrypoint control-migrate \
        control-api -database "postgres://ddp:ddp@postgres:5432/deepdocparse" up
    ;;

  *)
    echo "用法：scripts/dev.sh {secrets|up [--gpu]|down|status|logs <service>|migrate}" >&2
    exit 1
    ;;
esac
