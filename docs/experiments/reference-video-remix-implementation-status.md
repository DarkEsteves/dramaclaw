# 参考视频改编：实施与发布验收状态

日期：2026-09-22。分支：`codex/reference-video-remix-plan`。基线：`origin/staging` 的 `dcfbc726`。

## 当前结论

固定媒体的原生画布链路已达到 **demo-ready**：上传参考视频、全片证据分析、蓝图审校、改编脚本、B-roll 绑定、旁白/对齐、语义时间轴、渲染清单、1080×1920/30 fps 合成、局部重跑与刷新恢复均有自动化验证；从有声参考视频到实际 MP4 导出的完整浏览器人工旅程也已通过。旧 Recipe v1、旧画布和旧合成路径保持原行为。

当前发布矩阵验证的是一条连续的单人旁白。音频节点保留多角色选声等兼容界面，但多人配音的拼接、角色切换与完整成片不属于本次已验收 MVP 范围。

本分支 **不是 production speech-ready**。真实中英文代表性样片、每种语言至少 100 个人工边界、授权/来源清单和模型权重许可证仍未提供，因此 P95 对齐误差与生产并发配额不得声称通过。

## 已闭环功能

| 阶段 | 已验收交付 |
| --- | --- |
| P0 试验架 | 六类样片协议、离线 WhisperX 探活、真实来源/标注防伪、P95/时间/内存/渲染报告。真实语音准确率门槛仍受外部输入阻塞。 |
| M1 证据与蓝图 | 项目内不可变 artifact revision/CAS，全片 VFR 证据，帧与台词精确引用，可编辑、审核、保存和重载的原片拆解节点。 |
| M2 脚本与时钟 | Unicode 语义选区，重复词/增删词失效，精确脚本/对齐/旁白 hash 绑定，裁剪和变速后的整数帧编译，人工覆盖冲突处理。 |
| M3 分层合成 | 显式背景/A-roll，图片/视频 B-roll，字幕安全区与逐词高亮，淡入淡出/缩放，旁白连续、BGM ducking、SFX，预览/导出共用同一 manifest。 |
| M4 编排与恢复 | Recipe v2 结构产物，五个注册处理动作，类型化 DAG，`继续改编` 可见入口，幂等提交、精确失效、取消/超时/迟到结果与刷新恢复。 |
| M5 发布矩阵 | 旧契约兼容、项目隔离、内容 hash/缺失文件缓存失效、路径穿越、ASS/过滤器安全、规格上限、队列容量、授权和计费边界回归。 |

## 发布验收矩阵

| 验收项 | 结果 | 自动化证据 |
| --- | --- | --- |
| 必要事件 | 通过 | 必要选区缺失、只对齐部分字符、缺少视觉/字幕输出都会阻塞；可选事件需显式省略决策。 |
| 时间编译 ≤1 帧 | 通过 | 分数时钟使用有理数和半开区间；2x 语速用例把 30–60 帧精确移到 15–30 帧。 |
| 1080×1920/30 fps | 通过 | manifest v2 拒绝其他画幅、超过 60 秒或超规 fps；真实 FFmpeg 输出探针见下文。 |
| 字幕安全 | 通过 | ASS 使用声明的左/右/底部边距，转义反斜杠、大括号、LF、CRLF 和 CR；用户路径不进入 filter graph。 |
| 连续旁白 | 通过 | manifest v2 要求旁白从第 0 帧连续覆盖结尾；B-roll 前/中/后都检查画面且输出保留 AAC 音轨。 |
| 预览/导出一致 | 通过 | 指定事件边界的 14/15/44/45 帧，预览 PNG 与导出抽帧 SHA-256 逐帧相同。 |
| 局部重跑 | 通过 | 字幕样式只重渲染；单个 B-roll 只重生该素材及合成；脚本/旁白才会重对齐和重编译。 |
| 刷新/迟到/并发 | 通过 | 重开使用已绑定 task 对账；超时不重投；取消/过期/旧 revision 结果只留历史；提交和确认都有 fencing token。 |
| 兼容与隔离 | 通过 | Recipe v1 指令不变，manifest v1 走旧 renderer，旧 canvas fixture 可读；跨项目 ref、绝对/上跳路径、symlink payload 被拒绝，删除产物不命中假缓存。 |
| 资源限制 | 通过 | WhisperX 转写/对齐进独立 speech lane，全局、项目及项目用户默认并发都为 1，等待 64，进程内另有单槽信号量；无语音的参考视频分析保持 default lane，渲染进 ffmpeg lane，各 lane 互不阻塞；排队任务可取消，队列满映射 HTTP 429。 |
| 独立 WhisperX Python | 通过（运行与完整浏览器旅程） | 浏览器人工检查先发现脚本启动丢失包上下文，随后发现独立环境没有应用侧 Pydantic。入口现显式加入包根；适配器内部的对照 DTO 改为仅用标准库，worker 无需安装应用依赖。readiness 直接启动真实 worker 的离线 probe，而非只探测 `import whisperx`。隔离 `-S` Python 回归、本机实际 WhisperX venv probe 和下述完整浏览器旅程均成功。 |
| 计费/授权 | 通过 | 确定性处理动作不伪造 Recipe 计费；创作 Recipe 继续使用预留/结算/待人工复核协议；读需 viewer、写/提交需 editor 且只能在项目 home node。 |
| 真实语音边界 | **阻塞** | 合格中文/英文人工边界都是 0，P95 未测；模型权重许可证仍为 `unverified-local-cache`。 |

