#!/bin/sh
# corpus schema 的迁移入口：alembic + 授权。
#
# **两件事必须绑在一起。** 只跑 alembic 的话，表建出来归超级用户所有，
# 而服务进程用的 `ddp_corpus` 一个权限都没有 —— 表现是每个容器都 healthy、
# 迁移全部成功，而任何一次写库都 `permission denied`。
# 见 grants.sql 开头那段。
set -e

# **别写死绝对路径。** 容器里仓库在 /src，裸进程部署（infra/autodl/stack.bash）
# 里在别处 —— 写死的表现是迁移跑完、grants 那一步 "No such file"，
# 而 `set -e` 之下这确实会红，但错误信息指向文件系统而不是部署形态。
HERE="$(cd "$(dirname "$0")" && pwd)"

alembic upgrade head

# 授权必须在迁移之后：ALTER DEFAULT PRIVILEGES 只管**之后**建的对象，
# 已经建好的那些要显式 GRANT 一遍
psql_url=$(python - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

# alembic 用的是 postgresql+asyncpg://，psql 只认 postgresql://
raw = os.environ["DATABASE_URL"]
parts = urlsplit(raw)
print(urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, parts.fragment)))
PY
)

# ON_ERROR_STOP：没有它，psql 会把失败的 GRANT 打成一行日志然后退出 0，
# 而那正是"迁移成功但服务没权限"这个故障的来源
psql "$psql_url" --set ON_ERROR_STOP=1 -f "$HERE/grants.sql"
echo "corpus schema 迁移与授权完成"
