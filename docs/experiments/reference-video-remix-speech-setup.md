# Remix 本地语音引擎试运行

语音任务默认关闭。只有在目标机器上安装 WhisperX、预先准备好 ASR 与对齐模型权重，并配置以下环境变量后，`transcribe_media` 与 `align_script` 才会接受任务：

```sh
export DRAMACLAW_REMIX_SPEECH_ENGINE=whisperx
export DRAMACLAW_REMIX_MODEL_CACHE=/absolute/path/to/whisperx-model-cache
export DRAMACLAW_REMIX_WHISPER_MODEL=small
export DRAMACLAW_REMIX_DEVICE=cpu
export DRAMACLAW_REMIX_COMPUTE_TYPE=int8
export DRAMACLAW_REMIX_BATCH_SIZE=4
```

模型目录必须事先存在。适配器向 WhisperX 的 ASR 加载传入 `local_files_only=True`，向对齐模型加载传入 `model_cache_only=True`。模型缺失、损坏或不支持指定语言时任务报错，不会自动下载权重。需要在隔离的试验环境固定 WhisperX 及模型版本，并按 [P0 计划](reference-video-remix-p0.md) 用真实样片与人工标注测准确率、耗时及内存，再确定生产依赖和并发配置。

当前仓库未把 WhisperX 加入主环境依赖，也未附带模型权重。仅有适配器单元测试使用内存中的假 WhisperX 模块验证调用与输出协议；这些测试不代表真实语音识别质量。
