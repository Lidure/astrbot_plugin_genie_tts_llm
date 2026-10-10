# Genie TTS 后端部署：魔搭 ModelScope 创空间

本 AstrBot 插件只负责调用 Genie TTS HTTP 服务，不包含声学模型及服务端。**推荐将兼容的 Genie TTS 服务部署到 ModelScope 创空间**。

## 1. 创建并运行创空间

1. 登录 [ModelScope 创空间](https://modelscope.cn/studios)，创建或复制一个提供 Genie TTS 接口的创空间。Docker 类型空间需满足魔搭平台的账号认证与资源要求。
2. 可参考 [clown145/genie-tts](https://github.com/clown145/genie-tts) 的服务端示例；根据该项目的说明部署模型并准备 `reference_audio`（包含参考音频 `.wav` / `.ogg`）。
3. 创空间运行后，确认 HTTP 服务存在 `POST /set_reference_audio`、`POST /tts`、`GET /ui/options`。**其他普通 Gradio TTS 演示空间不一定实现此 Genie 协议，不能仅靠填 URL 就直接兼容。**
4. 在创空间页面获取对外访问地址，一般是 `https://账号名-空间名.ms.show`。复制**根地址**，不要附加 `/tts`、`/studios/...` 或网页上的 iframe 路径。

## 2. 配置 AstrBot 插件

在插件设置中：

- **TTS 服务器列表 (`tts_servers`)**：填入创空间实际根地址，例如 `https://your-space.ms.show`（必须换成自己的地址）。多实例可填多个根地址。
- **ModelScope API Token (`modelscope_api_token`)**：公开的 `*.ms.show` 创空间**无需填写**。只有实际使用 `api-inference.modelscope.net` 专属推理网关时才填写 Token；也可用 `MODELSCOPE_API_TOKEN` 环境变量设置。
- 默认角色必须是服务端实际加载的角色，感情注册需要参考音频路径及文字与服务端一致。
- `/tts-status` 可以检测 Genie 服务 `GET /ui/options`，并在 `api-inference.modelscope.net` 地址上携带鉴权。公开创空间不会收到 Token。

> 安全说明：插件仅允许向精确的 `api-inference.modelscope.net` 或其子域发送 ModelScope Bearer Token，绝不会向任意第三方域名或公开 `.ms.show` 创空间发出。不要将 Token 提交到 GitHub，也不要把它写在 URL 里。

## 3. 网络、接口与注意事项

- 创空间休眠后首次请求可能较慢，可以在插件里启用 Space 保活并设置适合的平台周期。
- `GET /ui/options` / `POST /set_reference_audio` / `POST /tts` 都是 Genie 服务端提供的接口；网络可达但接口不匹配通常返回 404 或 405，应先排查创空间服务端部署。
- `401/403`：检查专属推理网关 Token/空间授权；`404`：确认是不是把 ModelScope 的 `/studios/...` 页面当作 API 根地址；`502/504`：检查创空间启动、模型加载与请求超时。
- 此改版沿用上游最新版的 `provider_translation`、翻译、情感与 WebUI 工作台。与魔搭创空间适配无关的模型下载和角色配置仍由 Genie 服务端维护。

原上游：<https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm>。

## 4. 平静语气与自然停顿（v1.11.2 起）

需要更自然的聊天节奏时，可在插件配置中将默认感情设为已经注册的 `平静`，并开启 `enable_natural_pauses`。推荐先使用：`natural_pause_sentence_ms=550`、`natural_pause_clause_ms=160`。句间和逗号短停顿不会修改音频音高、语速，也不需提示 LLM 手动插标签。可以按试听结果在插件配置中随时调整或关闭。

此功能要求 ModelScope 创空间的 Genie `/tts` 能识别 `[pause=毫秒]`，服务端不能把标记当普通文字念出来；其它不支持该协议的 TTS 后端请保持关闭。已经手动插入的 `[pause=毫秒]` 会被保留（需开启原有的 `enable_custom_pause_marker`），不会再叠加自动停顿。单句且没有内部停顿标点时保持原样。
