# Remix 本地语音引擎试运行

仓库提供一个仅启动本地 CE API 的入口。它使用现有的独立 WhisperX Python 和模型缓存，启动后轮询只读的语音探活接口：

```sh
scripts/start-remix-speech.sh
```

默认路径是 `$HOME/.local/share/dramaclaw/whisperx-venv/bin/python` 和 `$HOME/.cache/dramaclaw/whisperx-models`。可以通过下面两个同名环境变量覆盖。脚本显式清空控制面、Redis 和 Celery 配置，以单租户 CE 运行；脚本中不保存凭证。模型目录缺失或为空、Python 不可执行、API 未在超时内就绪时，脚本会带原因退出。

探活地址为 `GET /api/v1/projects/local/freezone/remix/speech-readiness`。它先检查 small ASR、中英文对齐缓存的关键文件，再用配置的 Python 在 `HF_HUB_OFFLINE`、`TRANSFORMERS_OFFLINE` 和 `HF_DATASETS_OFFLINE` 模式下真实启动仓库的 JSON worker，验证包入口和 WhisperX 依赖可导入并返回版本。probe 不加载模型权重，也不下载文件。返回值包含 `ready`、`python`、`model_cache`、`whisperx_version`、`downloads_allowed: false` 和可操作的 `error`。

语音任务默认关闭。只有在目标机器上安装 WhisperX、预先准备好 ASR 与对齐模型权重，并配置以下环境变量后，`transcribe_media` 与 `align_script` 才会接受任务：

```sh
export DRAMACLAW_REMIX_SPEECH_ENGINE=whisperx
export DRAMACLAW_REMIX_MODEL_CACHE=/absolute/path/to/whisperx-model-cache
export DRAMACLAW_REMIX_WHISPER_MODEL=small
export DRAMACLAW_REMIX_DEVICE=cpu
export DRAMACLAW_REMIX_COMPUTE_TYPE=int8
export DRAMACLAW_REMIX_BATCH_SIZE=4
# WhisperX 安装在独立虚拟环境时设置：
export DRAMACLAW_REMIX_WHISPERX_PYTHON=/absolute/path/to/whisperx-venv/bin/python
```

`transcribe_media` 和 `align_script` 固定进入独立的 `speech` 任务队列，不与普通文本/分析任务共用 `default` 执行槽。CE 默认的全局并发、项目并发和项目内单用户并发都是 1，全局等待队列上限是 64；适配器调用还有一层进程内单槽信号量，防止绕过任务队列的调用同时加载权重。排队任务可以正常取消，不会阻塞 `default`、`ffmpeg`、`video` 或 `world` lane。如需改队列上限，可使用 `ST_CE_GLOBAL_MAX_QUEUED_SPEECH_TASKS`；生产默认不建议调高上述并发 1 的闸门。

模型目录必须事先存在。适配器向 WhisperX 的 ASR 加载传入 `local_files_only=True`，向对齐模型加载传入 `model_cache_only=True`。模型缺失、损坏或不支持指定语言时任务报错，不会自动下载权重。需要在隔离的试验环境固定 WhisperX 及模型版本，并按 [P0 计划](reference-video-remix-p0.md) 用真实样片与人工标注测准确率、耗时及内存，再确定生产依赖和并发配置。

WhisperX 3.8.6 的强制对齐还需要 NLTK 的 `punkt_tab` 数据；安装后应在 WhisperX 虚拟环境中运行 `python -m nltk.downloader punkt_tab`，并确认该环境能找到 `tokenizers/punkt_tab/english/`。

当前仓库未把 WhisperX 加入主环境依赖，也未附带模型权重。设置 `DRAMACLAW_REMIX_WHISPERX_PYTHON` 后，任务进程通过 JSON 子进程调用独立虚拟环境，避免 WhisperX 的 NumPy 2.x 依赖覆盖项目的 NumPy 1.x。此 Python 路径必须是可执行的本地文件。未设置时仍支持主环境直接安装 WhisperX。

适配器单元测试使用内存中的假 WhisperX 模块验证调用与输出协议；这些测试不代表真实语音识别质量。

## 2026-09-21 本机有界运行记录

`/tmp/remix-p0/report.json` 是本机运行产物，不纳入版本库。运行使用 `synthetic-local`，所以所有合成标注都不计入生产门槛：

- macOS 26.6.2 arm64、CPU、并发上限 1、batch size 1、int8；FFmpeg/FFprobe 8.1。
- WhisperX 3.8.6（BSD-2-Clause）、PyTorch 2.8.0。ASR 缓存为 `Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120`；中文对齐缓存为 `99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd`，英文对齐文件 SHA-256 为 `488fd4f16de84438ffc945334278c1b9fb9b7159a806c1080b16111a958c945d`。本地缓存没有可审计的权重许可证元数据，报告将两项许可证记为 `unverified-local-cache`，这本身是生产采用前的阻塞项。
- 六类清单均提供了本地合成输入，报告保留每个媒体的 FFprobe 输出。内存字段是各项结束时整个进程的累计高水位，不能当作该项独占内存。合成纯音不是真实语音，两项清晰语音都按脚本不匹配失败，不能用于准确率结论。
- harness 现在实际执行重复词/数字/emoji、无语音、脚本不匹配和 VFR/裁切/变速协议；当前本地输入仍不具备合法来源、逐文件清单和人工标注来源，因此这些执行结果不能转为生产验收。CUDA、Linux 和其他主机未测。

生产语音门槛仍为 **blocked**：合格的中文和英文人工边界均为 0，P95 均未测。命令行 `--fixture-kind` 只是报告标签，不能让素材合格；必须为每项 fixture 提供已批准来源，为每个文件提供匹配的 SHA-256/来源/许可证/审批/取得时间，并让人工标注绑定音频哈希和标注者/复核者记录。每种语言还必须至少 100 个手工边界、同一脚本两种语速、P95 不超过 200 ms，并记录非占位的模型、权重和主机版本与许可证。不要修改报告算法或用合成边界补数。
