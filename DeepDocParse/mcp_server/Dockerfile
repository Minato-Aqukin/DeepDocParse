FROM python:3.12-slim

# 国内构建可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
COPY mcp_server/pyproject.toml /tmp/mcp/pyproject.toml
COPY mcp_server/server.py mcp_server/corpus.py /tmp/mcp/
RUN pip install --no-cache-dir /tmp/mcp

COPY gateway/ddp_core /app/ddp_core
COPY mcp_server/server.py mcp_server/corpus.py /app/

EXPOSE 9100
CMD ["python", "server.py"]
