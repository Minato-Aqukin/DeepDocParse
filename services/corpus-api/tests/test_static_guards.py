"""**静态守卫**：不连库、不 import 被测模块，只解析源码就能查出来的错。

住在这里的都有一个共同点 —— **常规单测结构上覆盖不到，漏改不会红**：
迁移文件从不被 import；脚本也不被 import；而 `index.search()` 少传一个参数，
跑起来照样是一份"看着挺像样"的答案。

§1 迁移与脚本里 import 的模块必须真的存在
§2 每一次检索都必须带相似度下限

---

§1 **为什么需要这条**：阶段 1 把 `ddp_corpus/tokenize.py` 等模块搬进了 `ddp_core`，
但 `alembic/versions/0005_*.py:128` 里那句 `from app.tokenize import tokenized`
没跟着改。已有库早就过了 0005，所以升级路径上看不见 —— 而**任何全新部署
都会死在那里并停在 0004**（`deploy/docker.bash`、CI 接真库、灾备重建）。
阶段 1 的验收放过了它，阶段 2a 的验收才抓到。

两套 pytest 都覆盖不到这类问题：迁移文件从不被 import，脚本也不被 import。
所以单独用一条**静态**守卫扫它们 —— 不需要数据库，不需要跑迁移。

§2 见文件下半部分 `test_every_retrieval_passes_the_similarity_floor` 的说明。
"""
import ast
import importlib
import importlib.util
import pathlib

import pytest

SERVICE = pathlib.Path(__file__).resolve().parent.parent
REPO = SERVICE.parent.parent

# 扫这些地方的 import：它们都不会被单测 import 到，因此漏改不会红。
# 迁移住在 database/corpus（与 control 侧并列），脚本住在仓库根 scripts/
SCAN_DIRS = [REPO / "database" / "corpus" / "alembic" / "versions", REPO / "scripts"]

# 只查本项目的顶层包 —— 第三方依赖由安装环节保证，不该在这里重复校验
OWNED_PREFIXES = ("ddp_corpus.", "ddp_core.", "ddp_contracts.", "ddp_worker.")

#: **已经不存在的顶层包。** 合仓前两个发行包都叫 `app`；改名之后残留的
#: `from app.x import y` 不会被上面那条守卫抓到（它只查"本项目的包存不存在"，
#: 而 `app` 压根不在名单里），于是漏改会一路绿到全新部署时才炸。
#: 合仓当天就有一处残留：`0008_citation_rank_and_backfill.py` 里的
#: `from app.backfill import backfill`。
DEAD_PREFIXES = ("app.",)


def _local_imports(path: pathlib.Path) -> set[str]:
    """文件里所有指向本项目两个顶层包的 import（含函数体内的惰性 import）。"""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(OWNED_PREFIXES):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(OWNED_PREFIXES):
                    found.add(alias.name)
    return found


def _dead_imports(path: pathlib.Path) -> set[str]:
    """指向已经不存在的顶层包的 import。"""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(DEAD_PREFIXES) or node.module == "app":
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(DEAD_PREFIXES) or alias.name == "app":
                    found.add(alias.name)
    return found


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.py") if p.name != "__init__.py"))
    return out


