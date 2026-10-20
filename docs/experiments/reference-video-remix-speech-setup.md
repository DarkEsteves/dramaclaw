# 参考视频改编：NewAPI 语音转写配置

画布的“转写”“按脚本对齐”和逐段成片编排统一调用 NewAPI 网关的 `audio-transcribe` 模型。模型需要通过 `/v1/audio/transcriptions` 返回词级时间戳。按脚本对齐会将识别结果与脚本逐字比较；两者不一致或缺少词时间戳时会要求复核，不会伪造时间。该能力是基于识别词的映射，不是声学强制对齐。

本地运行前设置网关和密钥，或使用项目已有的 NewAPI 运行时配置：

```sh
export NEWAPI_BASE_URL=https://your-gateway.example/v1
export NEWAPI_API_KEY=your-key
uv run novelvideo api --port 8780
```

不要把密钥提交到仓库。语音任务使用独立 `speech` 队列；可通过现有队列设置控制并发。页面提示转写服务未配置时，先检查网关和密钥，再调用已认证的 `GET /api/v1/projects/local/freezone/remix/transcription-readiness`。返回的 `ready` 只表示已配置有效的网关地址和密钥，不会发出模型请求，也不证明该模型已获得访问权限。一次实际转写才能验证模型可用性。

语音质量仍需用合法来源的中英文样片和人工词边界测量。试验方法见 [P0 实验](reference-video-remix-p0.md)。
