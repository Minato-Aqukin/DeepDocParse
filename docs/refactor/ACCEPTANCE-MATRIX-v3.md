# v3 端到端验收矩阵台账（T01–T88）

> 对照工作区计划 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md` §13。
> 2026-09-15 建立，基线 main `356f10e` 之后的 `feat/conflict-axis-and-acceptance-ledger`。
> **这是台账，不是通过报告**：每行只记"现在有什么证据、还缺什么"。

## 怎么读

| 状态 | 含义 |
|---|---|
| ✅ 已验证 | 有针对这条判据的测试，在 CI 或仓库门禁里真跑；判据里的每个动作都被覆盖 |
| 🟡 部分 | 协议/单测层成立，但判据里有一部分没被验证（常见：界面未接、没有真实多主机/真进程重启、只在合成夹具上） |
| 🔴 未验证 | 没有实现，或实现了但没有任何测试对着这条判据 |
| ⛔ 需外部条件 | 判据本身要求本机没有的东西（NVIDIA GPU、多台主机、真实用户机/平台） |

**证据口径**（计划 §13 [R0]）：

- 列出的是**测试名**（`文件::用例`）或**验证记录**（`docs/refactor/*.md`、`artifacts/`）。
  没有测试名只有文档的，状态最多 🟡 —— 文档是一次性的，门禁不会替它再跑。
- **mock 不证明真实能力**：解析/模型能力只认真实运行记录；协议测试可以用替身，
  但替身必须按真实端点的拒绝规则写（F-34 教训）。
- **计划要求每条用例保存输入、期望、实测输出、平台/模型版本和脱敏 trace**。
  本台账只做到"指向证据"，没有逐条归档运行产物 —— 这是台账自身的已知缺口。
- "界面未接"指 Web/App 界面没有对应入口（见改进项 ①）；API 层成立时标 🟡 并写明。

## 汇总

<!-- counts:begin -->
| 状态 | 条数 |
|---|---|
| ✅ 已验证 | 39 |
| 🟡 部分 | 45 |
| 🔴 未验证 | 4 |
| ⛔ 需外部条件 | 0 |
| **合计** | **88** |
<!-- counts:end -->

⛔ 为 0 不代表没有外部依赖：需要 GPU / 多主机的部分已经并进对应行的 🟡 缺口里写明，
因为那些判据在本机都还有能验的一半。

**🔴 的四条**：T25（App 调中心真实解析）、T47（人工标注的主张支持度）、T48（来源离线与
许可快照）、T52（跨 A/B 的 Wiki）。只有设计保证、没有专门测试的（如 T37）记 🟡，不记 ✅。

## P1 资源、权限与出处

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T01 两用户上传相同字节 | P1 | ✅ | `corpus-api/tests/test_resource_acl.py::test_t01_t02_real_upload_has_independent_assets_and_retry_dedup`；`test_resource_layer.py::test_same_bytes_two_users_get_independent_resources` | — |
| T02 同上传/提交重试 | P1 | ✅ | 同上 `test_t01_t02_…`；`test_federation_tasks.py::test_task_intent_idempotency_replays_and_conflicts`；Go `upload_pg_s3_test.go::TestUploadRealLostCreateReceiptResumesWithoutReallocating`；显式重新上传被接受且各自解析 `test_documents.py::test_duplicate_upload_reuses_content_with_independent_parse_attempts` | — |
| T03 删除一人的重复内容 | P1 | ✅ | `test_resource_acl.py::test_t03_delete_one_resource_keeps_other_content_and_owner`；`test_bundles.py::test_t03_running_task_retains_deleted_bundle_until_terminal` / `test_t03_durable_citation_protects_unique_original` / `test_t03_wiki_revision_references_protect_unique_source_without_old_citation`；真 PG 竞态见 `BUNDLE-GC-VALIDATION-v3.md` | — |
| T04 同文件名不同内容 | P1 | ✅ | `test_resource_acl.py::test_t04_same_filename_new_content_creates_fixed_version`；`test_resource_layer.py::test_a_fixed_version_is_a_new_row_not_an_edit` | — |
| T05 私有资源多入口读取 | P1 | ✅ | `test_resource_acl.py::test_t05_private_multi_entry_read_denied_before_storage_or_upstream` / `test_private_evidence_and_unknown_evidence_are_indistinguishable`；`test_federation_error_desensitization.py::test_resource_and_evidence_ids_have_no_cross_org_existence_oracle`；Go `resource_authorization_test.go::TestFileAuthorizationRechecksACLAndNeverFollowsRedirect` | — |
| T06 私有资料进入 RAG/Wiki | P1 | ✅ | `test_resource_acl.py::test_t06_search_scope_applies_before_candidate_limit` / `test_same_content_private_parse_outputs_never_enter_other_asset_models` / `test_extraction_rechecks_permission_before_each_model_dispatch` / `test_t06_private_copy_cannot_publish_derived_content`；Wiki 侧 `test_wiki_revisions.py::test_private_context_cannot_launder_via_public_citation` | — |
| T07 MCP/历史/统计/缓存 | P1 | ✅ | `test_resource_acl.py::test_t07_private_listing_statistics_and_history_do_not_disclose`；`test_static_guards.py::test_mcp_uses_actor_authorized_corpus_http_without_database_credentials`；根守卫 `test_mcp_cannot_import_the_corpus_database_layer`；`test_federation_cache_wiring.py::test_withdrawn_collection_is_never_reused`；跨组织 `test_resource_acl.py::test_published_resources_never_cross_the_organization_boundary`（F-34 #8） | — |
| T08 历史 doc_id 与旧证据 | P1 | ✅ | `test_resource_acl.py::test_t08_ambiguous_legacy_context_is_never_first_row` / `test_unbound_legacy_history_cannot_adopt_a_later_public_asset`；`test_resource_migrations.py::test_first_uploader_without_historical_organization_is_quarantined` | — |
| T09 本站与互联公共目录 | P1/P5 | 🟡 | `test_resource_acl.py::test_t09_catalog_explicit_local_scope_and_no_private_or_temporary` / `test_t09_temporary_resource_cannot_be_published`；互联目录 API 见 T71–T74 | Web 没有互联公开目录页，"来源节点可见"只在 API 层 |
| T45 引用点击与原文高亮 | P1/P5 | 🟡 | `ddp_core/tests/test_shared_cpu.py::test_cropbox_translation_rotation_and_visible_intersection`；gateway `test_layout.py::test_borndigital_handles_page_rotation` / `test_borndigital_bbox_is_top_left_origin`；Web e2e `environment-workspace.spec.ts`「固定证据打开真实 PDF 预览，生成文本不能触发外部资源请求」 | 印刷页码 `printed_page_label` 只在契约里，没有生产者 |
| T46 生成器编造 evidence_ref | P1/P5 | ✅ | `test_federation_answer.py::test_fabricated_or_missing_citations_reject_the_answer`；`test_federation_answer_delegation.py::test_fabricated_binding_ids_reject_the_answer_but_keep_evidence`；`ddp_core/tests/test_bundle.py::test_t12_forged_evidence_rejected_even_with_valid_file_digests` | — |

## P2 共享内核与本地运行

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T11 Bundle 本地/中心往返 | P2 | ✅ | `ddp_core/tests/test_bundle.py::test_t11_portable_bundle_roundtrip` / `test_t11_original_missing_is_explicit_and_does_not_invent_bytes`；`test_bundles.py::test_t11_import_export_keeps_source_version_evidence_and_private_local_owner`；`ddp_local/tests/test_runtime.py::test_source_only_bundle_keeps_missing_revision_and_can_roundtrip` | — |
| T12 恶意 Bundle/路径/压缩包 | P2 | ✅ | `test_bundle.py::test_t12_*`（路径穿越、符号链接、重复成员、压缩炸弹、未知 schema、摘要、伪证据、定位越界）；`ddp_local/tests/test_runtime.py::test_malicious_bundle_never_publishes`；`test_bundles.py::test_t12_invalid_archive_never_publishes_or_writes_storage` | — |
| T13 伪造已有私有文件哈希 | P1/P2 | ✅ | `test_bundles.py::test_t13_missing_original_cannot_claim_known_private_content`（只凭摘要拿不到文件、不改归属；**已知摘要与从没见过的摘要响应同形**，不泄露存在性） | — |
| T14 解析时输入被修改 | P2/P3 | 🟡 | `ddp_local/tests/test_runtime.py::test_snapshots_idempotency_and_changed_source`；`ddp_local/tests/test_consents.py::test_input_manifest_cannot_self_assert_content_existence`；中心上传全量摘要 Go `TestUploadRealExpiredPresignResumeCompleteLossAndFullDigest` | App 把本地输入交给中心计算的路径没接（见 T25） |
| T15 真实本地 CPU 解析 | P2 | 🟡 | `ddp_local/tests/test_runtime.py::test_real_cpu_parse_search_evidence_bundle_restart` / `test_real_chinese_pdf_keyword` / `test_real_cpu_code_identifier_uses_common_compiler`；gateway `test_layout.py::test_borndigital_*`；`P2-LOCAL-VALIDATION-v3.md`（24 页 code-corpus 192 条出处逐项一致） | **"保留引擎与质量记录"没有被断言**：被引用用例只验解析结果与出处，不验引擎名与质量记录，本地运行时也不产出质量记录。声明支持范围是有文字层的 PDF；扫描件/OCR 需 GPU |
| T16 真实本地检索/生成/Wiki | P2/P3 | 🟡 | 真实 Qwen3-1.7B（llama.cpp CPU、断网 namespace）：`P3-LOCAL-MODEL-VALIDATION-v3.md`、`P2-LOCAL-WIKI-VALIDATION-v3.md` 与 `artifacts/local-wiki-real-v5-final.json` | 真实模型运行是手工脚本（`eval_wiki.py`），不进 CI；本地检索只有关键词（无 embedding） |
| T51 纯本地 Wiki 构建 | P2/P3 | 🟡 | `P2-LOCAL-WIKI-VALIDATION-v3.md`：两页六句、两条模型选择的原文关系、人工编辑 CAS、重启恢复，全部来自真实模型；`ddp_local/tests/test_wiki.py::test_persistent_pages_relations_attempts_and_restart_replay` / `test_concurrent_edit_cas_preserves_history_and_human_provenance` | CI 用例用替身 provider，真实模型那一半只有手工记录（同 T16：mock 不证明真实能力） |

## P3 Electron 工作台与本地/中心双执行

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T17 严格本地模式外发 | P3 | 🟡 | `test_federation_two_node.py::test_local_only_exploration_gate_sends_zero_traffic`；`ddp_local/tests/test_consents.py::test_workspace_local_only_policy_cannot_be_overridden_by_dispatch_flag`；`ddp_local/tests/test_runtime.py::test_import_has_no_server_database_or_network_side_effects`；真实模型在无网 namespace 运行（T16 记录） | 没有 App 整体（Electron + 运行时）的操作系统层网络审计 |
| T18 本地模型缺失 | P3 | 🟡 | `ddp_local/tests/test_model_process.py::test_runtime_compatibility_and_missing_model_fail_before_process_launch`（`model_not_installed`，进程不启动）；`ddp_local/tests/test_runtime.py::test_provider_protocol_failure_and_oom_are_explicit`；`test_federation_answer.py::test_generation_not_ready_at_plan_time_keeps_old_behavior`；Web e2e「模型页仅查询状态，显式下载…」 | App 侧"模型缺失时不悄悄请求远端"没有专门用例（现在靠没有这条回退路径，不是靠测试钉住） |
| T19 GPU OOM/后端不支持 | P3 | 🟡 | CPU 真实 OOM 与恢复：`ddp_local/tests/test_model_process.py::test_owned_runtime_oom_is_visible_and_explicit_restart_can_recover`；`P3-LOCAL-MODEL-VALIDATION-v3.md` | 真实 GPU OOM / GPU profile 需 NVIDIA 机器 |
| T20 普通用户安装 App | P3 | 🟡 | 可复现 Arch 包与目录包（`RELEASE-MANUAL-v3.md` §0）；`apps/desktop/VALIDATION.md`（Wayland 真实 GUI 冒烟、包外 CPU 链）；`tests/test_desktop_release.py` | 没有干净机器上的真实用户安装；Windows 安装向导/卸载未验 |
| T21 中心凭证存储与日志 | P3 | ✅ | 错误与日志：`ddp_local/tests/test_federation_client.py::test_credential_never_leaks_into_errors_or_repr`、`test_federation_peer_client.py::test_credentials_never_leak_into_errors_or_followed_redirects`、日志脱敏门禁 `scripts/check_log_redaction.py --with-self-test`；普通配置：`apps/desktop/test/boundaries.test.mjs`「basic_text and unavailable backends are session-only and never encrypt or persist a secret」；网页存储：Web e2e「系统密钥库不可用时中心配对只用会话凭证，秘密不进入工作区草稿」（断言 localStorage / sessionStorage 与工作区草稿里都没有凭证） | — |
| T22 远端页面与本地 IPC | P3 | ✅ | `apps/desktop/test/boundaries.test.mjs`「only the trusted main frame can call a fixed, schema-validated IPC operation」「packaged static protocol does not expose files outside bundled UI」；`client-host.test.mjs`「fixed client schema rejects scope/path/URL injection…」；Web e2e「固定证据打开真实 PDF 预览，生成文本不能触发外部资源请求」 | — |
| T24 中心 API 能力握手 | P3 | ✅ | `packages/client-runtime/test/http.test.mjs`「a chat-only service cannot impersonate the complete corpus API」「an old service missing a required client capability is rejected before snapshot」；Go `TestCenterClientRealCorpusAuthAndScope`（`P3-CENTER-CLIENT-VALIDATION-v3.md`） | — |
| T25 App 调用中心真实解析/任务 | P3 | 🔴 | 只有失败即关闭的边界：`http.test.mjs`「remote command dispatch requires an approved plan…」 | 批准计划后的输入外发、远端解析、产物回传没实现（改进项 ①） |
| T26 大文件与输出传输中断 | P3 | 🟡 | 中心直传续传与全量摘要 Go `TestUploadRealExpiredPresignResumeCompleteLossAndFullDigest`；模型下载续传 `ddp_local/tests/test_model_install.py::test_explicit_download_resumes_partial_and_validates_range` | App↔中心的输出传输中断/续传没有路径（同 T25） |
| T27 关闭 Web/重启中心入口 | P3 | 🟡 | 受理与队列同事务、worker 推进：`test_federation_queue.py::test_submit_returns_202_and_worker_advances_the_task` / `test_admit_persists_execution_and_queue_task_in_one_transaction`；真 PG `test_federation_pg.py::test_coordinator_end_to_end_on_pg_queue_path`；清扫 `test_sweeper_on_pg_fails_expired_lease_and_stalled_request` | 没有"杀掉入口进程再查询"的真进程重启演练 |
| T28 关闭 App/本地休眠 | P3 | 🟡 | `apps/desktop/test/runtime.test.mjs`「real owned runtime starts once, retains workspace identity across suspend…」 | 没有真实操作系统休眠（冒烟走同一生命周期路径） |
| T29 下载后回执丢失/重复 | P3 | 🟡 | `ddp_local/tests/test_federation_dispatch.py::test_tampered_delivery_result_is_refused_with_explicit_reason` / `test_ack_expired_response_never_marks_local_confirmed` / `test_unverified_delivery_cannot_be_confirmed`；`test_federation_tasks.py::test_ack_wrong_digest_and_expired_delivery_cannot_confirm` / `test_ack_is_owner_scoped_like_delivery_read`；同键重复确认回同一回执 `test_federation_tasks.py::test_local_intent_plan_approve_execute_coverage_events_and_ack` | 负向路径齐全；**"回执丢失后换键重复确认"与"确认前不提前清理"没有正向用例** |
| T30 临时输入/派生数据清理 | P3/P7 | 🟡 | 引用安全 GC：`test_bundles.py::test_gc_partial_failure_keeps_durable_remaining_keys_and_retries`；`test_ops.py::test_gc_*` | 远端计算的临时区/派生数据清理策略没有实现（没有远端计算输入路径） |
| T31 Worker 崩溃/旧租约继续写 | P3/P7 | ✅ | `test_federation_concurrency_pg.py::test_cancel_between_claim_and_succeed_fences_the_stale_writer` / `test_expired_lease_reclaim_reruns_and_terminal_rows_are_never_touched`；`corpus-worker/tests/test_queue.py::test_stale_worker_cannot_overwrite_a_newer_result`；`ddp_local/tests/test_runtime.py::test_expired_lease_restart_and_generation_fence` | — |
| T56 本地资料/私有问题转委托 | P3/P5 | ✅ | `ddp_core/tests/test_plans.py::test_center_only_does_not_authorize_a_third_executor` / `test_b_data_c_generation_denied_even_through_local_reexport`；`ddp_local/tests/test_consents.py::test_dispatch_rechecks_exact_approved_boundary` | — |
| T65 多环境与多身份连接 | P3 | 🟡 | `packages/client-runtime/test/runtime.test.mjs`「profile cache binding persists across registry replacement」「identity is checked before any credential leaves the secret broker」；`sqlite.test.mjs`「drafts have independent profile keys and a CAS revision…」；Web e2e「桌面根路径进入业务工作台，身份切换恢复各自草稿且旧订阅不能污染当前状态」 | 缓存/草稿按环境与身份隔离成立；**"同名路径不替代远端工作区""删除连接不误删用户资产"没有被引用证据覆盖** |
| T66 多面板竞争重连 | P3 | ✅ | `packages/client-runtime/test/runtime.test.mjs`「network retry has one finite owner」「authentication failure is bounded and acquiring another panel does not retry it」「several panels share one connection; one bad observer cannot disconnect all panels」；连接问题不阻止本地输入：Web e2e「提交回执不明时保留操作键，断线恢复不重复生成，输入仍可编辑」 | — |
| T67 旧连接事件与游标 | P3 | ✅ | `packages/client-runtime/test/runtime.test.mjs`「late snapshot from a disposed generation cannot replace newer state」「projection and cursor commit together…」「expired log cursor starts a new snapshot…」 | — |
| T68 连接恢复与写命令 | P3 | ✅ | `packages/client-runtime/test/runtime.test.mjs`「intent precedes command; uncertain writes never replay on wake or execute; receipt repairs」；`sqlite.test.mjs`「a crash between dispatch and receipt never turns the recorded intent into a fresh command」 | — |
| T69 Linux 凭证库不可用 | P3/P7 | ✅ | `apps/desktop/test/boundaries.test.mjs`「basic_text and unavailable backends are session-only and never encrypt or persist a secret」 | （判据外）GNOME Keyring / KWallet 的持久化正例未验 —— 判据只要求拒绝不合格回退 |

## P4 节点发现、范围枚举与精确定位

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T10 离线节点及分页目录 | P5/P6 | 🟡 | 有界窗口 `test_client_projection.py::test_window_counts_fixed_pages_and_acl_revocation` / `test_metadata_above_four_mib_remains_explicit_bounded_windows`；范围不完整 Go `TestScopeHTTPPartialForInvalidEnumerationAndBudget` | 界面上没有离线节点与水位显示（改进项 ①） |
| T70 节点注册与能力声明不一致 | P4 | ✅ | `corpus-api/tests/test_capabilities.py::test_accepting_admissions_is_truthful`；Go `internal/api/discovery_pg_test.go::TestDiscoveryHTTPProducerNoRedirectAndUnknownWhenUnavailable`；gateway `model-gateway/tests/test_capabilities.py::test_non_2xx_health_is_not_ready` | — |
| T71 成员完整枚举和重复路径 | P4 | ✅ | Go `TestScopePGFrozenDeduplicatedPagesIsolationAndDurableRevocation`、`expand_test.go::TestExpandScopeStopsCyclesAndDuplicatePaths`；`test_collection_catalog.py::test_node_published_snapshot_stable_pages_total_and_limit` | — |
| T72 下级目录不可展开 | P4/P6 | ✅ | Go `TestScopePGDirectMembersAreUnknownNotInventedCollections`、`expand_test.go::TestExpandScopeNonEnumerableDirectMemberStillPublishesCollections`；`ddp_core/tests/test_coverage.py::test_manifest_allof_sealed_cannot_keep_unexpanded_subtrees` | — |
| T73 枚举游标过期或目录中途更新 | P4/P6 | ✅ | Go `TestScopeHTTPPartialForInvalidEnumerationAndBudget`（修订变化、缺终止页、提前 complete、环、重定向、预算）；目录快照过期显式 `catalog_snapshot_expired`、重新取新快照 `test_collection_catalog.py::test_fixed_pages_caller_scope_and_new_arrivals` | — |
| T74 范围封存后节点加入/撤销 | P4/P6 | ✅ | Go `scope_integration_test.go::TestScopeRealCorpusPublishedCatalogAndWithdrawal`（真实 corpus 目录）、`TestDiscoverySnapshotsFreezeVisibleMembershipAndRetainRevocations` | — |
| T75 成员快照与全文快照混淆 | P5/P6 | ✅ | `P4-SCOPE-VALIDATION-v3.md`（封套与生产者都显式拒绝全文快照保证）；`test_federation_cache_wiring.py::test_reuse_is_rejected_when_the_index_revision_moved`；`ddp_core/tests/test_coverage.py::test_record_succeeded_requires_receipt_and_actual_index_revision` | — |

## P5 Probe、执行图、接单与联邦业务

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T32 跨中心同名用户/节点凭证 | P5 | 🟡 | `test_resource_acl.py::test_t32_same_subject_other_issuer_and_node_creds_not_owner`；`test_federation_two_node.py::test_wrong_peer_token_fails_closed_without_fabrication`；`http.test.mjs`「copied public node metadata without the private signing key never releases a credential」 | 节点间仍是共享 peer token，没有逐节点密钥与受限委托（改进项 ③） |
| T33 A 无证据、B 有证据 | P5 | 🟡 | `test_federation_two_node.py::test_b_only_evidence_over_real_peer_http`；`test_federation_answer.py::test_two_node_remote_excerpt_reaches_real_prompt`（B 为真实子进程节点） | Web 已能在 A 的任务页展示 B 的证据与答案（Web e2e「执行中的任务轮询到落定：矛盾与未查全在答案之前，引用指回证据，本节点证据可开原文」），但**从网页发起任务**还没接（改进项 ① a2-2）；生成用替身模型 |
| T34 必要证据分散于 A/B | P5 | 🟡 | `test_federation_two_node.py::test_split_evidence_keeps_origins_and_identity` | 被引用用例的 A/B 是**相同字节**，测的是去重与归属（T44 的场景）；没有"答案必须同时用到两侧证据"的用例，也没有答案层断言"不能只选一个答案字符串" |
| T35 本地相似但不足的干扰资料 | P5/P6 | 🟡 | `eval/tests/test_routing_eval.py`（`local-similar-decoy`：穷查 100%、fast 33%，合成夹具） | fast 没有"缺子问题且有预算就继续下一批"的扩展逻辑（§7.2）；没有真实语料 |
| T36 最近节点缺资料/能力 | P5/P6 | 🟡 | `test_federation_answer_delegation.py::test_not_ready_node_never_gets_an_answer_step`；评测 `nearest-node-decoy`（合成） | 距离/可达性排序本身没有实现，只证明了"能力是硬约束" |
| T37 节点使用不同 embedding | P5 | 🟡 | 设计：各节点在本域检索，融合是证据并集，`_score` 不出 HTTP（`federation_tasks._public_item`） | 没有两节点不同模型的测试 |
| T38 各允许节点均无支持证据 | P5 | ✅ | `test_federation_answer.py::test_insufficient_evidence_never_generates_and_keeps_bindings_empty`；`test_federation_tasks.py::test_no_evidence_at_all_stays_failed_with_truthful_reason`；评测 `no-evidence-in-scope` | — |
| T39 部分节点超时或失败 | P5 | ✅ | API：`test_federation_tasks.py::test_remote_poll_timeout_stays_retryable_on_resume`、`test_federation_probes.py::test_truncated_candidates_report_partial`、双节点`test_federation_two_node.py::test_unreachable_registered_peer_stays_in_denominator`；界面：Web e2e「执行中的任务轮询到落定：矛盾与未查全在答案之前，引用指回证据，本节点证据可开原文」（partial 与未取回目标、覆盖账本里的 unreachable 目标都在答案之前可见）、`apps/web/src/components/federation/__tests__/TaskResultPanel.spec.ts`「只查了部分范围时列出没取回证据的目标与原因」 | — |
| T43 错 audience、越权委托、恶意端点 | P5/P7 | 🟡 | `test_federation_ssrf.py::test_redirect_to_another_listener_is_not_followed_and_never_sees_the_token` / `test_dns_swapped_response_identity_is_never_treated_as_the_approved_peer`；Go `peer_test.go::TestPeerClientNeverFollowsRedirectOrForwardsCredentials` | 限定 audience 的委托凭证不存在（改进项 ③） |
| T44 同资料多上传/多副本/多路径 | P5/P6 | 🟡 | `test_federation_two_node.py::test_split_evidence_keeps_origins_and_identity`（归属各自保留）；Go `TestExpandScopeStopsCyclesAndDuplicatePaths`；`test_documents.py::test_duplicate_upload_reuses_content_with_independent_parse_attempts` | **"不制造多个独立证据共识"没有断言**：两份相同副本仍算两条证据，没有支持度去重检查 |
| T47 真实主张支持与矛盾来源 | P5/P7 | 🔴 | 矛盾轴有规则与生成标注两路（见 T86），全部 `needs_review` | 没有人工标注的主张支持度评测（§14.3），引用存在率不能替代 |
| T48 来源离线与已许可快照 | P5 | 🔴 | 不可达目标如实 partial（T39） | 没有"已许可快照"概念与原文预览降级测试 |
| T50 入口 A 没有生成模型 | P5 | 🟡 | `test_federation_answer_delegation.py::test_delegated_answer_keeps_real_evidence_bindings`；`test_federation_two_node.py::test_remote_answer_delegation_over_real_http` | "在 A 展示"只在 API；生成是替身模型，真实委托生成需 GPU |
| T52 跨 A/B 的 Wiki 构建 | P5 | 🔴 | — | 联邦 `wiki_pages` 操作未实现：执行者只接 retrieve/answer（改进项 ②） |
| T76 Probe 前没有探索外发许可 | P5 | ✅ | `ddp_local/tests/test_federation_dispatch.py::test_dispatch_without_approved_consent_sends_nothing`；`test_federation_tasks.py::test_missing_or_expired_consent_is_egress_denied_without_probes`；`test_federation_two_node.py::test_local_only_exploration_gate_sends_zero_traffic` | — |
| T77 节点自称擅长但没有真实证据 | P5 | ✅ | `test_federation_probes.py::test_capability_probe_for_generation_is_unknown_without_model_observation`；`test_federation_answer.py::test_model_name_alone_is_not_readiness`；`test_federation_answer_delegation.py::test_executor_readiness_is_rechecked_at_admission`；`ddp_core/tests/test_probe.py::test_can_generate_is_boolean_and_no_can_solve_field` | — |
| T78 文件元数据预检与内容检查 | P5 | 🟡 | `test_federation_admissions.py::test_admission_waiting_input_when_content_cannot_be_verified`；`ddp_core/tests/test_admission.py::test_waiting_input_must_not_carry_a_verified_input_digest` | "上传阶段不占 GPU"在本机只能验协议，没有资源占用断言 |
| T79 Offer/Plan 过期或权限变化 | P5 | ✅ | admission 自己重查许可：`test_federation_admissions.py::test_admission_rechecks_stale_or_widened_consent_and_writes_nothing`（过期 / 换计划修订 / 换接收方都拒绝、不留受理行；删掉执行者的过期检查即红）；协调者侧 `test_federation_tasks.py::test_missing_or_expired_consent_is_egress_denied_without_probes`；本地 `ddp_local/tests/test_consents.py::test_expiry_revocation_and_retry_never_reuse_stale_grant` / `test_source_policy_revocation_is_rechecked_on_dispatch` | — |
| T80 同一幂等键对应不同正文 | P5 | 🟡 | `test_federation_admissions.py::test_admission_same_key_different_digest_is_conflict`；`test_federation_tasks.py::test_task_intent_idempotency_replays_and_conflicts` / `test_execute_replay_and_conflicts`；真 PG `test_admission_same_key_race_one_row_replay_and_conflict` | 同键异体回冲突成立；**"不同用户的键域不能串用"没有用例**（被引用的都是同一用户） |
| T81 接单成功但回执丢失 | P5 | 🟡 | `test_federation_tasks.py::test_unknown_admission_outcome_reconciles_without_second_execution` / `test_local_unknown_admission_outcome_reconciles_without_second_execution`；`test_federation_queue.py::test_resume_reconciles_target_admitted_before_the_crash` | 按业务键对账、不增加执行代次成立；**"不重复扣成功交付计量"没有联邦计量用例** |
| T82 已接单任务暂时失联 | P5/P6 | ✅ | `test_federation_tasks.py::test_remote_poll_timeout_stays_retryable_on_resume` / `test_resume_after_crash_before_ledger_fills_fast_denominator`；generation fence（T31） | — |
| T83 资料只在 B、生成只在 C | P5 | 🟡 | `ddp_core/tests/test_plans.py::test_b_data_c_generation_denied_even_through_local_reexport`；委托答案的类型化数据边 `test_federation_answer_delegation.py::test_consent_without_answer_edge_is_egress_denied_without_bytes` | "B 出数据、C 生成"的正向计划只有内核用例；协调者层的委托答案用例里 B 与 C 是同一节点；三台真实主机未跑 |
| T84 唯一关键证据不在主题摘要中 | P5 | ✅ | 评测 `summary-hidden`：`eval/tests/test_routing_eval.py::test_summary_hidden_questions_sit_outside_kernel_summary_ranking` 与穷查全找到 `eval/tests/test_routing_eval.py::test_exhaustive_finds_every_annotated_required_evidence`；快速模式永不 complete：`ddp_core/tests/test_coverage.py::test_completeness_fast_never_returns_complete`；快速模式未选中的目标如实记 `not_attempted / search_mode_fast` 进分母：`test_federation_tasks.py::test_resume_after_crash_before_ledger_fills_fast_denominator` | （判据外）评测数字来自合成夹具 |
| T85 单节点多集合/内部分片失败 | P5 | ✅ | `ddp_core/tests/test_compatibility.py::test_probe_internal_limits_can_never_be_washed_into_success`；`test_coverage.py::test_record_internal_limits_force_partial_even_with_explicit_success` / `test_record_execution_limits_force_partial_without_a_probe` / `test_completeness_is_a_conjunction_over_deduplicated_targets`（查一个集合不能标整节点完成） | — |
| T86 全部目标无命中或证据矛盾 | P5 | ✅ | 不足：`test_insufficient_evidence_never_generates_and_keeps_bindings_empty`；矛盾：`test_federation_tasks.py::test_version_divergence_of_one_source_marks_the_ledger_conflicting` / `test_same_text_across_versions_is_not_a_conflict` / `test_resume_keeps_the_divergence_found_by_an_earlier_round` / `test_versions_split_across_resume_rounds_are_still_compared`（两版分两轮到达照样比较）、`test_federation_answer.py::test_generation_reported_conflict_marks_ledger_and_is_lifted_from_the_answer` / `test_unverifiable_conflict_markup_rejects_the_answer`、`test_federation_answer_delegation.py::test_remote_conflicts_outside_the_sent_evidence_reject_the_answer` / `test_remote_conflicts_are_accepted_as_generation_reported_only`；**不足与矛盾并存时报不足、生成闸照拦**：`test_federation_answer_delegation.py::test_version_divergence_never_hides_insufficient_or_opens_the_generation_gate`、`ddp_core/tests/test_coverage.py::test_sufficiency_uses_rules_not_confidence` | （判据外）跨资料的语义矛盾依赖生成标注，恒 `needs_review`（不裁决）；版本分歧是坐标比较，块序平移时可能误报或漏报 |

## P6 递归联邦、路由优化与有界缓存

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T40 路由环路和重复路径 | P6 | 🟡 | Go `expand_test.go::TestExpandScopeStopsCyclesAndDuplicatePaths`、`TestScopeCreateToleratesMutualMemberDirectoriesAndSeals` | 只到目录展开层；任务不经上级转发，任务层环路无从测起 |
| T41 多级 fanout 预算 | P6 | 🟡 | `ddp_core/tests/test_routing.py::test_root_budget_never_resets_or_backfills` / `test_root_budget_shared_caps_and_sub_caps`；Go `TestScopeCreateRemoteBudgetStopsRecursionHonestly` | 下级按预算份额委托没有实现 |
| T42 路由摘要过期/撤销/乱序 | P6 | ✅ | `test_collection_catalog.py::test_withdrawn_ancestor_invalidates_reads_and_terminal_proof`；`test_cache.py::test_probe_reuse_rejects_expired_digest_revision_and_org`；Go `TestDiscoveryStateRevisionRollbackAndEmptySnapshot`；目录快照过期后显式 `catalog_snapshot_expired` 并可取新快照 `test_collection_catalog.py::test_fixed_pages_caller_scope_and_new_arrivals` | — |
| T49 资源撤销影响答案/Wiki/缓存 | P6 | ✅ | `test_wiki_cache_invalidation.py::test_tombstoned_source_unpublishes_wiki_and_purges_cache`；`test_federation_error_desensitization.py::test_withdrawn_source_has_no_oracle_for_peers`；`test_wiki_revisions.py::test_revoke_between_model_calls_stops_next_request`；`test_federation_cache_wiring.py::test_withdrawn_collection_is_never_reused` | — |
| T53 来源更新与人工编辑 | P6 | 🟡 | 中心 `test_wiki_revisions.py::test_fixed_manifest_human_edits_cas_rebuild_and_retry` / `test_exact_parse_binding_stale_and_missing_binding`；本地读取时标 stale | 本地没有"同一资源追加新版本"的公开 API，本地这条用户流程不完整 |
| T54 Wiki 并发更新 | P6 | ✅ | `ddp_local/tests/test_wiki.py::test_concurrent_edit_cas_preserves_history_and_human_provenance` / `test_build_publication_cas_rejects_concurrent_edit_after_model_started`；中心 `test_fixed_manifest_human_edits_cas_rebuild_and_retry`；`http.test.mjs`「Wiki metadata, immutable revisions and CAS edits…」 | — |
| T55 生成 Wiki 互引/无限扩展 | P5/P6 | 🟡 | `ddp_core/tests/test_shared_wiki.py::test_source_only_wiki_rejects_generated_self_citation_and_corrupt_excerpt`；页面预算 `test_manual_pages_never_disappear_to_satisfy_page_budget` | 引用展开深度、token 与依赖深度上限没有单独用例 |
| T57 A/B→P→R 与上级故障 | P6 | 🟡 | Go `expand_test.go::TestExpandScopeRecursesThroughDirectoryAndCatalogs`、`TestScopeCreateExpandsRemoteDirectoryAndPersistsChildManifests`（替身 peer） | 没有多主机拓扑与"上级停止后下级本域继续"的实验 |
| T58 远端重复受理/取消竞态 | P5/P7 | 🟡 | `test_federation_queue.py::test_mark_failed_cannot_overwrite_a_concurrent_cancel` / `test_mark_stalled_cannot_overwrite_a_concurrent_cancel` / `test_execute_plan_never_overwrites_a_cancelled_row`；`test_federation_admissions.py::test_admission_replay_returns_same_receipt_without_second_execution` | **"消耗记录与业务幂等分开"没有任何计量相关用例** |
| T59 GPU/上传/检索预算与缓存限制 | P6/P7 | 🟡 | `test_cache.py::test_entries_cap_evicts_least_used_then_oldest` / `test_bytes_cap_is_enforced_by_eviction` / `test_concurrent_puts_do_not_exceed_caps`；真 PG `test_cache_caps_hold_under_parallel_writer_sessions` | GPU 并发与生成费用预算需 GPU 机器 |
| T87 Probe/枚举/多跳的隐藏预算消耗 | P6 | 🟡 | 发现与探测共用根请求额度：`test_federation_cache_wiring.py::test_remote_catalog_fetch_respects_the_discovery_budget`；`test_federation_tasks.py::test_expired_scope_is_410_and_probe_budget_exhaustion_is_visible`；`ddp_local/tests/test_consents.py::test_expiry_and_root_probe_budget_are_atomic`；预占不退款 `ddp_core/tests/test_routing.py::test_root_budget_never_resets_or_backfills` | 多跳（层级委托）的预算份额没有实现，见 T41 |
| T64 扩容后每中心存储行为 | P6/P7 | 🟡 | 设计：不镜像远端正文、缓存有字节/条目/TTL 上限（T59 用例） | 没有"登记节点数增加时存储不增长"的规模实验 |

## P7 发行、安全、性能、迁移与恢复

| 测试 | 阶段 | 状态 | 证据 | 缺口 |
|---|---|---|---|---|
| T23 App 更新与本地迁移 | P7 | 🟡 | `tests/test_desktop_release.py::test_apply_keeps_previous_and_rollback_restores_it` / `test_interrupted_update_leaves_old_version_runnable` / `test_models_are_untouched_by_apply_and_rollback`；`ddp_local/tests/test_wiki.py::test_v1_migration_is_atomic_and_preserves_existing_resources` | 已安装 pacman 包的热更新未验；签名只用临时密钥 |
| T60 模型/引擎包及不支持平台 | P7 | 🟡 | `tests/test_desktop_release.py::test_bad_checksum_is_rejected` / `test_abi_mismatch_is_rejected`；`ddp_local/tests/test_model_install.py::test_import_needs_exact_digest_and_partial_never_becomes_ready`；`COMPATIBILITY-MATRIX-v3.md`（未测平台一律 ⬜） | 真实 GPU 报告需 NVIDIA 机器 |
| T61 数据库/对象/节点身份恢复 | P7 | 🟡 | `RECOVERY-DRILL-v3.md`（真实 PG+MinIO 备份恢复逐表对账）；Go `identity_drill_test.go::TestNodeIdentityBackupRestoreDrill` | 只有最小数据集；无 PITR、无生产量级、RTO/RPO 未定 |
| T62 旧资源迁移与回退 | P7 | 🟡 | `test_resource_migrations.py::test_first_uploader_without_historical_organization_is_quarantined` / `test_backfill_full_length_ids_and_unknown_organization`；`test_backfill.py::test_backfill_is_idempotent`；CI 迁移 upgrade→downgrade→upgrade | 没有生产快照上的回填、影子读取与回退演练 |
| T63 新中心与旧 App | P7 | 🟡 | `http.test.mjs`「an unknown handshake protocol version is rejected before authoritative data is applied」；`ddp_core/tests/test_compatibility.py::test_old_probe_version_is_protocol_incompatible`；Go `protocol_versions_test.go` | 没有真实的旧版本客户端产物可跑 |
| T88 首发安装包与升级机制实测 | P7 | 🟡 | `RELEASE-MANUAL-v3.md`（可复现 Arch 包、真实 0.1.0⇄0.1.1 更新与回滚、Wayland 冒烟）；Windows 便携包 CI 冒烟（desktop-windows 工作流） | 包管理器层升级、数据库备份与运行时重启一起的实测；X11/其他发行版未验 |

## 维护规则

1. **改一行必须带证据**：状态升级要写上新增的测试名或验证记录路径；降级（发现假守卫、
   发现判据被误读）同样要写原因。
2. **汇总表由脚本校对**：`scripts/check_acceptance_matrix.py` 数每个状态的行数并与汇总表比对，
   也检查 T01–T88 一个不少、不重复。不一致即红（门禁「验收台账」）。
3. 引用的测试名改名时台账必须跟着改。脚本校对：每个 ✅ 行的**证据栏**至少有一个可校验的测试引用（缺口栏里的测试名不算证据）；
   `文件::用例` 的文件必须唯一定位、用例必须在那个文件里；前缀引用（`test_x*`、
   `` `test_x…` ``、「标题…」，`...` 同 `…`）不许宽到什么都没指；不带省略号的「标题」必须**恰好等于**
   某个会真跑的 `test(...)`/`it(...)` 的标题（skip/todo 不算）；只写文件名的引用文件必须存在。
4. **✅ 行的缺口栏只能是 `—`，或以"（判据外）"开头**（记判据之外的已知限制）。缺口栏写着
   "判据的某一半没有用例"而状态是 ✅，就是自相矛盾 —— 守卫只查这个形式约定，语义仍靠验收。
5. **2026-09-15 提交前第五次验收**抽查 22 条 ✅，12 条只覆盖了判据的一部分，已按本表口径
   降为 🟡 并写明缺口（T15 T18 T29 T34 T44 T51 T58 T65 T78 T80 T81 T83）；T79 补了
   admission 自查许可的用例（删掉执行者的过期检查即红）后保持 ✅。**升回 ✅ 必须补上
   缺口栏里写的那一半**，不是改一个字。
