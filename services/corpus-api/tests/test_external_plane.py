"""对外解析平面（`/v1/parse*`）在语料侧的那一半。

合仓前这些用例住在 `test_proxy.py`，与鉴权/限速/额度/SSE/MCP 混在一起。
那几样整体迁去了 control-api（Go），**这里只剩语料侧真正拥有的东西**：
外部任务的 Document/ParseJob 记录、归属，以及按页计量的归集口径。

等价覆盖对照（迁走的那些）：

  验 key / 撤销 / 过期        -> Go: requireAPIKey + store.APIKey.Live
  换 service token 转发        -> Go: TestProxyReplacesClientAuthorization
  逐跳头过滤                   -> 同上
  SSE 逐帧透传                 -> Go: TestProxyStreamsWithoutBuffering
  限速 429 / 额度 402          -> Go: requireAPIKey + domainThrottle
  MCP 会话头双向透传           -> Go: TestProxyPassesMCPSessionHeaderBothWays
  上游错误体形状               -> Go: proxy 原样透传状态码与响应体

**下面这五条是 1b 语料共享化引入的计量回归**，共同根因是：全局去重之后
一个 ParseJob 被多个用户共享，而原来的计量锚点（`job.page_count == 0`
= "这次任务还没记过账"）是**按任务**的 —— 任务一共享，那个锚点就从
"每人各记一次"退化成"整个部署只记一次"。
"""
import httpx
import respx
from sqlalchemy import select

from ddp_corpus.config import settings
from ddp_corpus.models import Document, DocumentUpload, ParseJob, new_id
from tests.conftest import ACTOR, ORG, SERVICE, actor_headers, usage_events

LAYOUT = {"pdf_info": [{"page_idx": i} for i in range(3)]}
RESULT = {"markdown": "# x", "layout_json": LAYOUT, "images": []}


def _api_key_actor(actor_id: str, key_id: str) -> dict:
    """对外平面的调用者：actor 是 api_key 类型，带着 key id。

    生产里这组头由入口在验完 key 之后填。
    """
    return actor_headers(actor_id, kind="api_key", api_key_id=key_id)


@respx.mock
async def test_submit_creates_external_job_and_result_meters_pages(actor_client, session):
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-9"}))
    respx.get(f"{SERVICE}/v1/parse/s-9/result").mock(
        return_value=httpx.Response(200, json=RESULT))

    headers = _api_key_actor("actor-a", "key-a")
    submit = await actor_client.post("/v1/parse", headers=headers,
                                     json={"file_url": "https://third-party.example/a.pdf"})
    assert submit.status_code == 202 and submit.json()["task_id"] == "s-9"

    document = (await session.execute(select(Document))).scalars().one()
    job = (await session.execute(select(ParseJob))).scalars().one()
    assert document.object_key == "" and document.origin == "external", "外部任务不归档，仅留记录"
    assert job.service_task_id == "s-9" and document.filename == "a.pdf"

    # 取结果：按页计量一次
    for _ in range(2):
        got = await actor_client.get("/v1/parse/s-9/result", headers=headers)
        assert got.status_code == 200 and got.json()["markdown"] == "# x"

    billed = [(u["kind"], u["pages"]) for u in await usage_events(session, "parse")]
    assert billed == [("parse", 3)], "重复取结果不得重复计费"


@respx.mock
async def test_forward_swaps_caller_credentials_for_the_service_token(actor_client, session):
    """网关只认内网凭据，绝不能看到调用方的 sk- key。"""
    route = respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-1"}))

    headers = dict(_api_key_actor("actor-a", "key-a"))
    headers["Authorization"] = "Bearer sk-a-real-user-key"
    # 入口会把服务凭据换上去；这里模拟"入口已经换过"的形态，
    # 断言的是**本服务不会把收到的 Authorization 继续往上游传**
    await actor_client.post("/v1/parse", headers=actor_headers("actor-a", kind="api_key",
                                                               api_key_id="key-a"),
                            json={"file_url": "https://third-party.example/a.pdf"})
    forwarded = route.calls.last.request.headers["authorization"]
    assert forwarded == f"Bearer {settings.service_token}"
    assert "sk-a-real-user-key" not in forwarded


@respx.mock
async def test_result_metering_with_shared_service_task(actor_client, session):
    """同一份文档既从 Web 传过、又用 key 提交过时，本层会有两个 job 指向同一个
    网关任务。取结果必须照常计量，而不是撞上"查出多行"直接 500（真机 e2e 抓到过）。"""
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-shared"}))
    respx.get(f"{SERVICE}/v1/parse/s-shared/result").mock(
        return_value=httpx.Response(200, json=RESULT))

    web_doc = Document(id=new_id(), uploaded_by="actor-a", organization_id=ORG,
                       doc_id="web-doc", origin="web", filename="a.pdf",
                       object_key="uploads/x/a.pdf", page_count=3)
    session.add(web_doc)
    await session.commit()
    session.add(ParseJob(id=new_id(), document_id=web_doc.id, engine="mineru",
                         options_hash="web", service_task_id="s-shared",
                         status="succeeded", page_count=3))
    await session.commit()

    headers = _api_key_actor("actor-a", "key-a")
    assert (await actor_client.post("/v1/parse", headers=headers, json={
        "file_url": "https://third-party.example/a.pdf"})).status_code == 202

    got = await actor_client.get("/v1/parse/s-shared/result", headers=headers)
    assert got.status_code == 200, got.text
    billed = [(u["kind"], u["pages"]) for u in await usage_events(session, "parse")]
    assert billed == [("parse", 3)], "必须记在这把 key 自己的任务上，且只记一次"


