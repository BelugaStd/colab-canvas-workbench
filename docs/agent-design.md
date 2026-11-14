# Canvas Agent

## Sources before implementation

The sidebar layout was inspected in the user's open TapNow canvas on 2026-11-13: project navigation, nested canvas conversations, a resizable 400px conversation panel between navigation and canvas, bottom composer and canvas attachments. No paid TapNow generation was submitted.

- [FLORA FAUNA](https://docs.flora.ai/editor/fauna): persistent project conversations, selected/@ node context, graph editing/execution and inspect-before-mutate.
- [FLORA execution](https://docs.flora.ai/nodes/how-nodes-run): dependencies execute in order; active versions are downstream inputs; upstream edits do not silently rerun outputs.
- [FLORA character consistency](https://flora.ai/guides/how-to-keep-a-character-consistent-across-ai-images-and-video): reuse original identity anchor instead of chaining identities through successive scenes.
- [Google Flow Agent](https://support.google.com/flow/answer/17093911?hl=en): planning, prompts, project sessions, references, batch generation and asset management.
- [Flow video references](https://support.google.com/flow/answer/16353334?hl=en) and [scene editing](https://support.google.com/flow/answer/16935718?hl=en): first/end frame roles, ingredient references, extracted frames and deterministic sequencing.

These sources inform interactions; they do not imply MiniMax supports other vendors' video-edit models or guarantees perfect identity.

## Minimal architecture

Keep existing Store, project validation, compiler, Manager jobs and version model. Add an aiohttp provider adapter, a durable Agent service/router, validated domain tools, deterministic media actions and a glass sidebar. No new agent framework dependency.

Sessions persist canonical protocol-neutral messages. Each run records lifecycle, model usage and tool actions. Append complete model responses before executing tools. Persist a tool invocation before a mutation and its durable result before asking the model again. Generation uses existing durable job IDs and a per-call idempotency ledger; uncertain side effects are reconciled instead of blindly replayed. Cancellation stops Agent continuation; already submitted generation is kept visible and cancellation has its own tool.

Read canvas first, then mutate with the observed revision. Atomic graph patches use the same canvas/reference validators as manual operation. Apply exact input snapshots to generation jobs. Preserve active versions and merge completed assets on the server for subsequent dependent operations; manual UI and Agent refresh use revisions to avoid autosave overwrite.

Creative tools cover capabilities/canvas/assets/jobs/server inspection; brief/storyboard; graph creation/update/connect/disconnect/layout/remove; output selection; generate/batch/wait/cancel/resume; frame extraction; timeline assembly/export; project/canvas organization and compute lifecycle. Server stop requires explicit visible approval within the application. There is no arbitrary shell tool, unrestricted file access or arbitrary Comfy-class submission. The remote command line remains a manual advanced-panel feature. Tools operate only on this run's project/account and named local assets.

Modes: Plan permits planning and graph/prompt editing, no paid inference. Auto permits generation and deterministic assembly. Tool cards show observable actions/results, never hidden reasoning. Conversations nest under their canvas; selected-node chips/@ picker provide explicit context. Users can cancel and reopen sessions without losing completed work.

## Cache contract

Freeze versioned director instructions and tool schemas/order. Dynamic canvas state is an appended tool response, not rewritten system instructions. Keep the canonical model history including provider reasoning items/call IDs where required, and append deltas rather than injecting timestamped full snapshots each request. Cache metrics use actual provider cached-token counters; absent counters remain unknown. Provider-specific caching options are opt-in/capability-specific; no Anthropic protocol or automatic endpoint fallback after an uncertain request.

Compact only at a deliberate size boundary: preserve stable project brief, node/asset/job IDs, unfinished plan and recent complete tool pairs in a durable checkpoint; do not prune an open tool round. The immutable prefix remains identical after compaction. Result reuse applies to explicit dependency/output reuse, not a user's request to regenerate a fresh version. GPU execution remains serial per account; compatible image stages precede video stages to reduce model switching.

Provider contracts are grounded in [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Responses](https://developers.openai.com/api/reference/resources/responses/methods/create), [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching). There is no Anthropic Messages adapter. Completed Responses output, including opaque reasoning, is replayed intact; calls from truncated or explicitly unfinished responses are rejected. Accepted requests are not retried after uncertain response-read failures. Connection tests and summaries use streaming for gateways that require it.

## 用户操作

从项目侧栏打开 Agent 或进入画布下的已有对话。设置服务地址、模型名、密钥，选择 Responses / Chat Completions 并「保存并测试」。远程服务要求 HTTPS；本机兼容服务允许 HTTP。模型必须支持工具调用，实际看图还需视觉输入能力。更换模型或协议请新建对话。

选中节点作为显式上下文，或用 `@` / 引用按钮指定节点；附件先进入本地素材和画布。Plan 允许策划、提示词及图编辑，但禁止连接 GPU、付费生成和媒体处理。Auto 允许执行，GPU 停止仍需审批。输出复用依据当前版本；重新生成保留为新版本。

停止 Agent 仅停止后续编排，已提交的 GPU 任务保留。生成任务本身由 `cancel_job` 单独取消。重启或失败后继续原任务，查询旧 job 而非重新生成。悬空工具轮必须恢复闭合或新建对话；较旧任务不能越过更新消息恢复。工具卡显示可观察输入和结果，不展示隐藏推理。

## 模块与工具

长篇短剧的制作台账、固定资产版本、独立审片与对白调度见 [专业剧集制作与验收](agent-production.md)。

| 模块 | 职责 |
| --- | --- |
| `backend/agent_prompts.py` | 版本化导演规则、模型约束与创作质量 |
| `backend/agent_provider.py` | 两种协议、流式响应、完整调用与实际用量 |
| `backend/agent_service.py` | 持久会话、审批、取消恢复、账本与检查点 |
| `backend/agent_tools.py` | 固定 schema、领域校验、原队列调用 |
| `backend/agent_media.py` | 本地 ffmpeg 抽帧、裁剪、合片和导出核验 |
| `backend/agent_vision.py` | 本地图片临时缩图与有界 LRU 缓存 |
| `frontend/src/creative/AgentPanel.tsx` | 侧栏对话、设置、上下文与工具卡 |

| 工具 | 用途 |
| --- | --- |
| `inspect_canvas` | 当前 revision、节点/连线、当前输出、可选版本及模型能力 |
| `inspect_assets` | 本地素材元数据；`visual_asset_ids` 指定最多四张实际图片 |
| `edit_canvas` | 原子建图、更新、布局、连接/断线、删除、选择输出 |
| `save_story` | 故事目标、人物设定、视觉规则与有序分镜 |
| `generate_nodes` | 调用原有编译和队列；返回持久 job ID |
| `get_jobs` | 本地等待终态或立即查询，合并完成输出 |
| `cancel_job` / `resume_job` | 取消生成 / 恢复查询原任务 |
| `server` | 所选账号状态、日志、连接、需批准的停止 |
| `extract_frame` | 本地视频指定时间提帧，保存图片和可选节点 |
| `assemble_film` | 按真实素材 ID 编排、裁剪、合片与添加成片节点 |
| `manage_project` | 当前项目/画布重命名，新建或复制画布 |

变更携带读取到的 `expected_revision`；冲突 409 要求重新读取。默认 `get_jobs` 在本地循环等终态，等待数分钟不反复付费请求模型；`wait:false` 立即读一次。相同账号 GPU 串行，不同账号并行。

本地重启后，Auto 的等待发现旧 job 为 recovering 且没有查询进程时，只恢复该 job 的 worker；原有 prompt 使调度器仅重连原 GPU。一次恢复仍受阻时返回可见错误，不在本地无限等待；Plan 不启动 worker。媒体下载走既有会话的认证文件接口，独立于 SSH 控制通道，验证 Range/文件版本和完整长度后才原子保存；无法使用时记录后备路径，不公开运行时服务。

## 持久化与恢复边界

请求以会话和唯一 `request_id` 去重，消息、账号、模式和选择必须一致。调用账本以会话加 `call_id` 定位，名称、规范化参数和所属任务必须一致；完成结果先保存，再追加工具消息。生成元数据在 worker 启动前落盘，恢复读取原 job。读取可重做，媒体和新画布通过确定性 ID 复用。

审批等待和决定持久保存，重启后继续原任务再确认；批准只对应当前调用。无可靠幂等保证的修改在执行结果不明时保存可见错误、闭合调用并停止，不能盲目再次修改。已保存的最终模型回答恢复时直接完成，避免再次计费。取消后的执行须清理完毕才能恢复。

私密配置位于数据目录 `agent-private.json`，公共 GET 不返回密钥，PUT 留空保留原密钥。对话、工具结果及按要求检查的素材会发送给配置的模型服务。视觉工具仅接受校验过的本地图片 ID，最长边缩至 1024；LRU 最多 32 个缩图，数据库只保留素材标记，不写 base64。视频先提帧再看图。

约 240,000 个序列化字符触发摘要，安全切点必须是完整工具轮边界；完整原始历史、摘要、模型、协议和时间作为检查点保留。摘要调用的实际用量计入总量。cached token 缺失显示未知 `—`，不当作零命中；GPU 权重驻留与模型供应商的提示缓存分别管理。高级 `cache_key` / `cache_options` 通过设置 API 可选配置，空选项不发送，不默认强制特定保留时长。

## 本地 API

API 沿用工作台访问令牌。

| 路径 | 用途 |
| --- | --- |
| `GET/PUT /api/agent/settings` | 公开配置 / 保存模型连接 |
| `POST /api/agent/test` | 当前协议的一次真实流式测试 |
| `GET/POST /api/agent/sessions` | 按项目/画布列出 / 新建对话 |
| `GET /api/agent/sessions/{id}` | 可见消息及执行记录 |
| `POST /api/agent/sessions/{id}/messages` | 消息、模式、账号、选择和唯一请求 ID |
| `GET /api/agent/runs/{id}` | 状态、工具卡、用量和项目 revision |
| `POST /api/agent/runs/{id}/cancel` | 停止后续 Agent 编排 |
| `POST /api/agent/runs/{id}/resume` | 继续中断、失败或已取消任务 |
| `POST /api/agent/runs/{id}/approval` | `approved` 与当前 `call_id`，明确批准或拒绝对应调用 |

画布工具使用可扩展的故事字段和可选参数，因此当前不声明 OpenAI `strict:true` schema。设置会拒绝 `strict_tools:true`，旧配置读取时按 false 处理；非严格协议下仍在本地完整校验，不能绕过参数、引用和权限边界。

## 合片规格

本地 ffmpeg 支持 1–60 个视频片段、逐镜起点和时长、24/30/60 fps、最高 4K 像素预算，总长最多 10 分钟。统一尺寸时保持比例并补边；无音轨片段补静音，保留音轨的片段重采样为 AAC。最终 H.264/AAC 通过实际宽高、帧率、帧数、时长和音轨检查才入库。它不自动创作连续配乐，模型画面和角色一致性需要另外检查。

## One-minute film acceptance

Original story, 60-second timeline, one identity anchor and style bible, individual 5–15s H3 shots with approved first/end stills, fixed costume/props and deliberate limited camera motion. All stills return to the original anchor. Validate actual output frames and reference fidelity before committing remaining GPU budget. Assemble ordered clips deterministically; verify exact duration/fps/codec/audio and inspect contact sheets. Preserve editable graph, prompts, versions and final media node. Track gross account CU difference with a 100-CU cap. Keep this test GPU and local services running after delivery as requested; the budget guard may stop only the owned test session before exceeding the authorized cap.
