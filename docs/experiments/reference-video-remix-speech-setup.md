# Remix 本地语音引擎试运行

仓库提供一个仅启动本地 CE API 的入口。它使用现有的独立 WhisperX Python 和模型缓存，启动后轮询只读的语音探活接口：

```sh
scripts/start-remix-speech.sh
```

默认路径是 `$HOME/.local/share/dramaclaw/whisperx-venv/bin/python` 和 `$HOME/.cache/dramaclaw/whisperx-models`。可以通过下面两个同名环境变量覆盖。脚本显式清空控制面、Redis 和 Celery 配置，以单租户 CE 运行；脚本中不保存凭证。模型目录缺失或为空、Python 不可执行、API 未在超时内就绪时，脚本会带原因退出。

探活地址为 `GET /api/v1/projects/local/freezone/remix/speech-readiness`。它只检查配置、目录和可执行文件，不导入 WhisperX、不运行子进程也不下载权重。返回值包含 `ready`、`python`、`model_cache`、`downloads_allowed: false` 和可操作的 `error`。

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

模型目录必须事先存在。适配器向 WhisperX 的 ASR 加载传入 `local_files_only=True`，向对齐模型加载传入 `model_cache_only=True`。模型缺失、损坏或不支持指定语言时任务报错，不会自动下载权重。需要在隔离的试验环境固定 WhisperX 及模型版本，并按 [P0 计划](reference-video-remix-p0.md) 用真实样片与人工标注测准确率、耗时及内存，再确定生产依赖和并发配置。

WhisperX 3.8.6 的强制对齐还需要 NLTK 的 `punkt_tab` 数据；安装后应在 WhisperX 虚拟环境中运行 `python -m nltk.downloader punkt_tab`，并确认该环境能找到 `tokenizers/punkt_tab/english/`。

当前仓库未把 WhisperX 加入主环境依赖，也未附带模型权重。设置 `DRAMACLAW_REMIX_WHISPERX_PYTHON` 后，任务进程通过 JSON 子进程调用独立虚拟环境，避免 WhisperX 的 NumPy 2.x 依赖覆盖项目的 NumPy 1.x。此 Python 路径必须是可执行的本地文件。未设置时仍支持主环境直接安装 WhisperX。

适配器单元测试使用内存中的假 WhisperX 模块验证调用与输出协议；这些测试不代表真实语音识别质量。

## 2026-09-21 本机有界运行记录

`/tmp/remix-p0/report.json` 是本机运行产物，不纳入版本库。运行使用 `synthetic-local`，所以所有合成标注都不计入生产门槛：

- macOS 26.6.2 arm64、CPU、并发上限 1、batch size 1、int8；FFmpeg/FFprobe 8.1。
- WhisperX 3.8.6（BSD-2-Clause）、PyTorch 2.8.0。ASR 缓存为 `Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120`；中文对齐缓存为 `99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd`，英文对齐文件 SHA-256 为 `488fd4f16de84438ffc945334278c1b9fb9b7159a806c1080b16111a958c945d`。本地缓存没有可审计的权重许可证元数据，报告将两项许可证记为 `unverified-local-cache`，这本身是生产采用前的阻塞项。
- 六类清单均提供了本地合成输入，报告保留每个媒体的 FFprobe 输出。本次进程内首先运行的中文项为 4.619 秒、进程峰值 1170.453 MiB；随后英文项为 0.560 秒、累计进程峰值 1441.922 MiB。合成纯音不是真实语音，两项清晰语音都按脚本不匹配失败，不能用于准确率结论。
- `no-speech` 和预期脚本不匹配类别被记录为预期状态；重复词/数字/emoji 与 VFR/裁切/变速仍缺少可执行的真实 fixture 协议。CUDA、Linux 和其他主机未测。

生产语音门槛仍为 **blocked**：合格的中文和英文人工边界均为 0，P95 均未测。只有 `approved-real` 输入可以计入门槛；每种语言必须至少 100 个手工边界、同一脚本两种语速、P95 不超过 200 ms，并记录模型、权重和主机版本。拿到合法样片和标注后继续使用同一条 P0 命令，把 `--fixture-kind` 改为 `approved-real`，不要修改报告算法或用合成边界补数。