## 固定样片与输出探针

样片与权重不进 Git。本机合成 fixture 仅用于协议和渲染回归，不计入真实语音准确率。

- P0 报告：`/tmp/remix-p0/report.json`，SHA-256 `95c6d68dd2ae58c51bd13afc3162672bb5e329187942b3dbb327e563996754fb`。
- 可变帧率+裁剪/变速产物：SHA-256 `433a6672d325b1a25e4dc93cc7c9f59b62b372a9ccf4dcf3b0607e7653b3e1fd`。
- 清晰样例参考视频：SHA-256 `3acc06646ed54d5ced9b62c9d2b9d620190ed89180e1641b4ad4505a9673995d`；快/慢旁白分别为 `330bf5854b4283fc7c145a9878e09c4a80fff1f1ec83980fb36e4f7a3d80dbcc` 和 `2a647c902d073eeff3a770f832154a3ba8534e175e520834573b711a7fa78228`。
- 发布合成探针：SHA-256 `2f5db2ee14115f8e2ac003cba104bcd55ffbe6d0ed73eba544612a4945c402fe`；FFprobe 为 H.264 `1080x1920` `30/1`＋ AAC 44.1 kHz mono，时长 `15.000000` 秒。
- 浏览器完整旅程输出：SHA-256 `0736f9a9f5a6897fd1c23b2bf4100d45702bfd04233cb18be4f0f91073cba4db`；FFprobe 为 H.264 `1080x1920` `30/1`＋ AAC 16 kHz mono，时长 `15.000000` 秒。

## 运行环境与模型版本

- macOS 26.6.2 arm64；Python 3.12.13；FFmpeg/FFprobe 8.1。
- WhisperX 3.8.6，CPU，`int8`，batch size 1，语音并发上限 1，离线模式且禁止下载。
- ASR：`Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120`。
- 对齐：`zh-cn@99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd`；英文缓存 SHA-256 `488fd4f16de84438ffc945334278c1b9fb9b7159a806c1080b16111a958c945d`。
- 本机合成纯音不是真实语音；P0 报告保持 `blocked`，不会通过改命令行标签被伪造为通过。

## 人工画布完整旅程

使用 `scripts/start-remix-speech.sh` 启动后，在空白项目画布完成了一次真实浏览器全流程：上传 18.1 秒、720×1280、带人声的参考视频，由 WhisperX 转写原片，得到 9 个拆解事件并逐项标记为已核对；生成并保存 `remix_script.v1` r5 英文脚本，标记 5 个语义选区；生成 1 张真实咖啡图片，并将它复用于 4 个 B-roll 区间。

