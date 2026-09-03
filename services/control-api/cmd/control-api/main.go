// control-api 是 DeepDocParse 的企业控制面与统一入口。
//
// 它负责：组织 / 用户 / RBAC / API key / 配额 / 限速 / 计量 / 审计 /
// 上传签名 / 下载授权，以及 `/api` `/v1` `/mcp` 的统一入口与 SSE 代理。
//
// **它不做**：OCR、编译、索引、证据。那些在 Python 侧 ——
// 风险台账里「Go 重写证据规则 -> 假出处」说的就是不要越这条线。
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/api"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/migrate"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: logLevel(),
	})))

	if err := run(); err != nil {
		slog.Error("启动失败", "err", err)
		os.Exit(1)
	}
}

func run() error {
	// 占位密钥在这里就被拦下 —— 带着 change-me 跑起来的话鉴权形同虚设，
	// 而运行时不会有任何报错
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(ctx, cfg.DatabaseURL, cfg.DBMaxConns, cfg.DBMinConns)
	if err != nil {
		return err
	}
	defer db.Close()

	// 启动即迁移。**只对 control schema** —— corpus 的迁移归 alembic，
	// 两套各管各的（企业边界 5）
	if os.Getenv("CONTROL_AUTO_MIGRATE") != "false" {
		ran, err := migrate.Up(ctx, db.Pool())
		if err != nil {
			return err
		}
		if len(ran) > 0 {
			slog.Info("已应用 control 迁移", "versions", ran)
		}
		// 迁移只 `CREATE ROLE ... LOGIN`，不带口令 —— 口令是部署配置。
		// 漏了这一步，corpus-api 的每条查询都 InvalidPasswordError，
		// 而它自己的 /healthz 照样 200（那条只证明进程活着）
		if err := migrate.SetRolePasswords(ctx, db.Pool()); err != nil {
			return err
		}
	}

	objects, err := objectstore.Open(ctx, objectstore.Config{
		Endpoint:       cfg.ObjectEndpoint,
		PublicEndpoint: cfg.ObjectPublicHost,
		AccessKey:      cfg.ObjectAccessKey,
		SecretKey:      cfg.ObjectSecretKey,
		Bucket:         cfg.ObjectBucket,
		Secure:         cfg.ObjectSecure,
		PublicSecure:   cfg.ObjectPublicSecure,
		Region:         cfg.ObjectRegion,
		PresignTTL:     cfg.PresignTTL,
	})
	if err != nil {
		return err
	}

	oidcProvider, err := api.NewOIDC(ctx, cfg)
	if err != nil {
		// OIDC 配了却连不上 IdP 是**部署错误**，不能降级成"那就只用本地账号"
		// —— 那会让企业部署在没人发现的情况下退回弱鉴权
		return err
	}

	srv, err := api.NewServer(ctx, api.Deps{
		Config:  cfg,
		Store:   db,
		Objects: objects,
		Limiter: ratelimit.New(ctx, cfg.RedisURL),
		OIDC:    oidcProvider,
	})
	if err != nil {
		return err
	}
	srv.RunBackground(ctx)

	httpServer := &http.Server{
		Addr:    cfg.Addr,
		Handler: srv.Routes(),
		// **不设 WriteTimeout**：SSE 是长连接，写超时会把正常的流式问答
		// 拦腰截断，而表现是"回答到一半没了"。单个 handler 需要限时的
		// 用 http.ResponseController 自己设
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return ctx },
	}

	go func() {
		slog.Info("control-api 已启动", "addr", cfg.Addr,
			"corpus", cfg.CorpusURL, "gateway", cfg.GatewayURL, "mcp", cfg.MCPURL)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("监听失败", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	slog.Info("收到停止信号，开始优雅退出")
	// 给在途请求留时间。**SSE 长连接会被这个上限截断**，
	// 这是有意的：优雅退出不能变成永远不退出
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	return httpServer.Shutdown(shutdownCtx)
}

func logLevel() slog.Level {
	switch os.Getenv("LOG_LEVEL") {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
