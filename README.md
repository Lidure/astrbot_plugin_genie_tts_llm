
<div align="center">

<img src="logo.png" alt="Genie TTS LLM" width="120" />

# astrbot_plugin_genie_tts_llm

_✨ AstrBot LLM 回复语音合成插件 ✨_  

本 fork 基于 [clown145/astrbot_plugin_tts_llm](https://github.com/clown145/astrbot_plugin_tts_llm) 调整。

[![Release](https://img.shields.io/github/v/release/Whereis-Alice/astrbot_plugin_genie_tts_llm?label=Release&color=brightgreen)](https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm/releases/latest)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![GitHub](https://img.shields.io/badge/Fork-Whereis--Alice-blue)](https://github.com/Whereis-Alice)

</div>

## 📖 功能简介

本插件是为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的一款高级语音合成工具。它能将 LLM 的文本回复无缝转换为带有多样情感的语音消息，赋予您的机器人更加生动和个性化的表达能力。

- **两种语音化模式**：
    1.  **固定情感模式**：使用预设的默认或切换后的情感进行语音合成。
    2.  **自动情感识别模式**：调用 LLM 分析文本情感，并自动匹配最合适的声音进行合成。
- **长文本加速**：句子切分 + 并行合成，把长回复拆开并发请求多台 TTS 服务器，最后无缝拼接。
- **动态情感管理**：用指令或工作台自由注册、编辑、删除、试听各个角色的情感。
- **手动合成**：`/合成` 绕过 LLM 直接把指定文本读出来，方便调音色。
- **LLM 主动语音工具**：用户明确要求「说一句听听」「发段语音」时，LLM 可主动调用工具直接发一条语音，不影响日常自动触发策略。
- **故障转移与触发频率**：多台 TTS 服务器自动接力；支持每次触发、按时间间隔触发、按随机概率触发，降低聊天时的语音打扰。
- **参考音频泄漏防护 / 语音截断防护**：拦截后端偶发的「参考音频叠进结果」与「话没说完就结束」，异常时自动重合成，并在末尾补静音抵消播放端吃掉尾帧。
- **状态持久化**：会话/群语音开关、自动情感模式、各会话选的角色与感情都会保存，重启 AstrBot 后不用重新 `/tts-w`。
- **一键自检**：`/tts-status` 给出开关状态、当前音色是否可用、合成统计，并并发探测每台 TTS 服务器的连通性、延迟与可用角色。
- **WebUI 语音合成工作台**：在插件页里试听合成、批量管理感情、改配置、探测服务器、开关会话语音、看合成日志；内置 6 套 galgame 主题与舒适 / 紧凑双密度。
- **感情包导入导出**：一条指令把整套音色导出成 JSON，换机器人或分享给朋友时直接导入，支持三种合并模式与试运行预演。
- **语音收藏 / 语音转文件**：引用 bot 发过的语音，一句 `/语音收藏` 逐字节存进本地音库（不解码不转码），之后 `/发收藏` 原样重发、不耗 TTS 算力；`/语音转文件` 则把语音改成文件发出来，方便存到手机或电脑。
- **合成链路日志**：工作台「日志」页把每次合成的 LLM 原文、译文、真正送进 TTS 的文本、命中的参考音频、情感是 LLM 选的还是关键词兜底的、耗时与失败原因全串起来，并按「角色 · 情感」的失败率排出最该换参考音频的那几个。
- **注意**：该服务默认合成日语，但支持通过配置或注册指令合成其他语言（需模型支持）。

---

## 📚 文档

| 文档 | 里面有什么 |
| :--- | :--- |
| [部署 Genie TTS 服务](docs/deploy.md) | ModelScope 创空间部署、模型与参考音频准备、本地部署、自动保活 |
| [配置项详解](docs/config.md) | 全部配置逐项说明：基础、翻译 API、LLM 注入、性能、语音收藏、日志 |
| [指令手册](docs/commands.md) | 全部指令与别名：感情管理、开关与音色、手动合成、感情包、语音收藏、诊断 |
| [WebUI 语音合成工作台](docs/webui.md) | 十个视图分别能做什么、主题与信息密度、接口清单 |
| [感情包](docs/emotion-packs.md) | 换机器人 / 分享音色：导出、导入、三种合并模式、试运行、服务端快照 |
| [语音收藏](docs/voice-vault.md) | 把 bot 发过的好语音无损留下来：收藏、重发、转文件、收藏包 |
| [故障排查](docs/troubleshooting.md) | 语音重叠 / 尾音被切 / 没有停顿 / 一直失败 / 工作台空白……按现象查 |
| [更新日志](CHANGELOG.md) | 每个版本改了什么 |

只想跑起来的话，读完本页的「插件安装」和「快速开始」就够了。

---

## ⚠️ 重要前置：部署语音服务

本插件**自身不进行语音合成**，它依赖一个后端的 **Genie TTS 服务**（[官方仓库](https://github.com/High-Logic/Genie)）。你必须先拥有一个可访问的该服务，插件才能正常工作。

- **推荐**：使用魔搭社区（ModelScope）创空间部署兼容的 Genie TTS 服务，适合国内网络访问。
- **最快**：按 Genie 官方文档在本地部署，作者还提供 Windows 一键整合包。

部署完成后记下服务 URL（例如 `https://your-space.ms.show`），后面填进插件配置的 **TTS 服务器地址列表**。

👉 逐步操作、自定义模型与 `reference_audio` 的准备、Space 自动保活：**[部署 Genie TTS 服务](docs/deploy.md)**

---

## 📦 插件安装

- **方式一 (推荐)**: 在 AstrBot 的插件市场搜索 `astrbot_plugin_genie_tts_llm`，点击安装，等待完成即可。

- **方式二 (手动)**: 若安装失败，可尝试克隆源码。
  ```bash
  # 进入 AstrBot 插件目录
  cd /path/to/your/AstrBot/data/plugins

  # 克隆仓库
  git clone https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm.git

  # 重启 AstrBot
  ```

- **固定到某一个版本 (手动)**: 新版本有问题、想先退回上一版时用。可选的版本号见 [Releases](https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm/releases)，每个版本都有对应 tag。
  ```bash
  # 全新克隆指定版本（把 vX.Y.Z 换成你要的版本号）
  git clone --depth 1 --branch vX.Y.Z https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm.git

  # 已经克隆过的，切到那一版
  git fetch --tags && git checkout vX.Y.Z

  # 之后想回到最新
  git checkout main && git pull
  ```
  固定版本后目录处于 detached HEAD 状态，AstrBot 插件市场的「更新」按钮会失效（它走的是 `git pull`），要升级请先执行上面最后一条切回 `main`。

---

## 🚀 快速开始

1.  **部署服务与配置**：按 [部署 Genie TTS 服务](docs/deploy.md) 起好后端，在 AstrBot WebUI 的插件配置里填上 **TTS 服务器地址列表**（其余项都有默认值，可以先不动）。
2.  **注册情感**：使用 `/注册感情` 指令添加至少一个您想用的情感。对于一个角色，注册的情感越丰富，自动情感识别的效果就越好。<br>
    `示例: /注册感情 kisaki 开心 reference_audio/Kisaki_happy.ogg ほら、ホタルもとても喜んでいます。`
3.  **设置默认值**：回到 WebUI 配置，将“默认角色名”和“默认情感名”设为您刚刚注册的，并保存。
4.  **选择模式并开启**：
    *   **自动情感模式** (推荐)：发送 `/tts-w`。现在，机器人的LLM回复将自动分析情感并使用最匹配的语音！如果想换个角色，使用 `/sw-w <新角色名>`。
    *   **固定情感模式**：发送 `/tts-llm`。回复将使用默认或通过 `/sw` 指定的固定情感。
5.  **开始对话**：享受带声音的机器人对话吧！
6.  **关闭模式**：发送 `/tts-q` 即可恢复发送纯文本。

指令全表见 **[指令手册](docs/commands.md)**，每一项配置的含义见 **[配置项详解](docs/config.md)**，想在浏览器里点着做见 **[WebUI 工作台](docs/webui.md)**。

---

## 🛠️ 遇到问题

1. 先发 `/tts-status`：开关有没有开、当前角色与情感有没有注册成功、每台 TTS 服务器连不连得上（免费 Space 休眠唤醒要 1~3 分钟），一条消息全告诉你。
2. 再看工作台的 **▤ 日志** 页：每次合成的原文、译文、真正送进 TTS 的文本、情感来源、耗时与失败原因都在里面，还能按失败率排出「哪些情感不好」。
3. 对着现象查 **[故障排查](docs/troubleshooting.md)**：语音重叠、尾音被切、没有停顿、长回复念一半、一直超时、工作台空白等都收在里面。

---

## ⬆️ 更新日志

📜 [查看完整更新日志](CHANGELOG.md) · 🏷️ [Releases](https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm/releases)

每个版本都打了 tag 并建了对应 release，出问题想退回上一版可以直接固定版本安装（见 [插件安装](#-插件安装)）。想看两版之间到底改了什么，用仓库的 [compare 页](https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm/compare) 最直观。

## 📝 开发说明

本插件的开发过程得到了 AI 的大量协助。如果代码或功能中存在任何不妥之处，敬请谅解并通过 Issue 提出，感谢您的支持！

## 🤝 致谢

- 本插件的语音合成功能由 [**Genie TTS**](https://github.com/High-Logic/Genie) 库提供核心支持，由衷感谢原作者的杰出工作。
- 本 fork 基于 [clown145/astrbot_plugin_tts_llm](https://github.com/clown145/astrbot_plugin_tts_llm) 调整。


### 平静语气 + 自然停顿（Lidure 版 v1.11.2）

默认情感可选已注册的「平静」；勾选 **自动自然停顿**，句号等使用 `natural_pause_sentence_ms`，逗号等使用 `natural_pause_clause_ms`。推荐 550 / 160 ms。该功能不对 WAV 做降速处理，而是向支持 `[pause=ms]` 的 Genie 创空间发送标记；可随时关闭。参考 [ModelScope 部署说明](docs/deploy.md)。
