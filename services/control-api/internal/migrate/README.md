# 迁移文件是复制过来的

`internal/migrate/sql/*.sql` 由 `scripts/sync_control_migrations.sh` 从
`database/control/` 同步而来 —— Go 的 `//go:embed` **不能跨越模块根目录**
向上取文件，而迁移的权威位置应该与 corpus 侧并列在 `database/` 下。

CI 有一步比对两处内容一致（`scripts/check_control_migrations.py`），
漂开的表现是"改了 database/control 但服务跑的是旧 DDL"。