语音供应商 TTS 曾停在 10% 后被取消，不能据此声称供应商 TTS 成功。随后上传一条与最终脚本逐字一致的 15.000 秒单人旁白 WAV；WhisperX forced alignment 完成 163 字符 token 对齐并生成 `performance_timing.v1` r1，五个事件帧区间为 `0–19`、`28–98`、`109–180`、`189–254`、`265–392`。另行上传独立的 1080×1920 背景，得到 `composition_manifest.v1` r1 并实际渲染成功。页面最终显示 `预览与导出一致`。

导出的 MP4 SHA-256 为 `0736f9a9f5a6897fd1c23b2bf4100d45702bfd04233cb18be4f0f91073cba4db`；FFprobe 为 H.264 `1080x1920`、`30/1` fps，AAC 16 kHz mono，时长 `15.000000` 秒。人工旅程还发现并促成两类边界修复：英文空格不应导致选区对齐误判（`ad2546af`）；长 raw 指纹曾被后端 256 字符字段限制拒绝（`dae22cd5`），随后增加安全长度上限（`93c9cb12`）。

这次通过证明固定媒体画布的完整浏览器旅程可运行，但不改变真实语音生产门槛：它不是中英文各 100 个人工边界数据集，也不补足素材/权重许可证或目标 Linux/GPU 机器数据，因此仍不得声称 **production speech-ready**。

## 精确验证命令

```text
uv run pytest -q tests/test_remix_*.py tests/test_workflow_stable_contract.py tests/test_recipe_runtime.py tests/test_recipe_v2_delivery.py tests/test_freezone_canvas_store.py tests/test_api_compose_export_contract.py tests/test_video_composer_result_contract.py tests/test_task_backend_limits.py tests/contract/test_l014_inline_task_backend_concurrency.py
# 313 passed

uv run pytest -q
# 6639 passed / 19 skipped / 3 deselected（语音并发和 worker 包装修复后）

uv run pytest -q tests/test_remix_whisperx_adapter.py tests/test_remix_alignment.py tests/test_remix_p0.py tests/test_remix_tasks.py tests/test_task_backend_limits.py tests/test_task_runner_home_node_placement.py tests/contract/test_l014_inline_task_backend_concurrency.py
# 102 passed（独立运行时零应用依赖与 readiness worker probe 修复后）

uv run pytest -q tests/test_remix_whisperx_adapter.py::test_external_whisperx_worker_script_bootstraps_project_package tests/test_remix_whisperx_adapter.py::test_external_whisperx_aligner_runs_real_worker_subprocess tests/test_remix_whisperx_adapter.py::test_external_whisperx_python_runs_isolated_worker
# 3 passed；前两项是真实子进程，未 mock subprocess

pnpm exec vitest run src/features/canvas/remix src/api/remix.test.ts src/__tests__/features/canvas/remix-nodes.test.tsx src/__tests__/features/freezone/workflow-run-recovery-bar.test.tsx src/__tests__/features/superchat/chat-task-status-bar.test.ts
# 38 files / 297 passed

pnpm exec vitest run
# 566 files / 4702 passed

pnpm exec tsc -b --pretty false
uv run python scripts/generate_workflow_contract.py --check
UV_CACHE_DIR=/tmp/uv-task11-cache uvx pre-commit run gitleaks --all-files
git diff --check
```

## 外部阻塞与上线条件

上线生产语音前必须补齐：

1. 中文、英文各至少 100 个绑定原音频 SHA-256 的人工边界，包含标注者、复核者和时间。
2. 六类真实 fixture 的来源、许可证、审批 ID、取得时间和每个资产的 SHA-256 清单。
3. 中英文 P95 边界误差均 ≤200 ms，同一脚本的两种语速都通过，所有必要事件解析或明确阻塞。
4. 候选 ASR/对齐权重的非占位版本和许可证，以及目标 Linux/GPU 机器上的冷/热耗时、累计高水位内存和并发数据。

在这些证据到位前，可以演示和验收固定媒体画布流程，不得对真实语音准确率或生产容量做通过声明。

## 操作入口

新项目复现步骤、页面按钮、支持格式和常见恢复路径见[参考视频改编：画布操作与本地运行](../zh/guides/reference-video-remix.md)。本地语音依赖与离线探活细节见[Remix 本地语音引擎试运行](reference-video-remix-speech-setup.md)。
