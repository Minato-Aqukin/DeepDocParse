# 对象存储（开发栈 / CI / 备份演练）。MinIO 已撤回社区发行：Docker Hub 镜像
# 2026-09-11 删除，quay.io 2026-09-24 起拒绝匿名拉取，dl.min.io 二进制 410，
# 源码仓库已归档。这里从 Go 模块代理按钉住的版本编最后一个社区版本
# RELEASE.2025-10-15T17-29-55Z（上游只发了源码）。它不会再有安全更新 ——
# 只用于开发与 CI，生产对象存储另行选型。
#
# 版本只在这里钉一处：compose、scripts/backup_restore_drill.sh 都从本文件构建；
# infra/autodl 下的脚本需要二进制时也按这里的版本编。
ARG GO_VERSION=1.27

FROM golang:${GO_VERSION}-alpine AS build

# 国内构建可传 --build-arg GOPROXY=https://goproxy.cn,direct
ARG GOPROXY=https://proxy.golang.org,direct
# 伪版本 = 发布 tag 所指提交；经模块代理 + sum.golang.org 校验，内容不可替换
ARG MINIO_MODULE_VERSION=v0.0.0-20251015172955-9e49d5e7a648
ARG MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z
ENV GOPROXY=${GOPROXY} CGO_ENABLED=0 GOBIN=/out
# 与 control-api 同理：模块代理偶发断流，一次失败就白跑几分钟 —— 重试三次
RUN for i in 1 2 3; do \
      go install -trimpath \
        -ldflags "-s -w -X github.com/minio/minio/cmd.Version=2025-10-15T17:29:55Z -X github.com/minio/minio/cmd.ReleaseTag=${MINIO_RELEASE}" \
        github.com/minio/minio@${MINIO_MODULE_VERSION} && break; \
      echo "go install 第 $i 次失败，重试"; sleep 5; \
    done && /out/minio --version

FROM alpine:3.21.8
RUN apk add --no-cache ca-certificates
COPY --from=build /out/minio /usr/bin/minio
# 以 root 跑，与原官方镜像一致：已有的 miniodata 卷是它按 root 写的
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
