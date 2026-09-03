"""跨服务的架构守卫 —— 全部是**静态**检查（AST），不装任何东西就能跑。

正因为它们要防的事恰恰是"某个环境里装不上"、"某个 cwd 下解析到别的包"，
所以判据不能依赖运行时环境。这些用例住在仓库根的 `tests/`，
因为它们横跨 `python/ddp_core`、`services/model-gateway`、`services/mcp`
三个包 —— 放进任何一个包里都会变成"只在那个包的 CI 里跑"。

## 三条守卫

1. **`ddp_core` 是叶子**：不得 import 任何服务包（`ddp_gateway` / `ddp_corpus` / `ddp_mcp`）。
   反向依赖会把服务的应用层（config / task_store / FastAPI）拖进别人的进程。
2. **依赖切分**：`ddp_core` 里碰数据库的模块要 SQLAlchemy，在 `[db]` extra 里；
   **model-gateway 一行 ORM 都不 import，venv 里压根没装**。
   MCP 与 corpus-api 是语料消费方，允许越界。
3. **所有 httpx 客户端必须 `trust_env=False`**：带代理变量的机器上，
   内网调用会被塞进代理并**卡住而不是报错**。
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "python" / "ddp_core" / "ddp_core"
GATEWAY = ROOT / "services" / "model-gateway" / "ddp_gateway"
MCP = ROOT / "services" / "mcp" / "ddp_mcp"
CORPUS = ROOT / "services" / "corpus-api" / "ddp_corpus"

# 服务包名 —— ddp_core 不得 import 其中任何一个
SERVICE_PACKAGES = ("ddp_gateway", "ddp_corpus", "ddp_mcp", "ddp_worker")
# 装在 [db] extra 里、model-gateway 的 venv 没有的东西
CORPUS_DEPS = ("sqlalchemy", "pgvector")


def _imports(path: pathlib.Path, *, top_level_only: bool) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if top_level_only else ast.walk(tree)
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def _core_modules() -> list[pathlib.Path]:
    return sorted(CORE.glob("*.py"))


def test_ddp_core_never_imports_a_service_package():
    """`ddp_core` 不得 import 任何服务包 —— 它是叶子。

    反向依赖会把某个服务的应用层（config / task_store / FastAPI）
    整个拖进另一个服务的进程。合仓前这条守的是顶层包名 `app`
    （两个发行包同名，import 谁都是错的）；包名分开之后守的是同一件事。
    """
    offenders = []
    for path in _core_modules():
        # 连函数体内的惰性 import 一起查：藏进函数里不会让它变得正确，
        # 只会让它在第一次调用时才崩
        for module in _imports(path, top_level_only=False):
            head = module.split(".")[0]
            if head in SERVICE_PACKAGES or head == "app":
                offenders.append(f"{path.name} -> {module}")
    assert not offenders, (
        "ddp_core 反向依赖了服务包：" + "; ".join(offenders)
        + "。ddp_core 是叶子，服务可以 import 它，反过来不行")


def test_service_packages_do_not_share_a_top_level_name():
    """四个 Python 服务的顶层包名必须互不相同。

    这是旧系统的头号静默陷阱：两个发行包都叫 `app`，装进同一个环境后
    `import app` 解析到哪一个**取决于 cwd 与 .pth 的字母序**。
    在有环境变量的机器上会当场报 pydantic extra_forbidden，
    在**没有**那些变量的地方（容器 / CI / 干净 checkout）却会完全正常地
    加载成功，直到第一次访问某个字段才以 AttributeError 爆掉。
    """
    names = []
    for svc in sorted((ROOT / "services").iterdir()):
        pyproject = svc / "pyproject.toml"
        if not pyproject.exists():
            continue
        text = pyproject.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("packages = ["):
                names += [p.strip().strip('"\'') for p in
                          line.split("[", 1)[1].rstrip("]").split(",") if p.strip()]
    assert names, "一个服务包名都没解析出来，探测逻辑坏了"
    assert "app" not in names, "又出现了顶层包名 app —— 这是旧系统的头号静默陷阱"
    assert len(names) == len(set(names)), f"服务包名撞车：{names}"


def test_ddp_core_init_stays_import_free():
    """`__init__.py` 必须零 import。

    有一句 `from ddp_core.models import Base` 就够了：gateway 那边
    `import ddp_core.chunking` 会连带执行 `__init__`，于是缺 sqlalchemy 直接崩。
    这个包的"最小集 / [db]"切分全靠它是空的。
    """
    init = CORE / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    bad = [ast.unparse(n) for n in ast.walk(tree)
           if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not bad, f"ddp_core/__init__.py 有 import：{bad} —— 会让最小集装不起来"


def _corpus_modules() -> set[str]:
    """顶层就 import 了 SQLAlchemy/pgvector 的 core 模块 —— 即 [corpus] 那一层。

    **实测而不是写死名单**：写死的话，哪天有人往 `chunking.py` 里加一句
    顶层 `from sqlalchemy import ...`，名单不变、守卫照绿，而 gateway 容器起不来。
    """
    out = set()
    for path in _core_modules():
        if any(m.split(".")[0] in CORPUS_DEPS
               for m in _imports(path, top_level_only=True)):
            out.add(path.stem)
    return out


def test_gateway_does_not_reach_into_the_corpus_layer():
    """model-gateway 不得 import 需要 SQLAlchemy 的 core 模块。

    它的 venv 里没有 sqlalchemy（无状态适配层）。
    违反的表现是**容器起不来**，而开发机上单测全绿 ——
    因为开发机的共享 venv 里什么都装了。
    """
    corpus = _corpus_modules()
    assert corpus, "一个 corpus 模块都没识别出来，探测逻辑坏了"

    offenders = []
    for root in (GATEWAY,):
        for path in sorted(root.rglob("*.py")):
            for module in _imports(path, top_level_only=False):
                head = module.split(".")
                if head[0] == "ddp_core" and len(head) > 1 and head[1] in corpus:
                    offenders.append(f"{path.relative_to(root.parent)} -> {module}")
    assert not offenders, (
        f"gateway 碰到了 [corpus] 层（{sorted(corpus)}）：" + "; ".join(offenders)
        + "。gateway 的 venv 没有 sqlalchemy，容器会起不来")


def test_mcp_declares_the_corpus_layer():
    """MCP 是语料服务：必须显式依赖带 ORM 的那一层。

    漏了的表现是镜像起来直接 ModuleNotFoundError（好的那种），
    但**只在生产**：开发共享 venv 里 sqlalchemy 一直在。
    """
    pyproject = (MCP.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "ddp-core[db]" in pyproject, "MCP 没声明 ddp-core[db]"


def test_the_boundary_scan_actually_covers_something():
    """反哨兵：防止上面几条因为扫不到文件而恒真。

    路径写错的表现是**全绿**，而不是报错 —— 这条就是那个报错。
    """
    files = _core_modules()
    assert len(files) >= 8, f"只扫到 {len(files)} 个 core 模块，路径可能写错了"
    assert _corpus_modules() >= {"models", "search", "types"}, \
        f"[db] 层识别结果不对：{_corpus_modules()}"
    for root in (GATEWAY, MCP, CORPUS):
        assert any(root.rglob("*.py")), f"{root} 下一个 .py 都没有，路径写错了"


def test_every_httpx_client_disables_proxy_env():
    """所有 `httpx.AsyncClient` 都必须显式 `trust_env=False`。

    带代理变量的机器（AutoDL 镜像常年自带，旧 dev 机的 SOCKS 也是）会把
    `http://127.0.0.1:...` 这种内网调用也塞进代理。**表现不是报错，是卡住**：
    worker 回 Web 层下载源文件那一步过不去，解析任务就停在 running，
    直到 30 分钟的 poll_timeout 才落成 failed —— 中间一点线索都没有。

    2026-08-29 上机时实测：`mcp_server` 两处与 Web 的 `service_client` 早就带着它，
    **只有 gateway 的 `main.py` 与 `worker/tasks.py` 漏了**。靠"记得写"维持
    一致性的东西迟早会漏一处，所以钉成守卫。
    """
    import ast

    offenders = []
    for root in (GATEWAY, MCP, CORPUS):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (target.attr if isinstance(target, ast.Attribute)
                        else getattr(target, "id", ""))
                if name not in ("AsyncClient", "Client"):
                    continue
                keywords = {kw.arg for kw in node.keywords}
                if "trust_env" not in keywords:
                    offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert not offenders, (
        f"这些 httpx 客户端没写 trust_env=False：{offenders}。"
        f"带代理变量的机器上，内网调用会被塞进代理并卡住而不是报错")


def test_the_httpx_scan_actually_finds_clients():
    """反哨兵：扫不到任何客户端时上一条会恒真。"""
    import ast

    total = 0
    for root in (GATEWAY, MCP, CORPUS):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            total += sum(
                1 for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (node.func.attr if isinstance(node.func, ast.Attribute)
                     else getattr(node.func, "id", "")) in ("AsyncClient", "Client"))
    assert total >= 3, f"只扫到 {total} 个 httpx 客户端，扫描逻辑可能坏了"


# ---------------------------------------------------------------------------
# 密钥文件不得进 git
# ---------------------------------------------------------------------------

def test_generated_secret_files_are_gitignored():
    """`scripts/dev.sh secrets` 生成的文件必须被 git 忽略。

    **这是安全边界，不是整洁问题**：`infra/env/dev.env` 里有 JWT_SECRET、
    SERVICE_TOKEN 与对象存储密钥，提交进去等于把整套鉴权公开。
    而它是脚本生成的 —— 没人会记得每次都检查 `git status`。

    用 `git check-ignore` 而不是读 .gitignore 的文本：真正生效的是 git 的
    判断（还有 .git/info/exclude、全局 ignore），比对文本会漏。
    """
    import subprocess

    generated = [
        "infra/env/dev.env",
        "infra/env/prod.env",
        ".env",
        "services/corpus-api/.env",
    ]
    not_ignored = []
    for path in generated:
        done = subprocess.run(["git", "check-ignore", "-q", path],
                              cwd=ROOT, capture_output=True)
        if done.returncode != 0:
            not_ignored.append(path)
    assert not not_ignored, (
        f"这些文件会被 git 收进去：{not_ignored}。它们含 JWT_SECRET / "
        f"SERVICE_TOKEN / 对象存储密钥")


def test_example_env_files_are_still_tracked():
    """反哨兵：把 `infra/env/` 整个忽略掉的话，样板文件也会消失，
    而那会让"照着样板填"这条路径断掉，且没人会立刻发现。"""
    import subprocess

    done = subprocess.run(["git", "check-ignore", "-q", "infra/env/dev.env.example"],
                          cwd=ROOT, capture_output=True)
    assert done.returncode != 0, "样板文件 dev.env.example 被忽略了 —— 忽略规则写得太宽"


# ---------------------------------------------------------------------------
# §4 部署清单：网关与它的 worker 必须看到同一份世界
# ---------------------------------------------------------------------------

def _compose_services(*names: str) -> dict:
    """把一组 compose 文件按 docker compose 的语义合并（够用的近似）。"""
    import yaml

    merged: dict = {}
    for name in names:
        data = yaml.safe_load((ROOT / "infra" / "compose" / name).read_text(encoding="utf-8"))
        for service, spec in (data.get("services") or {}).items():
            target = merged.setdefault(service, {})
            for key, value in spec.items():
                if key == "environment" and isinstance(value, dict):
                    target.setdefault("environment", {}).update(value)
                else:
                    target[key] = value
    return merged


def test_gateway_and_its_worker_share_redis_and_registry():
    """**解析平面的两个进程必须看到同一个 Redis 库与同一份注册表。**

    任务真相住在 arq（Redis）里，引擎清单住在注册表里：
      * REDIS_URL 不一致 = 网关入队、worker 在另一个库里空等；
      * MODELS_CONFIG 不一致 = 网关受理了一个 worker 不认识的引擎。

    两种都表现为**任务卡住**，而网关那边一切正常、状态查得到、error 是 null。
    合仓时这个 worker 整个漏掉过一次（FINDINGS F-21），补回来之后
    GPU 档位又差点只换了网关那一半 —— 所以这条守卫盯的是**每一种档位组合**。
    """
    combos = {
        "无 GPU": ("compose.dev.yml",),
        "GPU": ("compose.dev.yml", "compose.gpu.yml"),
    }
    for label, files in combos.items():
        services = _compose_services(*files)
        assert "model-gateway-worker" in services, (
            f"{label} 档位没有 model-gateway-worker —— 解析任务将永远没有消费者")
        api = services["model-gateway"].get("environment") or {}
        worker = services["model-gateway-worker"].get("environment") or {}
        for key in ("REDIS_URL", "MODELS_CONFIG"):
            assert api.get(key) == worker.get(key), (
                f"{label} 档位下 model-gateway 与 model-gateway-worker 的 {key} 不一致："
                f"{api.get(key)!r} vs {worker.get(key)!r}")


def test_only_the_entry_publishes_a_port():
    """**只有统一入口映射端口。** 其余服务不做用户鉴权，只信任入口下发的
    actor 上下文头 —— 它们一旦直接可达，那份信任就变成了任何人都能自称 admin。
    """
    services = _compose_services("compose.dev.yml")
    published = {name: spec["ports"] for name, spec in services.items()
                 if spec.get("ports")}
    # 数据面容器（PG / MinIO / Redis）映射端口是给开发机上的工具用的，
    # 它们本来就有自己的凭据；应用服务则一个都不该映
    app_services = {"control-api", "corpus-api", "corpus-worker",
                    "model-gateway", "model-gateway-worker", "mcp"}
    offenders = {n: p for n, p in published.items() if n in app_services and n != "control-api"}
    assert not offenders, (
        f"这些服务直接映射了端口：{offenders}。"
        f"它们不校验用户身份，暴露出去等于绕过整个入口")


# ---------------------------------------------------------------------------
# §5 唯一一处不可逆丢数据的脚本
# ---------------------------------------------------------------------------

def test_dropping_legacy_tables_stays_hard_to_do_by_accident():
    """删旧账号表的脚本必须**难以被误触发**。

    它是这个仓库里唯一一处不可逆地丢数据的地方（`gc.py` 至少还有宽限期），
    所以三条性质要一直成立：

    1. **不在 alembic 迁移链里** —— 进了链，任何一次 `upgrade head` 都会执行它，
       包括在对账通过之前、包括在一台刚从生产快照恢复出来的库上；
    2. **默认只检查**（DROP 必须挡在 `args.apply` 后面）；
    3. **没有 --force** —— 前提不成立时该做的是查清楚，不是绕过。

    第 3 条最容易在"急着上线"的时候被加回来，所以钉在这里。
    行为侧的拒绝逻辑已做过变异确认（旧表行数灌到比新表多，
    `--apply` 也拒绝，四张表一张不少）。
    """
    import ast

    script = ROOT / "database" / "migrator" / "drop_legacy_account_tables.py"
    assert script.exists(), "删表脚本不见了 —— 它是有意存在的，不是遗留物"
    source = script.read_text(encoding="utf-8")

    # 1. 迁移链里不许有 DROP 这四张表的动作。
    #
    # **判据是内容，不是文件名。** 第一版按文件名匹配（同时含 "drop" 与
    # "account"），于是把 `op.drop_table("users")` 放进一个叫 `0014_cleanup.py`
    # 的文件里就能整个绕过 —— 而这条守的偏偏是三条里最要紧的那条：
    # 迁移链是**每次 `dev.sh up` 自动跑**的（`corpus-migrate` 一次性容器，
    # 所有应用容器 depends_on 它跑完），进了链就等于每次起服务都删一次。
    # 演练流程更糟：`pg_dump | psql` 复制一份 -> 跑迁移 -> 跑迁移器对账，
    # 第二步就会把第四步要对账的源数据删掉。
    versions = ROOT / "database" / "corpus" / "alembic" / "versions"
    legacy = {"users", "api_keys", "usage_records", "file_tokens"}
    # **只看 `upgrade()`。** `downgrade()` 里 drop 自己建的表是它的本分
    # （`0001_initial` 的 downgrade 就 drop 这四张）—— 把那些也算上的话
    # 守卫会对着正确的代码报红，然后被人加白名单加到失效。
    offenders = []
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrades = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "upgrade"]
        for node in (c for fn in upgrades for c in ast.walk(fn)):
            # op.drop_table("users")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "drop_table"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in legacy):
                offenders.append(f"{path.name}: op.drop_table({node.args[0].value!r})")
            # 裸 SQL 里的 DROP TABLE users
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                if "DROP TABLE" in upper:
                    for table in legacy:
                        if table.upper() in upper:
                            offenders.append(f"{path.name}: SQL 里 DROP TABLE {table}")
    assert not offenders, (
        f"alembic 迁移链里出现了删旧账号表的动作：{offenders}。"
        f"迁移链是每次 dev.sh up 自动跑的 —— 进了链就等于每次起服务都删一次，"
        f"而演练流程会因此在对账之前就把源数据删掉。"
        f"删表要走 database/migrator/drop_legacy_account_tables.py")

    # 2. DROP 之前必须有一道 args.apply 的闸。
    #
    # 两种写法都算数：`if args.apply:` 包住它，或者 `if not args.apply: return`
    # 提前返回。**只认其中一种是不对的** —— 第一版守卫就只认了前一种，
    # 于是对着写成后一种（而且更好读）的真脚本报红。
    # 守卫要验的是性质，不是某一种句法。
    tree = ast.parse(source)
    # **别把文档字符串当代码。** 脚本的说明里就写着
    # `DROP TABLE users CASCADE 会顺手删掉…`，按"文本里有没有这几个字"
    # 找的话，第一个命中永远是第 2 行的模块 docstring ——
    # 于是守卫报"DROP 前面没有闸门"，而它指的那个 DROP 根本不是代码。
    # 这是同一个毛病第三次出现（前两次：只认一种 if 写法、按文本查 --force）。
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    drops = [n for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and "DROP TABLE" in n.value and id(n) not in docstrings]
    assert drops, "脚本里没有 DROP TABLE（文档字符串不算）—— 判据失效了，先看它是不是改写过"
    first_drop = min(n.lineno for n in drops)

    gates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or node.lineno >= first_drop:
            continue
        if "apply" not in ast.dump(node.test):
            continue
        # 包住 DROP，或者自己 return / raise 掉
        covers = any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                     and "DROP TABLE" in c.value for c in ast.walk(node))
        exits = any(isinstance(c, (ast.Return, ast.Raise)) for c in ast.walk(node))
        if covers or exits:
            gates.append(node.lineno)
    assert gates, (
        f"第 {first_drop} 行的 DROP TABLE 之前没有任何 args.apply 的闸门 ——"
        f"默认跑一下就会删表")

    # **这条是静态近似**：它证明"闸门在那儿"，不证明"闸门关得住"。
    # 关得住是靠行为验的 —— 把旧表行数灌到比新表多，`--apply` 也必须拒绝、
    # 四张表一张不少（2026-09-02 实测过）。静态判据挡的是"闸门被删掉"。

    # 3. 没有 --force。**判据是 argparse 里有没有这个选项，不是文本里
    #    有没有这五个字符** —— 脚本的说明里就写着"不提供 --force"，
    #    按文本查会把那句说明本身当成违规（第一版就是这么红的）。
    # 名单不是"叫 --force 的选项"，是"**任何一个能跳过检查的开关**" ——
    # 换个名字叫 --yes 一样能绕过。这一条守的是性质
    escapes = {"--force", "-f", "--yes", "-y", "--skip-checks", "--no-verify",
               "--ignore-checks", "--i-know-what-im-doing"}
    forced = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
              and any(isinstance(a, ast.Constant) and a.value in escapes
                      for a in n.args)]
    assert not forced, (
        "删表脚本加了 --force。前提不成立的时候要做的是查清楚，不是绕过 ——"
        "这条如果真要改，先改掉脚本开头那段说明并说清为什么")


# ---------------------------------------------------------------------------
# §6 跨包的 tests 命名空间不许靠运气
# ---------------------------------------------------------------------------

def test_every_package_puts_itself_first_on_pythonpath():
    """每个包的 `pythonpath` 第一项必须是 `"."`。

    各包的 `tests/` 都没有 `__init__.py`，于是 `tests` 是一个**隐式命名空间包**，
    横跨 ddp_core / corpus-api / corpus-worker / model-gateway / mcp / eval
    六个 tests 目录 —— `from tests.conftest import ...` 落到哪一个，
    完全取决于 sys.path 顺序。

    而顺序取决于**怎么起 pytest**：

      * `python -m pytest` 会把 CWD 放进 sys.path（本机一直这么跑，所以一直对）
      * 裸 `pytest` 入口脚本**不会** —— 只剩几个可编辑安装的路径，谁先谁赢

    CI 跑的正是裸 `pytest`。于是 corpus-api 的用例导入到了 ddp_core 的 conftest：

        ImportError: cannot import name 'CHAT' from 'tests.conftest'
                     (.../python/ddp_core/tests/conftest.py)

    本机复现方式：在包目录下用**裸 `pytest`**（不是 `python -m pytest`）跑一次。

    把 `"."` 显式排在最前，两种起法就都对了。这条守卫钉住它别被删掉 ——
    删掉之后本机照样全绿，只有 CI 会红，而那时人已经在改别的东西了。
    """
    import tomllib

    packages = ["python/ddp_core", "services/corpus-api", "services/corpus-worker",
                "services/model-gateway", "services/mcp", "eval"]
    offenders = []
    for rel in packages:
        pyproject = ROOT / rel / "pyproject.toml"
        if not pyproject.exists():
            offenders.append(f"{rel}: 没有 pyproject.toml")
            continue
        conf = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        ini = conf.get("tool", {}).get("pytest", {}).get("ini_options", {})
        path = ini.get("pythonpath")
        if not path:
            offenders.append(f"{rel}: 没有 pythonpath")
        elif path[0] != ".":
            offenders.append(f"{rel}: pythonpath 第一项是 {path[0]!r}，不是 '.'")
    assert not offenders, (
        "这些包没有把自己排在 pythonpath 最前：" + "; ".join(offenders)
        + "。裸 `pytest` 起的时候，它们的 tests 会被别的包的同名目录顶掉")


def test_the_pythonpath_scan_actually_reads_something():
    """反哨兵：一个包都没查到时上一条恒真。"""
    import tomllib

    conf = tomllib.loads((ROOT / "services" / "corpus-api" / "pyproject.toml")
                         .read_text(encoding="utf-8"))
    ini = conf.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert ini.get("pythonpath"), "读不到 corpus-api 的 pythonpath —— 判据失效了"
