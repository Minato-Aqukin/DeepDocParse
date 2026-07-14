# mineru 镜像 —— 改编自官方 docker/china/Dockerfile（mineru-3.4.4）
# https://github.com/opendatalab/MinerU/blob/mineru-3.4.4/docker/china/Dockerfile
# 改动（相对官方）：
#   1. mineru[core] 锁定 ==3.4.4（铁律：镜像 pin 版本，禁止 latest）
#   2. dev 只下载 pipeline 模型（8GB 卡跑 pipeline 后端；prod vlm 模型在 M4 服务器上用 -m all）
# 基础镜像 vLLM 0.21.0 / CUDA 13.0，宿主驱动 591.74（CUDA 13.1）满足要求。
# 注：本机 docker.io 经系统代理可直连（DaoCloud 镜像源大文件传输不稳），故用官方源。

FROM vllm/vllm-openai:v0.21.0

# opencv 依赖 libgl；中文渲染需要 Noto 字体
RUN apt-get update && \
    apt-get install -y \
        fonts-noto-core \
        fonts-noto-cjk \
        fontconfig \
        libgl1 && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install -U 'mineru[core]==3.4.4' -i https://mirrors.aliyun.com/pypi/simple --break-system-packages && \
    python3 -m pip cache purge

# 模型内置进镜像，运行时离线（MINERU_MODEL_SOURCE=local）
RUN /bin/bash -c "mineru-models-download -s modelscope -m pipeline"

ENTRYPOINT ["/bin/bash", "-c", "export MINERU_MODEL_SOURCE=local && exec \"$@\"", "--"]
