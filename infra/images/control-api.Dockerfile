# Go 控制面。多阶段构建：编译产物是一个静态二进制，运行镜像里没有工具链。
ARG GO_VERSION=1.27

FROM golang:${GO_VERSION}-alpine AS build

# 国内构建可传 --build-arg GOPROXY=https://goproxy.cn,direct
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=${GOPROXY} CGO_ENABLED=0

WORKDIR /src
# 先只拷 go.mod/go.sum：依赖没变时 `go mod download` 这一层能命中缓存
COPY services/control-api/go.mod services/control-api/go.sum ./
RUN go mod download

COPY services/control-api/ ./
# 迁移文件由 //go:embed 打进二进制 —— 运行镜像里因此不需要它们，
# 也不会出现"镜像里的 SQL 与仓库里的不一致"（scripts/check_control_migrations.py
# 盯着仓库内的两份副本）
RUN go build -trimpath -ldflags="-s -w" -o /out/control-api ./cmd/control-api && \
    go build -trimpath -ldflags="-s -w" -o /out/control-migrate ./cmd/control-migrate

FROM alpine:3.21
# 证书：OIDC 与对象存储都可能走 https
RUN apk add --no-cache ca-certificates tzdata && \
    adduser -D -u 10001 ddp
COPY --from=build /out/control-api /usr/local/bin/control-api
COPY --from=build /out/control-migrate /usr/local/bin/control-migrate

# **不以 root 跑**：这是唯一直面公网的进程
USER ddp
EXPOSE 8080
ENTRYPOINT ["control-api"]
