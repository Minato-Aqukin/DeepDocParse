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
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

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
RUN pip install ./python/ddp_contracts ./python/ddp_core ./services/model-gateway
EXPOSE 9000
CMD ["uvicorn", "ddp_gateway.main:app", "--host", "0.0.0.0", "--port", "9000"]

# -------------------------------------------------------------- 语料 API
FROM source AS corpus-api
RUN pip install ./python/ddp_contracts "./python/ddp_core[db,cjk]" ./services/corpus-api
EXPOSE 8081
CMD ["uvicorn", "ddp_corpus.main:app", "--host", "0.0.0.0", "--port", "8081"]

# ------------------------------------------------------------ 语料 worker
FROM source AS corpus-worker
RUN pip install ./python/ddp_contracts "./python/ddp_core[db,cjk]" \
                ./services/corpus-api ./services/corpus-worker
# 没有端口：worker 不监听任何东西，健康与水位看 /metrics 与 corpus.tasks
CMD ["ddp-corpus-worker"]

# ------------------------------------------------------------------ MCP
FROM source AS mcp
RUN pip install ./python/ddp_contracts "./python/ddp_core[db,cjk]" ./services/mcp
EXPOSE 9100
CMD ["python", "-m", "ddp_mcp.server"]

# ------------------------------------------------------- 迁移（一次性容器）
FROM source AS migrate
RUN pip install ./python/ddp_contracts "./python/ddp_core[db]" ./services/corpus-api
COPY database/ database/
WORKDIR /src/database/corpus
CMD ["alembic", "upgrade", "head"]
