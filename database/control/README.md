# control schema 迁移

Go control-api 拥有的表。按文件名顺序执行，账本在 `control.schema_migrations`。

```bash
go run ./cmd/control-migrate -database "$CONTROL_DATABASE_URL" up
go run ./cmd/control-migrate -database "$CONTROL_DATABASE_URL" status
```

## 为什么不用 Alembic

Alembic 属于 Python 侧（`database/corpus/`）。让 Go 的表由 Python 的迁移工具
管理，就等于让 Python 有权改 Go 的 schema —— 而这份重构的第五条边界正是
「一个数据对象只能有一个写入所有者」。两套迁移各管各的 schema，
**没有跨 schema 外键**，因此两边的发布顺序互不依赖。

## 校验和

每个文件的 sha256 记进 `schema_migrations`。改一个已应用过的文件，
`status` 会报"校验和不符"—— 而不是安静地当作已经执行过。
历史上"改了迁移文件但库里是旧结构"是最难查的一类问题。
