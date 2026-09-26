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
#   scripts/dev.sh up --profile center-only|center-federated|compute-worker
#   scripts/dev.sh config --profile compute-worker --project ddp-compute --env-file infra/env/dev.env
#
# 合仓前每个仓库各有一份 init.sh / start.sh，`local` 模式还硬依赖
# "两个仓库是同级目录"这个假设。现在只有这一份。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${DDP_ENV_FILE:-infra/env/dev.env}"
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
profile="${DDP_DEPLOYMENT_PROFILE:-center-federated}"
project="${DDP_PROJECT:-}"
gpu=false
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) profile="${2:?--profile requires a value}"; shift 2 ;;
    --project) project="${2:?--project requires a value}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?--env-file requires a value}"; shift 2 ;;
    --gpu) gpu=true; shift ;;
    *) args+=("$1"); shift ;;
  esac
done
case "$profile" in
  center-only)
    # No peer execution or forwarding. Existing local source/catalog APIs remain available.
    export FEDERATION_PEERS='{}' FEDERATION_ADMISSIONS_ENABLED=false
    ;;
  center-federated) ;;
  compute-worker)
    COMPOSE+=(-f infra/compose/compose.compute.yml)
    project="${project:-ddp-compute}"
    ;;
  *) echo "::error::未知部署 profile: $profile" >&2; exit 1 ;;
esac
if [ "$gpu" = true ]; then
  COMPOSE+=(-f infra/compose/compose.gpu.yml)
fi
COMPOSE+=(-p "${project:-ddp}" --env-file "$ENV_FILE")

deployment_services() {
  local configured service
  configured="$("${COMPOSE[@]}" config --services)"
  services=()
  while IFS= read -r service; do
    if [ "$profile" = compute-worker ]; then
      case "$service" in
        postgres|minio|corpus-migrate|control-migrate|control-api|corpus-api|corpus-worker|mcp) continue ;;
      esac
    fi
    services+=("$service")
  done <<< "$configured"
  if [ "${#services[@]}" -eq 0 ]; then
    echo "::error::部署 profile 没有可启动的服务" >&2
    exit 1
  fi
}

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
    if [ "${#args[@]}" -ne 0 ]; then
      echo "::error::up 只接受已声明的 profile/project/env-file/gpu 选项；不能绕过部署角色追加服务。" >&2
      exit 1
    fi
    deployment_services
    # **构建重试三次。** 镜像构建在这台机器上被三类瞬时故障打断过：
    # 国内 pip 镜像给出坏包（哈希对不上）、goproxy 断流（unexpected EOF）、
    # Docker Hub 解析基础镜像 manifest 超时（DeadlineExceeded）。
    # 三类都是重试就好，而一次失败要白等好几分钟。
    # 前两类在 Dockerfile 里各自有重试，这一层管的是第三类与整体抖动。
    for attempt in 1 2 3; do
      "${COMPOSE[@]}" build "${services[@]}" && break
      if [ "$attempt" = 3 ]; then
        echo "构建三次都失败了 —— 这次多半不是网络问题，看上面的报错" >&2
        exit 1
      fi
      echo ">>> 构建第 $attempt 次失败，10 秒后重试" >&2
      sleep 10
    done
    "${COMPOSE[@]}" up -d "${services[@]}"
    echo
    echo "部署 profile=$profile project=${project:-ddp}"
    if [ "$profile" = compute-worker ]; then
      echo "计算节点只启动已认证模型网关/worker；不会创建永久语料库。"
    else
      echo "入口与对象存储地址取自 $ENV_FILE；健康检查 /healthz /readyz"
      echo "前端   cd apps/web && npm run dev   -> http://localhost:5173"
    fi
    ;;

  down)
    need_env
    "${COMPOSE[@]}" down "${args[@]}"
    ;;

  status)
    need_env
    deployment_services
    "${COMPOSE[@]}" ps "${services[@]}"
    ;;

  logs)
    need_env
    if [ "${#args[@]}" -eq 0 ]; then
      deployment_services
      args=("${services[@]}")
    fi
    "${COMPOSE[@]}" logs -f --tail=200 "${args[@]}"
    ;;

  migrate)
    need_env
    if [ "$profile" = compute-worker ]; then
      echo "::error::计算节点没有账户/语料数据库迁移；请对拥有数据的中心运行 migrate。" >&2
      exit 1
    fi
    # 两套迁移各管各的 schema，没有跨 schema 外键，所以顺序无关
    "${COMPOSE[@]}" up -d postgres
    "${COMPOSE[@]}" run --rm corpus-migrate
    # 两套迁移都是独立的一次性容器，连接串与口令都从 .env 取
    "${COMPOSE[@]}" run --rm control-migrate up
    ;;

  config)
    need_env
    deployment_services
    # Only service names: unlike raw compose config, this never prints expanded secrets.
    printf '%s\n' "${services[@]}"
    ;;

  *)
    echo "用法：scripts/dev.sh {secrets|up|down|status|logs <service>|migrate|config} [--profile center-only|center-federated|compute-worker] [--project NAME] [--env-file PATH] [--gpu]" >&2
    exit 1
    ;;
esac
