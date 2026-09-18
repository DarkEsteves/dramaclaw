# Remix 本地语音引擎试运行

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
