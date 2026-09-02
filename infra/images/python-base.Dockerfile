# 四个 Python 服务的公共基底。
#
# **一个 Dockerfile，四个 target** —— 它们共用同一份依赖解析与同一套
# 构建缓存。分成四份的话，`ddp_core` 的一次改动要在四个地方各写一次 COPY，
# 而漏写的表现是**镜像起来直接 ModuleNotFoundError**（旧的网关镜像
# 就在这条上踩过）。
#
# 构建：
#   docker build -f infra/images/python-base.Dockerfile --target corpus-api .
#   国内加 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- 依赖层
FROM python:${PYTHON_VERSION}-slim AS deps

ARG PIP_INDEX_URL=https://pypi.org/simple
# 重试时换的那个源。**必须是另一家** —— 同一个坏镜像重试三次还是坏的。
ARG PIP_FALLBACK_INDEX_URL=https://pypi.org/simple
# **不再 PIP_NO_CACHE_DIR=1。** 缓存放在 BuildKit 的 cache mount 里，
# 不进镜像层（镜像不会因此变大），但**跨构建保留** ——
# 这台机器的网络给大 wheel 的坏包率实测约 1/3（宿主机 curl 同一个
# pillow wheel，三次里有一次校验不上），而 pip 一个坏包就整条 install 失败。
# 有了缓存，每次重试都把上一次拿到的好包留下来，失败面逐次收缩；
# 没有缓存的话每次都从零开始，坏包率一乘就永远过不去。
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_FALLBACK_INDEX_URL=${PIP_FALLBACK_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# 国内镜像偶发**坏包**："THESE PACKAGES DO NOT MATCH THE HASHES"。
# 实测这台机器上**宿主机 curl 同一个 pillow wheel，三次里有一次校验不上** ——
# 也就是说坏包率约 1/3，而且换到 USTC 一样坏，说明问题在这条网络路径上，
# 不在某一家镜像。
#
# 所以两条一起上：重试时**换源**（PIP_FALLBACK_INDEX_URL），
# 并且**保留 pip 缓存**（上面那段 ENV）——
# 后者才是关键：每次重试把上一次拿到的好包留下来，失败面逐次收缩。
# 注意不能 --no-deps 或降低校验：坏包必须继续报错，只是允许换个地方再拿一次。
#
# wrapper 原样转发 "$@"（连子命令一起），别在里面写死 install ——
# 写死过一次，结果是 `pip install install ./...`，报的却是
# "No matching distribution found for install"，看着像网络问题。
#
# 换源用**环境变量**而不是 `--index-url`：那是 install 的选项，不是 pip 的
# 全局选项，`pip --index-url ... install ...` 会直接 "no such option"。
# 而这个错误长得跟"源不可用"一模一样 —— 第一版就是这么白跑了一轮。
RUN printf '%s\n' '#!/bin/sh' \
    'pip "$@" && exit 0' \
    'echo "pip 失败，换源重试：$PIP_FALLBACK_INDEX_URL"' \
    'for i in 1 2 3; do' \
    '  PIP_INDEX_URL="$PIP_FALLBACK_INDEX_URL" pip "$@" && exit 0' \
    '  echo "换源后第 $i 次仍失败，重试"; sleep 5' \
    'done' \
    'exit 1' > /usr/local/bin/pip-retry && chmod +x /usr/local/bin/pip-retry

# 先只拷 pyproject：依赖没变时这一层能命中缓存
COPY python/ddp_contracts/pyproject.toml python/ddp_contracts/
COPY python/ddp_core/pyproject.toml       python/ddp_core/
COPY services/model-gateway/pyproject.toml services/model-gateway/
COPY services/corpus-api/pyproject.toml    services/corpus-api/
COPY services/corpus-worker/pyproject.toml services/corpus-worker/
COPY services/mcp/pyproject.toml           services/mcp/

# ---------------------------------------------------------------- 源码层
FROM deps AS source
COPY python/ python/
COPY services/ services/
COPY packages/contracts/ packages/contracts/

# ------------------------------------------------------------ 模型网关
FROM source AS model-gateway
# **不装 [db]**：网关是无状态适配层，一行 ORM 都不该有。
# 失守的表现不是报错，而是镜像悄悄变大 —— CI 有一个 job 专门装最小集来钉这件事
RUN --mount=type=cache,target=/root/.cache/pip pip-retry install ./python/ddp_contracts ./python/ddp_core ./services/model-gateway
EXPOSE 9000
CMD ["uvicorn", "ddp_gateway.main:app", "--host", "0.0.0.0", "--port", "9000"]

# -------------------------------------------------------------- 语料 API
FROM source AS corpus-api
RUN --mount=type=cache,target=/root/.cache/pip pip-retry install ./python/ddp_contracts "./python/ddp_core[db,cjk]" ./services/corpus-api
EXPOSE 8081
CMD ["uvicorn", "ddp_corpus.main:app", "--host", "0.0.0.0", "--port", "8081"]

# ------------------------------------------------------------ 语料 worker
FROM source AS corpus-worker
RUN --mount=type=cache,target=/root/.cache/pip pip-retry install ./python/ddp_contracts "./python/ddp_core[db,cjk]" \
                ./services/corpus-api ./services/corpus-worker
# 没有端口：worker 不监听任何东西，健康与水位看 /metrics 与 corpus.tasks
CMD ["ddp-corpus-worker"]

# ------------------------------------------------------------------ MCP
FROM source AS mcp
RUN --mount=type=cache,target=/root/.cache/pip pip-retry install ./python/ddp_contracts "./python/ddp_core[db,cjk]" ./services/mcp
EXPOSE 9100
CMD ["python", "-m", "ddp_mcp.server"]

# ------------------------------------------------------- 迁移（一次性容器）
FROM source AS migrate
# psql：授权那一步要跑 grants.sql。**别想着用 SQLAlchemy 代劳** ——
# GRANT / ALTER DEFAULT PRIVILEGES 不接受参数占位符，用 ORM 拼字符串
# 只会把一个纯 SQL 文件变成一段不好读的 Python
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip pip-retry install ./python/ddp_contracts "./python/ddp_core[db]" ./services/corpus-api
COPY database/ database/
WORKDIR /src/database/corpus
# 迁移与授权绑在一起，见 migrate.sh 的说明
CMD ["sh", "/src/database/corpus/migrate.sh"]
