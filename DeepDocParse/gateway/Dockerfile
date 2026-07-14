FROM python:3.12-slim

# 国内构建可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY app ./app

# gateway 与 ARQ worker 同镜像，compose 里用不同 command 启动：
#   gateway: uvicorn app.main:app --host 0.0.0.0 --port 9000
#   worker:  arq app.worker.tasks.WorkerSettings
EXPOSE 9000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