@pytest.mark.parametrize("path", _files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_migrations_and_scripts_import_modules_that_exist(path):
    """迁移与脚本引用的每一个 app.* / ddp_core.* 模块都要能 import。

    **只解析不执行** —— 迁移里的 import 大多在函数体内，真跑要连数据库。
    这里用 importlib 查模块存不存在就够了：漏改的表现正是"模块没了"。
    """
    missing = []
    for module in sorted(_local_imports(path)):
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            missing.append(module)
    assert not missing, (
        f"{path.relative_to(REPO)} 引用了不存在的模块：{missing}。"
        f"搬家之后忘了改这里 —— 迁移与脚本都不被单测 import，漏改不会红，"
        f"但全新部署会死在这一步"
    )


@pytest.mark.parametrize("path", _files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_migrations_and_scripts_do_not_import_the_dead_app_package(path):
    """不许再出现 `app.*`。

    合仓前两个发行包都叫 `app`。改名之后，上一条守卫**抓不到残留**：
    它只查"本项目的包存不存在"，而 `app` 压根不在名单里，
    于是 `from app.backfill import backfill` 一路绿到全新部署时才炸
    （合仓当天 0008 里就有一处）。
    """
    dead = sorted(_dead_imports(path))
    assert not dead, (
        f"{path.relative_to(REPO)} 还在 import 已经不存在的顶层包：{dead}。"
        f"合仓时四个 Python 服务的包名都改了（ddp_gateway / ddp_corpus /"
        f" ddp_worker / ddp_mcp），这里漏改了")


def test_the_scan_actually_covers_something():
    """防止上面那条因为扫不到文件而恒真（空 parametrize 会静默通过）。"""
    files = _files()
    assert len(files) >= 6, f"扫到的文件太少（{len(files)}），SCAN_DIRS 是不是写错了"
    total = sum(len(_local_imports(f)) for f in files)
    # 3 是合仓后的实际值（迁移里 import ddp_core 的那几处）。
    # **不要把它调低到 0 附近**：这条的全部意义就是"扫到的东西不能为零"
    assert total >= 3, f"一条本项目的 import 都没扫到（{total}），解析逻辑可能坏了"


#: 语料 MCP 必须拿到的那几个键。少一个的表现是：所有容器都健康，
#: 直到**第一次 MCP 调用**才报数据库认证失败 —— 部署脚本特有的静默错位，
#: 常规 API 单测与 `compose config` 都抓不到。
CORPUS_MCP_KEYS = {
    "CORPUS_DATABASE_URL", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY", "MINIO_BUCKET",
}

COMPOSE = REPO / "infra" / "compose" / "compose.dev.yml"


def _compose_service(name: str) -> dict:
    import yaml

    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return spec["services"][name]


def _env_of(service: dict) -> dict:
    """compose 的 environment 可能是 map 也可能是 list，两种都要认。"""
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env if "=" in item)
    return env


def test_compose_passes_corpus_credentials_to_the_mcp_service():
    """语料 MCP 必须拿到 PG 与对象存储的凭据。

    合仓前这条查的是 quickstart 脚本有没有把随机密钥同步进 `.env.mcp`；
    脚本删了（compose 给每个服务显式的 environment 列表，不再需要写 .env），
    但**要守的事一件没少**：漏一个键 = 所有容器健康、第一次 MCP 调用才炸。
    """
    env = _env_of(_compose_service("mcp"))
    missing = sorted(CORPUS_MCP_KEYS - set(env))
    assert not missing, f"compose 没把这些语料配置给 mcp 服务：{missing}"


def test_corpus_keys_stay_out_of_the_model_gateway_environment():
    """语料/控制面的键**不许**出现在网关的 environment 里。

    网关的 Settings 是 `extra="forbid"`（故意的，用来抓拼错的键），
    而 pydantic-settings 直接读 cwd 下的 `.env` 文件，不只是环境变量 ——
    这几个键混进去，**裸进程起网关会当场 extra_forbidden 拒绝启动**。
    2026-08-29 在 AutoDL 上实测撞到：容器部署没事（compose 给每个服务
    显式的 environment 列表），而 AutoDL 跑不了 docker，只能裸进程。

    顺带钉住"网关不碰数据库"：它出现 DATABASE_URL 就说明无状态原则破了。
    """
    env = set(_env_of(_compose_service("model-gateway")))
    leaked = sorted(env & (CORPUS_MCP_KEYS | {
        "DATABASE_URL", "CONTROL_URL", "JWT_SECRET", "CONTROL_DATABASE_URL"}))
    assert not leaked, (
        f"这些键出现在网关的 environment 里：{leaked}。"
        f"网关的 Settings 是 extra=forbid，裸进程部署会拒绝启动；"
        f"而 DATABASE_URL 一类的出现意味着无状态原则破了")


def test_only_the_entry_publishes_ports():
    """**只有入口映射端口。**

    corpus-api / model-gateway / mcp 都不做用户鉴权，只信任入口下发的
    actor 上下文头 —— 把它们暴露到公网等于任何人都能自称 admin。
    数据面（PG / MinIO / Redis）在开发档位映射端口是有意的（要能用客户端连），
    生产档位另有覆盖。
    """
    import yaml

    spec = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    app_services = {"control-api", "corpus-api", "corpus-worker", "model-gateway", "mcp"}
    published = {name for name, svc in spec["services"].items()
                 if name in app_services and svc.get("ports")}
    assert published == {"control-api"}, (
        f"除入口外还有服务映射了端口：{sorted(published - {'control-api'})}。"
        f"它们不做用户鉴权，暴露出去等于任何人都能自称 admin")


# ---------------------------------------------------------------------------
# §2 每一次检索都必须带相似度下限
# ---------------------------------------------------------------------------

APP = SERVICE / "ddp_corpus"

# 下限的唯一来源。写死成字面量（哪怕数值一样）也算违规：
# 阈值要能被一处配置改掉，散落的字面量会让"调阈值"变成一次全仓库搜索
FLOOR = "qa_min_similarity"


def _search_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    """`ddp_corpus/` 下所有 `<...>index.search(...)` 调用。"""
    out: list[tuple[pathlib.Path, ast.Call]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "search"
                    and ast.unparse(node.func.value).endswith("index")):
                out.append((path, node))
    return out


def test_every_retrieval_passes_the_similarity_floor():
    """检索调用必须在 SearchIndex 或紧随其后的可审计门控应用统一下限。

    **这条守的是不变式 1：出处必须是真出处。** 下限的作用是让"向量路判定全都
    不相关"的问题不要靠共现词捞出 chunk 来充当出处 —— 而 `verified` 只看有没有
    裁剪图，于是假出处还会被打上"已做视觉验证"，比不给出处更糟。

    问答路有行为断言钉着（test_qa.py），但抽取路与跨文档检索路没有：
    阶段 2a 验收把这两处的 `settings.qa_min_similarity` 换成 `0.0`，**152 例全绿**。
    行为断言给这两条路补起来代价不小（要造"词面命中但语义不相关"的语料），
    阶段 6 的问答路是唯一例外：它要保留低分候选和拒绝原因，所以 SearchIndex
    不得先丢数据，必须取全后立刻交给 `gate_candidates`。其余检索路仍在 search
    调用处应用下限。两种形状都在这里钉死。
    """
    violations = []
    for path, call in _search_calls():
        where = f"{path.relative_to(SERVICE)}:{call.lineno}"
        kw = next((k for k in call.keywords if k.arg == "min_similarity"), None)
        if kw is None:
            violations.append(f"{where} 没传 min_similarity")
        elif path.name == "qa.py" and ast.unparse(kw.value) == "-1.01":
            continue
        elif not (isinstance(kw.value, ast.Attribute) and kw.value.attr == FLOOR):
            violations.append(f"{where} 的 min_similarity 是 {ast.unparse(kw.value)}，"
                              f"不是 settings.{FLOOR}")
    assert not violations, (
        "检索少了相似度下限 —— 低于下限的词面命中会成为出处，且被打上 verified：\n  "
        + "\n  ".join(violations))


def test_qa_deferred_gate_uses_the_configured_floor():
    """问答取全候选后必须用同一配置门控；否则假出处会从后门进入回答。"""
    path = APP / "qa.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func).endswith("gate_candidates")]
    assert len(calls) == 1, f"预期唯一 gate_candidates 调用，实际 {len(calls)}"
    kw = next((item for item in calls[0].keywords if item.arg == "min_similarity"), None)
    assert kw is not None
    assert isinstance(kw.value, ast.Attribute) and kw.value.attr == FLOOR, \
        f"问答门控阈值不是 settings.{FLOOR}: {ast.unparse(kw.value)}"


def test_the_search_scan_actually_finds_the_call_sites():
    """防止上面那条因为匹配不到调用而恒真（改个变量名就能让它静默失效）。"""
    calls = _search_calls()
    files = {p.relative_to(SERVICE).as_posix() for p, _ in calls}
    assert len(calls) >= 3, f"只扫到 {len(calls)} 处 index.search，匹配逻辑可能坏了"
    # 三条检索路各一处：问答、抽取、跨文档检索。少了任何一条都说明扫漏了
    assert files >= {"ddp_corpus/qa.py", "ddp_corpus/extraction.py",
                     "ddp_corpus/routers/search.py"}, \
        f"三条检索路没扫全，实际扫到 {sorted(files)}"


# ---------------------------------------------------------------------------
# §3 出处锚定判据只许有一份
# ---------------------------------------------------------------------------


def test_every_anchor_judgement_is_the_same_function():
    """读路径、历史回填、老的接回逻辑，用的必须是**同一个函数对象**。

    判据回答的是同一个问题："这条出处指的，还是当初那段原文吗？"
    三处一旦有差异，回填就会把一批本来对得上的标成失效（可惜，但安全），
    或者把对不上的标成有效 —— **那就是带着已验证标记的假出处**
    （plan.md §9 不变式 1）。

    这条守的是"同一个对象"而不是"行为一致"：行为一致要靠穷举输入才能证明，
    而同一个对象是结构性的，改不掉。
    """
    from ddp_core import anchor
    import ddp_corpus.backfill as backfill
    import ddp_corpus.evidence as evidence

    assert evidence.same_content is anchor.same_content
    assert backfill.same_content is anchor.same_content
    # 阶段 4 删掉了 app.qa 里那份老的接回逻辑（连同 messages.citations 列）。
    # **判据的实现只许有一处** —— 全仓库扫一遍，除 anchor.py 外不许再出现
    import pathlib

    reimplemented = []
    for path in sorted((SERVICE / "ddp_corpus").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if 'rstrip("…")' in text:
            reimplemented.append(path.name)
    assert not reimplemented, \
        f"{reimplemented} 又自己实现了一遍锚定判据 —— 判据一漂就会产出假出处"


def test_the_two_anchor_paths_really_differ():
    """严格路与宽松路**不能是同一件事** —— 否则 content_digest 白加了。

    反哨兵：如果 `same_content` 退化成只看 snippet，指纹就形同虚设，
    而"块尾被改掉"这类改动正好是 snippet 判据抓不到、指纹能抓到的。
    """
    from ddp_core.anchor import digest_of, same_content

    original = "开头这段话会被存进 snippet，后面还有很长一截内容。"
    tampered = original + "（后来被追加的一段，snippet 里看不见）"
    snippet = original[:12]

    # 宽松路：snippet 还在里面 -> 放行（这正是老判据的盲区）
    assert same_content(snippet=snippet, chunk_text=tampered, digest="") is True
    # 严格路：指纹对不上 -> 拦住
    assert same_content(snippet=snippet, chunk_text=tampered,
                        digest=digest_of(original)) is False


def test_citation_shape_has_exactly_one_implementation():
    """出处的对外形状只许有一处实现（阶段 4 合并的）。

    问答与抽取必须给出完全一样的形状 —— 前端的 CitationChip 两边共用一个组件。
    此前是各写一份，靠抽取那份 docstring 里"形状必须与 conversations 一致"
    这句话维持，而**靠注释维持的一致性迟早会破**：阶段 3 就抓到过
    两份接回逻辑走岔的后果（抽取的出处被无条件标成有效）。
    """
    import ast

    hits = []
    for path in sorted((SERVICE / "ddp_corpus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.endswith("citation_out"):
                hits.append(f"{path.relative_to(SERVICE)}::{node.name}")
    assert hits == ["ddp_corpus/evidence.py::citation_out"], \
        f"出处形状又出现了第二份实现：{hits}"


def test_fresh_is_an_assertion_not_a_default():
    """`resolved` 不许有默认值 —— 忘了算就必须缺，不能静默变成"有效"。

    写成 `setdefault("resolved", True)` 的话，任何漏算 resolved 的路径都会被
    判成有效出处。阶段 3 抓到的抽取平面 bug 就是这么来的。
    """
    from ddp_corpus.evidence import citation_out

    plain = citation_out("d1", {"seq": 0, "crop_key": None})
    assert "resolved" not in plain, "没人给 resolved，却被兜底成了有效"
    assert citation_out("d1", {"seq": 0, "crop_key": None}, fresh=True)["resolved"] is True


# ---------------------------------------------------------------------------
# §3 知识层可以整体关掉 —— 既有问答/检索链路不许 import 它
# ---------------------------------------------------------------------------

# 阶段 7 之前就存在、且被 `eval_agent` 那张表衡量的生产路径
QA_PATH = (
    SERVICE / "ddp_corpus" / "qa.py",
    SERVICE / "ddp_corpus" / "extraction.py",
    SERVICE / "ddp_corpus" / "indexing.py",
    SERVICE / "ddp_corpus" / "routers" / "conversations.py",
    SERVICE / "ddp_corpus" / "routers" / "search.py",
)
KNOWLEDGE_MODULES = ("ddp_corpus.knowledge", "ddp_corpus.review_export", "ddp_corpus.routers.knowledge",
                     "ddp_core.knowledge")


def test_the_existing_qa_path_does_not_import_the_knowledge_layer():
    """plan.md 阶段 7 验收的「图关掉后既有指标不变」，结构上的那一半。

    数值那一半在 `scripts/eval_graph.py`：对阶段 6 报表记录的基线逐字段比。
    **但只比数字挡不住"知识层被悄悄挂进问答链"** —— 那种改动会让基线一起漂，
    而人很容易顺手把基线也改了（`agent_baseline` 就在仓库里）。
    这条守卫改成机械可判的：这几个文件里出现任何一个知识层模块就红。

    反过来说，`ddp_corpus/routers/knowledge.py` 依赖问答侧的东西（evidence / metering）
    是允许的 —— 依赖方向只许从新到旧，关掉新的那头不影响旧的。
    """
    offenders = []
    for path in QA_PATH:
        assert path.is_file(), f"{path} 不在了 —— 这条守卫的扫描面得跟着改"
        for module in sorted(_local_imports(path)):
            if any(module == name or module.startswith(name + ".")
                   for name in KNOWLEDGE_MODULES):
                offenders.append(f"{path.relative_to(SERVICE)} -> {module}")
    assert not offenders, (
        f"既有问答链引用了知识层：{offenders}。"
        f"知识层必须能被 knowledge_enabled=False 整体关掉而不动既有指标；"
        f"真要挂进去，先改 plan.md 的验收标准并重跑 eval_agent 定新基线")


# ---------------------------------------------------------------------------
# §5 字节流不得经过本服务
# ---------------------------------------------------------------------------

def test_corpus_api_accepts_no_file_bodies():
    """**没有任何端点收文件体。**

    不变式 6：大文件不得完整进入应用进程内存，也不得由应用进程长期中转
    下载流量。合仓前 `POST /api/documents` 用 `UploadFile` 收 multipart，
    再 `_read_capped` 把整份文件读进一个 `bytes` —— 200MB 的文件就是 200MB
    的常驻内存，而扩容应用等于放大对象存储的带宽中转。

    现在字节流由浏览器凭预签名直传对象存储，本服务只收元数据。
    这条守卫钉住那件事：**任何人想把 `UploadFile` 加回来，这里立刻红**。

    静态扫而不是跑起来查路由：跑起来查只能发现"已经加上去的"，
    而静态扫连"import 了但还没用"都拦得住。
    """
    import ast

    offenders = []
    for path in sorted((SERVICE / "ddp_corpus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
                for alias in node.names:
                    if alias.name in ("UploadFile", "File"):
                        offenders.append(f"{path.relative_to(SERVICE)} -> {alias.name}")
            if isinstance(node, ast.Name) and node.id == "UploadFile":
                offenders.append(f"{path.relative_to(SERVICE)}:{node.lineno} 用到 UploadFile")
    assert not offenders, (
        "语料 API 里出现了文件上传参数：" + "; ".join(sorted(set(offenders)))
        + "。字节流必须直传对象存储，见 ddp_corpus/ingest.py 的模块说明")


def test_the_upload_scan_actually_looks_at_files():
    """反哨兵：扫不到文件时上一条恒真。"""
    files = list((SERVICE / "ddp_corpus").rglob("*.py"))
    assert len(files) >= 20, f"只扫到 {len(files)} 个文件，路径可能写错了"