@respx.mock
async def test_second_user_of_a_shared_parse_is_still_billed(actor_client, session):
    """**第二个用户不能白嫖。**

    全局去重让 B 提交一个 A 已经解析过的 file_url 时命中同一个 job，
    而那个 job 的 `page_count` 早就非 0 —— 于是 B **完全不计费**，
    可以无限白嫖解析并绕过配额（验收实测 B used_pages=0）。
    按 (用户, job) 判重之后**恰好还原去重之前的计费行为**。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-share"}))
    respx.get(f"{SERVICE}/v1/parse/s-share/result").mock(
        return_value=httpx.Response(200, json=RESULT))
    url = {"file_url": "https://third-party.example/shared.pdf"}

    first = _api_key_actor("actor-first", "key-first")
    await actor_client.post("/v1/parse", headers=first, json=url)
    await actor_client.get("/v1/parse/s-share/result", headers=first)

    other = _api_key_actor("actor-freeloader", "key-freeloader")
    await actor_client.post("/v1/parse", headers=other, json=url)
    await actor_client.get("/v1/parse/s-share/result", headers=other)

    billed = {u["actor_id"]: u["pages"] for u in await usage_events(session, "parse")}
    assert billed.get("actor-freeloader") == 3, f"第二个用户没被计费：{billed}"
    # 而且各自只记一次 —— 重复取结果仍然不得重复计费
    assert len(billed) == 2, billed


@respx.mock
async def test_polling_someone_elses_task_does_not_bill_the_wrong_person(actor_client, session):
    """**跨用户取结果不能把账记到错的人头上。**

    去掉按用户分行之后，简单的 `rows[0]` 兜底会选中**别人**的 job，
    而用量写的是当前调用者 —— A 的解析算在 B 头上，A 永远不被计费
    （验收实测 A=0 / B=3）。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-cross"}))
    respx.get(f"{SERVICE}/v1/parse/s-cross/result").mock(
        return_value=httpx.Response(200, json=RESULT))

    # A 提交，但**不取结果**
    await actor_client.post("/v1/parse", headers=_api_key_actor("actor-a", "key-a"),
                            json={"file_url": "https://third-party.example/cross.pdf"})

    # B 从没提交过，直接拿着同一个 task_id 取结果
    await actor_client.get("/v1/parse/s-cross/result",
                           headers=_api_key_actor("actor-eavesdropper", "key-b"))

    billed = [(u["actor_id"], u["pages"]) for u in await usage_events(session, "parse")]
    assert billed == [], f"没有人该被计费（B 没有自己的 job），实际 {billed}"


@respx.mock
async def test_external_submitter_gets_ownership_recorded(actor_client, session):
    """**对外平面提交者要能删掉自己提交的文档。**

    `_may_delete` 只查 `document_uploads`，而对外平面建 Document 时曾经漏了
    记归属 —— 于是提交者删自己的东西得到 403，永久。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-own"}))
    await actor_client.post("/v1/parse", headers=_api_key_actor("actor-owner", "key-o"),
                            json={"file_url": "https://third-party.example/mine.pdf"})

    document = (await session.execute(select(Document))).scalars().one()
    uploads = (await session.execute(
        select(DocumentUpload).where(DocumentUpload.document_id == document.id))
    ).scalars().all()
    assert [u.user_id for u in uploads] == ["actor-owner"], \
        "对外平面提交也要记归属，否则提交者自己删不掉"


@respx.mock
async def test_middle_submitter_of_a_three_way_share_is_still_billed(actor_client, session):
    """**三人以上共享同一份外部文档时，中间那些人也要计费。**

    `job.api_key_id` 每次提交都被覆盖成**最后一个**提交者，而 `initiated_by`
    只在建 job 时写入 = **第一个**发起人。三个人依次提交同一个 URL 时，
    中间那位两层兜底全对不上 —— 一分钱不记，且绕过配额。人一多，
    中间的用户全部免费。

    第三层兜底查 `document_uploads`（"他提交过吗"）补上这个洞，
    而没提交过的人不在那张表里，所以不会重新打开"拿别人的 task_id 白嫖"那个口子
    —— 那条由 `test_polling_someone_elses_task_does_not_bill_the_wrong_person` 钉着。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-three"}))
    respx.get(f"{SERVICE}/v1/parse/s-three/result").mock(
        return_value=httpx.Response(200, json=RESULT))
    url = {"file_url": "https://third-party.example/three.pdf"}

    first = _api_key_actor("actor-first", "key-1")
    middle = _api_key_actor("actor-middle", "key-2")
    last = _api_key_actor("actor-last", "key-3")

    for who in (first, middle, last):
        await actor_client.post("/v1/parse", headers=who, json=url)

    # 中间那位取结果 —— 他既不是 initiated_by 也不是最后的 api_key_id
    await actor_client.get("/v1/parse/s-three/result", headers=middle)

    billed = {u["actor_id"]: u["pages"] for u in await usage_events(session, "parse")}
    assert billed.get("actor-middle") == 3, f"中间的提交者没被计费：{billed}"


@respx.mock
async def test_status_query_is_a_plain_relay(actor_client):
    """状态查询不做任何记账，原样透传。"""
    route = respx.get(f"{SERVICE}/v1/parse/s-x").mock(
        return_value=httpx.Response(200, json={"task_id": "s-x", "status": "running",
                                               "progress": 0.5, "error": None}))
    resp = await actor_client.get("/v1/parse/s-x",
                                  headers=_api_key_actor(ACTOR, "key-a"))
    assert resp.status_code == 200 and resp.json()["status"] == "running"
    assert route.called
