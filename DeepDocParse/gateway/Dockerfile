FROM python:3.12-slim

# 国内构建可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY app ./app
# **ddp_core 不能漏。** `app.main` 挂载 routers 时就会 import 它
# （routers/extract.py 与 worker/tasks.py 都用），漏了这行镜像起来直接
# ModuleNotFoundError: No module named 'ddp_core' —— gateway 与 worker 一起挂。
# 注意上面那条 `pip install .` 只拉依赖：这个镜像靠 COPY + WORKDIR /app 提供包本身
# （实测构建出来的 wheel 里只有 dist-info，没有代码）。
COPY ddp_core ./ddp_core

# gateway 与 ARQ worker 同镜像，compose 里用不同 command 启动：
#   gateway: uvicorn app.main:app --host 0.0.0.0 --port 9000
#   worker:  arq app.worker.tasks.WorkerSettings
EXPOSE 9000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
