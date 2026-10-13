import asyncio
import os
import re
import uuid
import wave
import time
import hashlib
from collections import deque
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlparse

import httpx
from astrbot.api import logger, AstrBotConfig

try:
    from .audio_repeat_guard import classify_audio_repeat
except ImportError:
    from audio_repeat_guard import classify_audio_repeat

# --- 音频参数 (必须与Genie TTS服务输出匹配) ---
BYTES_PER_SAMPLE = 2
CHANNELS = 1
SAMPLE_RATE = 32000

# --- 参考音频泄漏防护 ---
# Genie 的 t2s 解码在“整段文本没有可发音字符”时会退化成输出整条参考音频的
# semantic tokens，最终合成出“参考音频 + 新语音”拼在一起的音频。
# 服务端已修复该缺陷，插件侧同样做两层兜底：
#   1) 切分阶段就把纯标点/纯符号片段并入相邻片段，不让它单独进入合成；
#   2) 合成后按“可发音字符数”估算时长上限，明显超标时重试并取较短结果。
PRONOUNCEABLE_PATTERN = re.compile(
    "["
    "0-9A-Za-z"
    "\u00c0-\u024f"  # 拉丁字母补充 + 拉丁扩展 A
    "\u0370-\u04ff"  # 希腊字母 + 西里尔字母
    "\u1100-\u11ff"  # 谚文字母
    "\u3005-\u3007"  # 々 〆 〇
    "\u3041-\u3096"  # 平假名
    "\u3105-\u312f"  # 注音符号
    "\u3131-\u318e"  # 谚文兼容字母
    "\u30a1-\u30fa"  # 片假名（不含长音符 ー 与中点 ・）
    "\u31f0-\u31ff"  # 片假名语音扩展
    "\u3400-\u4dbf"  # CJK 扩展 A
    "\u4e00-\u9fff"  # CJK 基本区
    "\uac00-\ud7a3"  # 谚文音节
    "\uf900-\ufaff"  # CJK 兼容表意文字
    "\uff10-\uff19"  # 全角数字
    "\uff21-\uff3a"  # 全角大写拉丁字母
    "\uff41-\uff5a"  # 全角小写拉丁字母
    "\uff66-\uff9d"  # 半角片假名
    "]"
)
PAUSE_MARKER_PATTERN = re.compile(r"\[pause\s*=\s*(\d+)\s*(?:ms)?\]", re.IGNORECASE)
MAX_CUSTOM_PAUSE_MS = 3000
DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR = 0.9
DEFAULT_LEAK_GUARD_MIN_SECONDS = 3.0

# --- 截断防护 ---
# Space 端的 T2S 采样没有固定随机种子，同一句话偶尔会在第一步就吐出 EOS，那个
# 句段会被服务端静默丢掉：落在中间就吞掉一个分句，落在最后就是「话没说完突然
# 结束」。根治在 Space（同一句段重新采样），这里是兜底——结果明显短于文本应有
# 的长度就重合成一次。日语实测最快 0.148s/可发音字，默认阈值取其一半，留足 30%
# 以上余量，只抓量级异常，不误伤语速快的短句。
DEFAULT_TRUNCATION_GUARD_SECONDS_PER_CHAR = 0.10
MAX_TRUNCATION_GUARD_SECONDS_PER_CHAR = 0.5
# 少于这个可发音字数时单字时长波动太大，判不准，直接放行（整段为空另有兜底）。
MIN_TRUNCATION_GUARD_CHARS = 4

# --- 尾部补静音 ---
# Space 有意丢掉每次请求末尾的句间停顿，成品最后一个音几乎贴着文件结尾；QQ 这
# 类平台还会把 WAV 转成 silk 再播放，实测尾帧偶尔被吃掉。补一小段静音是最便宜
# 的保险，不改变已合成的语音内容。
DEFAULT_TAIL_PADDING_MS = 180
MAX_TAIL_PADDING_MS = 2000

# --- 超长文本截断 ---
# 默认 300 字：够装下绝大多数一次性回复，又不会让一条消息排出十几个分块。历史默认
# 值是 150，长回复经常在半路被砍掉，听起来就是「话没说完」。
DEFAULT_MAX_TEXT_LENGTH = 300
# tts_max_text_length 之外的内容会被直接丢掉（只影响送入 TTS 的文本，展示的文字
# 仍是完整原文）。切在句末标点上听起来是「说完了」，切在逗号/顿号上听起来就是
# 「话没说完突然断了」，所以两类标点必须分开优先级：先找句末，找不到才退让到软
# 停顿，最后才硬切。
STRONG_TRUNCATE_BOUNDARY_CHARS = "\u3002\uff01\uff1f!?\u2026\uff0e.\n"
SOFT_TRUNCATE_BOUNDARY_CHARS = "\u3001\uff0c,\uff1b;\uff1a:"
# 句末标点后面常跟收尾的引号/括号，一起带上才不会把「」拆散。
TRUNCATE_TRAILING_CLOSERS = "\u300d\u300f\u3011\uff09\u300b\u201d\u2019\"'"

# --- 分段拼接的停顿补偿 ---
# Space 只在单次请求内部插入句间停顿，请求最后一句欠的那一拍会被有意丢掉
# （否则每条语音结尾都会多出一段死气）。开启句子切分后一条消息会被拆成多次
# 请求再拼接，这时如果不补上这一拍，分段边界就完全没有停顿，听起来像一口气
# 念完。这里按上一块的结尾标点决定补多久。
DEFAULT_CHUNK_GAP_MS = 260
MAX_CHUNK_GAP_MS = 2000
CHUNK_SENTENCE_END_CHARS = "。．！？.!?…"
CHUNK_SOFT_BREAK_CHARS = "、，,；;：:"
CHUNK_TRAILING_TRIM_CHARS = " \t\r\n」』】）》”’\"'"

# Space 端自动插入的停顿时长，仅用于泄漏防护的时长预算，需与 app.py 的
# GENIE_SENTENCE_PAUSE_MS / GENIE_ELLIPSIS_PAUSE_MS 默认值保持一致。
AUTO_SENTENCE_PAUSE_MS = 260
AUTO_ELLIPSIS_PAUSE_MS = 420
AUTO_PAUSE_BOUNDARY_PATTERN = re.compile(r"…+|\.{2,}|[。．！？.!?]+|[\r\n]+")

# 自动自然停顿：句子末尾和逗号后的轻停顿，只在后面还有可读文字时插入。
# 只插入标记，不改动语音的音高/时间尺度；由支持 [pause=ms] 的 Genie Space 实施。
NATURAL_PAUSE_BOUNDARY_PATTERN = re.compile(
    r"""[。！？!?…]+[」』】）》”’"']*|[、，,]+"""
)
MAX_NATURAL_PAUSE_MS = 1200


def inject_natural_pauses(text: str, sentence_ms: int = 500, clause_ms: int = 160) -> str:
    """在句中标点后插入可配置停顿；不叠加已有的 [pause=ms] 标记。

    - 句末不追加暂停（尾音留白由 _append_tail_padding 处理）
    - 不在重复标点、闭合引号、显式停顿标记旁边重复插入
    - 只处理有下一个可读片段的边界，不处理英文小数点
    """
    if not text:
        return text
    try:
        sentence_ms = max(0, min(int(sentence_ms), MAX_NATURAL_PAUSE_MS))
        clause_ms = max(0, min(int(clause_ms), MAX_NATURAL_PAUSE_MS))
    except (TypeError, ValueError, OverflowError):
        return text
    result = []
    cursor = 0
    for found in NATURAL_PAUSE_BOUNDARY_PATTERN.finditer(text):
        result.append(text[cursor:found.end()])
        cursor = found.end()
        remaining = text[cursor:].lstrip()
        if not remaining or remaining.lower().startswith("[pause"):
            continue
        if not has_pronounceable(remaining):
            continue
        punctuation = found.group(0)
        is_clause = punctuation[0] in "、，,"
        duration = clause_ms if is_clause else sentence_ms
        if duration:
            result.append(f"[pause={duration}]")
    result.append(text[cursor:])
    return "".join(result)



def strip_pause_markers(text: str) -> str:
    """剥掉 [pause=ms] 标记本身，只留下真正会被念出来的文本。

    标记永远不会被朗读（关闭时在送入 TTS 前剥除，开启时由 Space 换成静音），但
    "pause" 和后面的数字本身落在可发音字符集里。不先剥掉的话：一条只有标记的
    消息会被判成「有内容」送去合成（正好触发参考音频泄漏），带标记的正常句子也
    会把可发音字数算大，让截断防护误判偏短、白白重试一次。
    """
    return PAUSE_MARKER_PATTERN.sub(" ", text or "")


def has_pronounceable(text: str) -> bool:
    """判断文本里是否存在至少一个可发音字符（不含 [pause] 标记自身）。"""
    return bool(PRONOUNCEABLE_PATTERN.search(strip_pause_markers(text)))


def count_pronounceable(text: str) -> int:
    """统计可发音字符数量（不含 [pause] 标记自身），用于估算合理音频时长。"""
    return len(PRONOUNCEABLE_PATTERN.findall(strip_pause_markers(text)))


def pause_budget_seconds(text: str) -> float:
    """统计 [pause=ms] 标记声明的静音总时长（秒）。"""
    total_ms = 0
    for raw in PAUSE_MARKER_PATTERN.findall(text or ""):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        total_ms += max(0, min(value, MAX_CUSTOM_PAUSE_MS))
    return total_ms / 1000.0


def auto_pause_budget_seconds(text: str) -> float:
    """估算 Space 会自动插入的句间静音总时长（秒）。

    Space 在句末标点后插入约 260ms、省略号后约 420ms 的静音，这是刻意加上的
    呼吸感，不该被算进"参考音频泄漏"的判定里，否则停顿密集的文本（大量省略号
    或换行）会被泄漏防护误伤、白白重试一次。刻意算得偏宽，只作为上限。
    """
    total_ms = 0
    for match in AUTO_PAUSE_BOUNDARY_PATTERN.finditer(text or ""):
        token = match.group(0)
        if "…" in token or token.count(".") >= 2:
            total_ms += AUTO_ELLIPSIS_PAUSE_MS
        else:
            total_ms += AUTO_SENTENCE_PAUSE_MS
    return total_ms / 1000.0


def _modelscope_auth_headers(server_url: str, config) -> dict[str, str]:
    """Authenticate Genie endpoints only on genuine ModelScope inference hosts.

    Public ModelScope Space URLs (*.ms.show) work without this token; never
    forward ModelScope tokens to a user-supplied public Space or another host.
    """
    parsed = urlparse(str(server_url))
    if parsed.scheme.lower() != "https":
        return {}
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not (hostname == "api-inference.modelscope.net" or
            hostname.endswith(".api-inference.modelscope.net")):
        return {}
    token = str(config.get("modelscope_api_token", "") or
                os.environ.get("MODELSCOPE_API_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


class TTSEngine:
    """处理所有与TTS合成相关的核心逻辑，包括文本分块、并发合成和音频合并"""

    DEFAULT_TTS_CLEAN_REGEX = (
        r"\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|【[^【】]*】|\{[^{}]*\}|｛[^｛｝]*｝|<[^<>]*>|《[^《》]*》"
    )
    MAX_TTS_CLEAN_ROUNDS = 10

    def __init__(
        self,
        config: AstrBotConfig,
        http_client: httpx.AsyncClient,
        plugin_data_dir: Path,
    ):
        self.config = config
        self.http_client = http_client
        self.plugin_data_dir = plugin_data_dir
        self.tts_server_index = 0

        # 服务器锁：确保同一服务器的 set_reference_audio + /tts 是原子操作
        self._server_locks: Dict[str, asyncio.Lock] = {}

        # 全局合成锁：确保多个语音请求按"先到先得"顺序串行处理
        self._synthesis_lock = asyncio.Lock()

        # 设置临时音频目录
        self.temp_audio_dir = self.plugin_data_dir / "temp_audio"
        self.temp_audio_dir.mkdir(parents=True, exist_ok=True)

        # 运行统计，供 /tts-status 展示
        self.stats: Dict[str, int] = {
            "requests": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_no_speech": 0,
            "leak_guard_hits": 0,
            "truncation_guard_hits": 0,
            "text_truncated": 0,
            "empty_result_retries": 0,
            "audio_repeat_guard_hits": 0,
        }

        # Only bounded RAM fingerprints, no persisted or uploaded voice content.
        self._recent_audio_signatures = deque(maxlen=6)

        # 启动清理任务
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        # 全局 FIFO 请求队列：严格先到先得
        self._request_queue = asyncio.Queue()
        self._request_worker_task = asyncio.create_task(self._request_worker_loop())

    # ------------------------------------------------------------------ 工具

    def _server_urls(self) -> list[str]:
        """读取并规范化已配置的 TTS 服务器列表（去空、去尾斜杠、去重）。"""
        raw_servers = self.config.get("tts_servers", []) or []
        urls: list[str] = []
        for item in raw_servers:
            url = str(item or "").strip().strip("/")
            if url and url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _discard_temp_file(path: Optional[str]) -> None:
        """安全删除临时音频文件，忽略文件不存在的情况。"""
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(f"删除临时文件 {path} 失败: {exc}")

    def _clean_text_for_tts(self, text: str) -> str:
        cleaned = text

        pattern = self.config.get("tts_text_clean_regex", self.DEFAULT_TTS_CLEAN_REGEX)
        if isinstance(pattern, str) and pattern:
            try:
                # Run multiple rounds so nested brackets can also be cleaned.
                for _ in range(self.MAX_TTS_CLEAN_ROUNDS):
                    updated = re.sub(pattern, "", cleaned)
                    if updated == cleaned:
                        break
                    cleaned = updated
            except re.error as exc:
                logger.warning(f"文本清洗正则无效，已跳过: {pattern} ({exc})")

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _ensure_terminal_punctuation(self, text: str, language: str = None) -> str:
        text = (text or "").strip()
        if not text:
            return text

        closing_marks = "\"'”’）)]】』」》"
        terminal_pattern = rf"[。！？!?\.．…]+[{re.escape(closing_marks)}]*$"
        if re.search(terminal_pattern, text):
            return text

        language_code = str(language or self.config.get("tts_default_language", "jp") or "jp").lower()
        punctuation = "." if language_code in {"en", "english"} else "。"
        closing_match = re.search(rf"([{re.escape(closing_marks)}]+)$", text)
        if closing_match:
            insert_at = closing_match.start()
            return f"{text[:insert_at]}{punctuation}{text[insert_at:]}"
        return f"{text}{punctuation}"

    def _truncate_text_for_tts(self, text: str, max_length: int) -> str:
        """将过长文本按句子边界截断，避免超长回复被切成过多块、长时间排队等待。

        仅影响送入 TTS 的音频文本；audio_and_text 模式下展示给用户的文字仍是完整原文。
        优先切在句末标点上；句末标点太靠前才退让到逗号一类的软停顿，并把软停顿本身
        去掉（后面会由 _ensure_terminal_punctuation 补上句号），否则听起来就是「话没
        说完突然断了」；两档都不可用才硬切。
        """
        text = (text or "").strip()
        if max_length <= 0 or len(text) <= max_length:
            return text

        window = text[:max_length]
        # 边界过于靠前会丢掉太多内容，低于这个下限就换下一档策略。
        floor = max(1, max_length // 2)
        for boundary_chars, keep_mark in (
            (STRONG_TRUNCATE_BOUNDARY_CHARS, True),
            (SOFT_TRUNCATE_BOUNDARY_CHARS, False),
        ):
            hit = -1
            for idx in range(len(window) - 1, -1, -1):
                if window[idx] in boundary_chars:
                    hit = idx
                    break
            if hit < 0 or hit + 1 < floor:
                continue
            cut_at = hit + 1 if keep_mark else hit
            if keep_mark:
                # 句末标点后常跟收尾的引号/括号，一起带上才不会把「」拆散。
                while (
                    cut_at < len(window)
                    and window[cut_at] in TRUNCATE_TRAILING_CLOSERS
                ):
                    cut_at += 1
            truncated = window[:cut_at].strip()
            if truncated:
                return truncated

        return window.strip()

    async def _cleanup_loop(self):
        """定期清理过期的临时音频文件"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时检查一次
                current_time = time.time()
                expiration_time = 1800  # 30分钟过期

                if not self.temp_audio_dir.exists():
                    continue

                count = 0
                for file_path in self.temp_audio_dir.glob("*.wav"):
                    try:
                        if current_time - file_path.stat().st_mtime > expiration_time:
                            file_path.unlink()
                            count += 1
                    except Exception as e:
                        logger.warning(f"清理文件 {file_path} 失败: {e}")

                if count > 0:
                    logger.info(f"已清理 {count} 个过期的临时音频文件。")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务发生错误: {e}")

    async def _request_worker_loop(self):
        """全局请求调度器：严格按 FIFO 处理整单合成请求。"""
        while True:
            try:
                request, result_future = await self._request_queue.get()
            except asyncio.CancelledError:
                break

            if result_future.cancelled():
                logger.info(
                    f"[{request.get('session_id_for_log', 'unknown')}] 请求已取消，跳过合成。"
                )
                self._request_queue.task_done()
                continue

            try:
                result = await self._synthesize_direct(**request)
                if not result_future.done():
                    result_future.set_result(result)
            except asyncio.CancelledError:
                if not result_future.done():
                    result_future.set_result(None)
                raise
            except Exception as e:
                logger.error(f"[{request.get('session_id_for_log', 'unknown')}] 请求处理异常: {e}")
                if not result_future.done():
                    result_future.set_result(None)
            finally:
                self._request_queue.task_done()

    async def terminate(self):
        """在插件卸载时停止后台清理任务。"""
        if self._request_worker_task:
            self._request_worker_task.cancel()
            await asyncio.gather(self._request_worker_task, return_exceptions=True)
            while not self._request_queue.empty():
                try:
                    _, result_future = self._request_queue.get_nowait()
                    if not result_future.done():
                        result_future.set_result(None)
                    self._request_queue.task_done()
                except asyncio.QueueEmpty:
                    break

        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)

    # -------------------------------------------------------------- 文本切分

    @staticmethod
    def _merge_unvoiceable_sentences(sentences: list[str]) -> list[str]:
        """把不含可发音字符的片段并入相邻片段。

        纯标点片段（例如被切出来的单独 "……"）单独送进 Genie 会触发参考音频泄漏，
        因此这里把它并到后一个可发音片段前面；若结尾仍有残留则追加到最后一段末尾。
        整段都没有可发音字符时返回空列表，表示这次不该合成。
        """
        merged: list[str] = []
        pending = ""
        for sentence in sentences:
            if not sentence:
                continue
            if has_pronounceable(sentence):
                merged.append(f"{pending}{sentence}" if pending else sentence)
                pending = ""
            else:
                pending += sentence

        if pending:
            if not merged:
                return []
            merged[-1] = f"{merged[-1]}{pending}"
        return merged

    def _split_text_into_chunks(self, text: str, sentences_per_chunk: int) -> list[str]:
        """根据标点将文本切分为句子，再按指定数量合并成块。

        返回空列表表示整段文本没有任何可发音内容，调用方应放弃本次合成。
        """
        if not has_pronounceable(text):
            return []

        if sentences_per_chunk <= 0:
            return [text]

        regex_pattern = self.config.get("sentence_split_regex", r"([。、，！？,.!?])")
        try:
            splitter = re.compile(regex_pattern)
        except re.error as exc:
            logger.warning(f"句子切分正则无效，已退回单块模式: {regex_pattern} ({exc})")
            return [text]

        sentences = splitter.split(text)
        if not sentences:
            return []

        if splitter.groups == 1:
            # 带一个捕获组时 re.split 交替返回 [正文, 分隔符, 正文, ...]
            full_sentences = []
            for i in range(0, len(sentences) - 1, 2):
                # 修复：分隔符不再因为正文为空而被整个丢弃（例如连续的 "！？"）
                merged = f"{sentences[i]}{sentences[i + 1]}"
                if merged:
                    full_sentences.append(merged)
            if len(sentences) % 2 == 1 and sentences[-1]:
                full_sentences.append(sentences[-1])
        else:
            # 无捕获组（分隔符已被丢弃）或多捕获组时，按原顺序保留非空片段
            full_sentences = [part for part in sentences if part]

        full_sentences = self._merge_unvoiceable_sentences(full_sentences)
        if not full_sentences:
            return []

        chunks = []
        for i in range(0, len(full_sentences), sentences_per_chunk):
            chunk = "".join(full_sentences[i : i + sentences_per_chunk])
            chunks.append(chunk)

        logger.info(f"文本已切分为 {len(chunks)} 个块。")
        return chunks

    def _chunk_gap_ms(self) -> int:
        """分段拼接时补在块之间的静音基准时长（毫秒），已 clamp 到合法区间。"""
        raw = self.config.get("chunk_gap_ms", DEFAULT_CHUNK_GAP_MS)
        try:
            gap_ms = int(raw)
        except (TypeError, ValueError):
            gap_ms = DEFAULT_CHUNK_GAP_MS
        return max(0, min(gap_ms, MAX_CHUNK_GAP_MS))

    def _tail_padding_ms(self) -> int:
        """补在成品语音末尾的静音时长（毫秒），已 clamp 到合法区间。"""
        raw = self.config.get("tts_tail_padding_ms", DEFAULT_TAIL_PADDING_MS)
        try:
            padding_ms = int(raw)
        except (TypeError, ValueError):
            padding_ms = DEFAULT_TAIL_PADDING_MS
        return max(0, min(padding_ms, MAX_TAIL_PADDING_MS))

    @staticmethod
    def _boundary_gap_ms(previous_chunk: Optional[str], gap_ms: int) -> int:
        """按上一块的结尾标点决定这个拼接点该补多久静音。

        句末标点 / 省略号 → 补满；逗号顿号这类软停顿 → 补一半；切在句子中间
        （自定义切分正则可能不带标点）→ 不补，硬塞静音只会造成结巴感。
        """
        if gap_ms <= 0:
            return 0
        if previous_chunk is None:
            return gap_ms
        tail = previous_chunk.rstrip(CHUNK_TRAILING_TRIM_CHARS)
        if not tail:
            return gap_ms
        if tail[-1] in CHUNK_SENTENCE_END_CHARS:
            return gap_ms
        if tail[-1] in CHUNK_SOFT_BREAK_CHARS:
            return gap_ms // 2
        return 0

    async def _merge_wav_files(
        self, input_paths: list[str], chunk_texts: Optional[list[str]] = None
    ) -> Optional[str]:
        """以无损的方式将多个WAV文件按顺序合并为一个，并清理分块文件。

        块之间会按 chunk_gap_ms 补一段静音：Space 会丢掉每次请求末尾的句间
        停顿，逐块裸拼会让分段边界的停顿归零，整条语音听起来像一口气念完。
        采样参数一律取自实际音频，不用模块常量，避免服务端换采样率后错位。
        """
        if not input_paths:
            return None

        output_path = self.temp_audio_dir / f"{uuid.uuid4()}_merged.wav"

        try:
            with wave.open(input_paths[0], "rb") as wf_in:
                params = wf_in.getparams()

            base_gap_ms = self._chunk_gap_ms()
            frame_bytes = params.nchannels * params.sampwidth
            inserted_ms = 0

            with wave.open(str(output_path), "wb") as wf_out:
                wf_out.setparams(params)
                for index, file_path in enumerate(input_paths):
                    if index:
                        previous = (
                            chunk_texts[index - 1]
                            if chunk_texts and index - 1 < len(chunk_texts)
                            else None
                        )
                        gap_ms = self._boundary_gap_ms(previous, base_gap_ms)
                        if gap_ms:
                            gap_frames = round(params.framerate * gap_ms / 1000)
                            wf_out.writeframes(b"\x00" * (gap_frames * frame_bytes))
                            inserted_ms += gap_ms
                    with wave.open(file_path, "rb") as wf_in:
                        wf_out.writeframes(wf_in.readframes(wf_in.getnframes()))

            logger.info(
                f"成功将 {len(input_paths)} 个音频文件合并到: {output_path}"
                f"（块间共补 {inserted_ms}ms 停顿）"
            )
            return str(output_path)

        except Exception as e:
            logger.error(f"合并WAV文件时出错: {e}")
            self._discard_temp_file(str(output_path))
            return None
        finally:
            # 无论合并成功与否，都尝试清理输入的临时分块文件
            for file_path in input_paths:
                self._discard_temp_file(file_path)

    def _append_tail_padding(
        self, audio_path: Optional[str], session_id_for_log: str
    ) -> Optional[str]:
        """在成品 WAV 末尾补一小段静音，防止最后一个字被播放端吞掉。

        Space 刻意丢掉每次请求末尾的句间停顿，所以最后一个音几乎贴着文件结尾；
        再叠上 QQ 的 silk 转码和播放器的收尾，尾音听起来就像被硬切。补静音不会
        改变已合成的语音内容，失败时沿用原音频，绝不因为补静音把整条语音搞没。
        """
        padding_ms = self._tail_padding_ms()
        if not audio_path or padding_ms <= 0:
            return audio_path

        padded_path = self.temp_audio_dir / f"{uuid.uuid4()}_tail.wav"
        try:
            with wave.open(audio_path, "rb") as wf_in:
                params = wf_in.getparams()
                frames = wf_in.readframes(wf_in.getnframes())
            pad_frames = round(params.framerate * padding_ms / 1000)
            if pad_frames <= 0:
                return audio_path
            frame_bytes = params.nchannels * params.sampwidth
            with wave.open(str(padded_path), "wb") as wf_out:
                wf_out.setparams(params)
                wf_out.writeframes(frames)
                wf_out.writeframes(b"\x00" * (pad_frames * frame_bytes))
        except Exception as e:
            logger.warning(
                f"[{session_id_for_log}] 尾部补静音失败，沿用原音频: {e}"
            )
            self._discard_temp_file(str(padded_path))
            return audio_path

        self._discard_temp_file(audio_path)
        return str(padded_path)

    def _get_server_lock(self, server_url: str) -> asyncio.Lock:
        """获取或创建指定服务器的锁，用于保证 set_reference_audio + /tts 的原子性。"""
        if server_url not in self._server_locks:
            self._server_locks[server_url] = asyncio.Lock()
        return self._server_locks[server_url]

    # ---------------------------------------------------------------- 合成实现

    def _leak_guard_enabled(self) -> bool:
        return bool(self.config.get("enable_tts_leak_guard", True))

    def _expected_audio_seconds(self, text: str) -> float:
        """按可发音字符数 + 停顿时长估算这段文本合理的最大音频时长（秒）。

        阈值刻意放得很宽（默认 0.9s/字，约为正常语速的 6 倍），只用于识别
        “整条参考音频被拼进结果”这类量级异常，不会误伤正常的慢速朗读。
        """
        per_char = self.config.get(
            "tts_leak_guard_seconds_per_char", DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR
        )
        floor_seconds = self.config.get(
            "tts_leak_guard_min_seconds", DEFAULT_LEAK_GUARD_MIN_SECONDS
        )
        try:
            per_char = float(per_char)
        except (TypeError, ValueError):
            per_char = DEFAULT_LEAK_GUARD_SECONDS_PER_CHAR
        try:
            floor_seconds = float(floor_seconds)
        except (TypeError, ValueError):
            floor_seconds = DEFAULT_LEAK_GUARD_MIN_SECONDS

        per_char = max(per_char, 0.2)
        floor_seconds = max(floor_seconds, 1.0)
        budget = (
            count_pronounceable(text) * per_char
            + pause_budget_seconds(text)
            + auto_pause_budget_seconds(text)
        )
        return max(floor_seconds, budget)

    def _truncation_guard_enabled(self) -> bool:
        return bool(self.config.get("enable_tts_truncation_guard", True))

    def _minimum_audio_seconds(self, text: str) -> float:
        """按可发音字符数估算这段文本合理的最低音频时长（秒）。

        只用来识别「服务端漏掉了整个句段 / 采样提前吐 EOS」这类明显缺字的结果。
        刻意不把文本自带的停顿算进下限：停顿是静音，缺了不影响「字有没有念全」，
        算进来只会抬高阈值、把正常结果误判成截断。
        """
        per_char = self.config.get(
            "tts_truncation_guard_seconds_per_char",
            DEFAULT_TRUNCATION_GUARD_SECONDS_PER_CHAR,
        )
        try:
            per_char = float(per_char)
        except (TypeError, ValueError):
            per_char = DEFAULT_TRUNCATION_GUARD_SECONDS_PER_CHAR
        per_char = max(0.0, min(per_char, MAX_TRUNCATION_GUARD_SECONDS_PER_CHAR))
        voiced = count_pronounceable(text)
        if per_char <= 0 or voiced < MIN_TRUNCATION_GUARD_CHARS:
            return 0.0
        return voiced * per_char

    @staticmethod
    def _duration_penalty(
        seconds: float, minimum_seconds: float, expected_seconds: float
    ) -> tuple[int, float]:
        """给一次合成结果的时长打分，元组越小越好。

        (2, 超出量)：过长，疑似整条参考音频被拼进结果，用户会听到「上次的语音
                     ＋这次的」，这是最坏的情况；
        (1, 缺失量)：过短，疑似句段被服务端漏掉或采样提前结束，会缺字；
        (0, 0.0)  ：落在合理区间。

        同一档内取偏离更小的一次，于是「两次都偏长取更短的」「两次都偏短取更长
        的」由同一个比较式覆盖，两个防护也不会互相打架。
        """
        if expected_seconds > 0 and seconds > expected_seconds:
            return 2, seconds - expected_seconds
        if minimum_seconds > 0 and seconds < minimum_seconds:
            return 1, minimum_seconds - seconds
        return 0, 0.0

    async def _stream_tts_to_file(
        self,
        server_url: str,
        character_name: str,
        text: str,
        tts_timeout: float,
        session_id_for_log: str,
    ) -> tuple[Optional[str], float]:
        """调用一次 /tts 并把裸 PCM 流封装成 WAV 落盘，返回 (文件路径, 音频秒数)。

        /tts 返回 0 字节时（服务端判定整段文本无可发音内容）返回 (None, 0.0)，
        绝不能把 0 帧的空 WAV 当成合成成功，否则用户会收到一条空语音。
        """
        tts_payload = {
            "character_name": character_name,
            "text": text,
            "split_sentence": self.config.get("tts_server_split_sentence", False) is True,
        }
        output_path = self.temp_audio_dir / f"{uuid.uuid4()}.wav"
        tts_start = time.perf_counter()
        first_byte_elapsed = None
        total_audio_bytes = 0

        try:
            async with self.http_client.stream(
                "POST", f"{server_url}/tts", json=tts_payload,
                headers=_modelscope_auth_headers(server_url, self.config),
                timeout=tts_timeout
            ) as response_tts:
                response_tts.raise_for_status()
                with wave.open(str(output_path), "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(BYTES_PER_SAMPLE)
                    wf.setframerate(SAMPLE_RATE)
                    async for chunk in response_tts.aiter_bytes():
                        if not chunk:
                            continue
                        if first_byte_elapsed is None:
                            first_byte_elapsed = time.perf_counter() - tts_start
                        total_audio_bytes += len(chunk)
                        wf.writeframes(chunk)
        except BaseException:
            # 任何异常（含取消）都不留下半截临时文件
            self._discard_temp_file(str(output_path))
            raise

        total_elapsed = time.perf_counter() - tts_start
        audio_seconds = total_audio_bytes / float(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)
        ttfb_text = (
            f"{first_byte_elapsed:.2f}s" if first_byte_elapsed is not None else "无音频"
        )
        logger.info(
            f"[{session_id_for_log}] TTS服务器 {server_url} 计时 | "
            f"首包响应 {ttfb_text} | 合成传输 {total_elapsed:.2f}s | "
            f"音频时长 {audio_seconds:.2f}s"
        )

        if total_audio_bytes <= 0:
            self._discard_temp_file(str(output_path))
            logger.warning(
                f"[{session_id_for_log}] TTS服务器 {server_url} 未返回任何音频数据，"
                f"已丢弃空音频（文本可能只有标点或表情符号）。"
            )
            return None, 0.0

        return str(output_path), audio_seconds

    async def _attempt_synthesis_on_server(
        self,
        server_url: str,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """Try this server twice if audio is abnormal; never send known-bad loops."""
        logger.info(f"[{session_id_for_log}] 尝试TTS服务器: {server_url}")
        server_lock = self._get_server_lock(server_url)
        best_path: Optional[str] = None
        best_signature = None
        best_penalty: Optional[tuple[int, float]] = None
        best_seconds = 0.0
        async with server_lock:
            try:
                if not language:
                    language = self.config.get("tts_default_language", "jp")
                ref_payload = {
                    "character_name": character_name,
                    "audio_path": ref_audio_path,
                    "audio_text": ref_audio_text,
                    "language": language,
                }
                tts_timeout = self.config.get("tts_timeout_seconds", 120)
                ref_start = time.perf_counter()
                response = await self.http_client.post(
                    f"{server_url}/set_reference_audio",
                    json=ref_payload,
                    headers=_modelscope_auth_headers(server_url, self.config),
                    timeout=60,
                )
                response.raise_for_status()
                logger.info(
                    f"[{session_id_for_log}] 参考音频设定完成 | "
                    f"{time.perf_counter() - ref_start:.2f}s"
                )

                leak_guard = self._leak_guard_enabled()
                expected_seconds = self._expected_audio_seconds(text) if leak_guard else 0.0
                truncation_guard = self._truncation_guard_enabled()
                minimum_seconds = (
                    self._minimum_audio_seconds(text) if truncation_guard else 0.0
                )
                repeat_guard = (
                    self.config.get("enable_tts_audio_repeat_guard", True) is True
                )
                text_digest = hashlib.sha256(text.encode("utf-8")).digest()
                recent = getattr(self, "_recent_audio_signatures", ())
                prior_signatures = [
                    signature
                    for saved_character, saved_digest, signature in recent
                    if saved_character == character_name and saved_digest != text_digest
                ] if repeat_guard else []

                # Only two synthesis attempts per server. A rejected recording
                # is deleted rather than being clipped (which loses real speech).
                for attempt in range(2):
                    audio_path, audio_seconds = await self._stream_tts_to_file(
                        server_url,
                        character_name,
                        text,
                        tts_timeout,
                        session_id_for_log,
                    )
                    if not audio_path:
                        if attempt == 0 and best_path is None:
                            self.stats["empty_result_retries"] += 1
                            logger.warning(
                                f"[{session_id_for_log}] 合成未返回音频，重试一次。"
                            )
                            continue
                        break

                    signature = None
                    if repeat_guard:
                        repeat_kind, signature = classify_audio_repeat(
                            audio_path, prior_signatures
                        )
                        if repeat_kind:
                            self.stats["audio_repeat_guard_hits"] += 1
                            logger.warning(
                                f"[{session_id_for_log}] 音频重复防护检测到"
                                f"{'同条语音循环' if repeat_kind == 'internal' else '跨请求固定台词重复'}；"
                                + ("已丢弃并重试一次。" if attempt == 0
                                   else "重试后仍重复，拒绝发送。")
                            )
                            self._discard_temp_file(audio_path)
                            continue

                    penalty = self._duration_penalty(
                        audio_seconds, minimum_seconds, expected_seconds
                    )
                    if best_penalty is None or penalty < best_penalty:
                        self._discard_temp_file(best_path)
                        best_path = audio_path
                        best_penalty = penalty
                        best_signature = signature
                        best_seconds = audio_seconds
                    else:
                        self._discard_temp_file(audio_path)

                    if best_penalty[0] == 0:
                        if best_signature is not None:
                            self._recent_audio_signatures.append(
                                (character_name, text_digest, best_signature)
                            )
                        return best_path

                    if penalty[0] == 2:
                        if attempt == 0:
                            self.stats["leak_guard_hits"] += 1
                        logger.warning(
                            f"[{session_id_for_log}] 合成结果异常偏长"
                            f"（{audio_seconds:.2f}s > 上限 {expected_seconds:.2f}s）；"
                            + ("正在重试一次。" if attempt == 0
                               else "两次均异常，拒绝发送较长的结果。")
                        )
                    else:
                        if attempt == 0:
                            self.stats["truncation_guard_hits"] += 1
                        logger.warning(
                            f"[{session_id_for_log}] 合成结果异常偏短"
                            f"（{audio_seconds:.2f}s < 下限 {minimum_seconds:.2f}s）；"
                            + ("正在重试一次。" if attempt == 0
                               else "采用较接近完整长度的一份。")
                        )

                if best_path is None:
                    return None
                if best_penalty is not None and best_penalty[0] == 2:
                    # Old behavior sent the shorter of two overlong takes,
                    # even though both could contain reference-audio leakage.
                    logger.warning(
                        f"[{session_id_for_log}] 最短一次仍异常偏长"
                        f"（{best_seconds:.2f}s），已丢弃而非发送重复语音。"
                    )
                    self._discard_temp_file(best_path)
                    return None

                if best_signature is not None:
                    self._recent_audio_signatures.append(
                        (character_name, text_digest, best_signature)
                    )
                return best_path

            except Exception as exc:
                logger.warning(
                    f"[{session_id_for_log}] TTS服务器 {server_url} 交互失败: {exc}"
                )
                self._discard_temp_file(best_path)
                return None

    async def _synthesis_worker(
        self,
        worker_id: int,
        task_queue: asyncio.Queue,
        results_list: list,
        retry_counts: dict,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        session_id_for_log: str,
        language: str = None,
    ):
        """单个TTS服务器的工作进程，从队列中获取任务并处理，失败时会重试"""
        servers = self._server_urls()
        num_servers = len(servers)
        if num_servers == 0:
            return
        max_retries = self.config.get("tts_max_retries", 3)

        while True:
            try:
                # 使用非阻塞方式获取任务，如果队列为空则退出
                task_index, chunk_text = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                # 队列真正为空，退出循环
                break
            except asyncio.CancelledError:
                break

            current_retry = retry_counts.get(task_index, 0)
            start_server_idx = worker_id % num_servers
            log_id = f"{session_id_for_log}-chunk-{task_index + 1}"
            audio_path = None

            # 第一轮 prefer_idle=True：只挑当前空闲的服务器，立刻开工；
            # 第二轮 prefer_idle=False：所有服务器都忙，则按顺序排队等待。
            for prefer_idle in (True, False):
                for offset in range(num_servers):
                    server_url = servers[(start_server_idx + offset) % num_servers]
                    if prefer_idle and self._get_server_lock(server_url).locked():
                        continue

                    audio_path = await self._attempt_synthesis_on_server(
                        server_url,
                        character_name,
                        ref_audio_path,
                        ref_audio_text,
                        chunk_text,
                        log_id,
                        language=language,
                    )
                    if audio_path:
                        suffix = "" if prefer_idle else " (等待后)"
                        logger.info(
                            f"[Worker-{worker_id}] 成功合成块 {task_index + 1} "
                            f"于服务器 {server_url}{suffix}"
                        )
                        results_list[task_index] = audio_path
                        break
                if audio_path:
                    break

            if not audio_path:
                # 失败处理：检查是否还有重试机会
                if current_retry < max_retries:
                    retry_counts[task_index] = current_retry + 1
                    logger.warning(
                        f"[Worker-{worker_id}] 块 {task_index + 1} 合成失败，"
                        f"放回队列重试 ({current_retry + 1}/{max_retries})"
                    )
                    # 延迟一小段时间后重新放回队列，避免立即重试造成服务器过载
                    await asyncio.sleep(0.5)
                    await task_queue.put((task_index, chunk_text))
                else:
                    logger.error(
                        f"[Worker-{worker_id}] 块 {task_index + 1} 达到最大重试次数 "
                        f"({max_retries})，放弃。"
                    )
                    results_list[task_index] = None

            task_queue.task_done()

    async def _synthesize_direct(
        self,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """合成一条语音并做统一收尾。由全局队列调度器按 FIFO 调用。

        尾部补静音放在这一层：单块模式、单块归并、多块合并三条出口都会经过，
        不用在每个 return 点各补一次。
        """
        audio_path = await self._synthesize_audio(
            character_name=character_name,
            ref_audio_path=ref_audio_path,
            ref_audio_text=ref_audio_text,
            text=text,
            session_id_for_log=session_id_for_log,
            language=language,
        )
        return self._append_tail_padding(audio_path, session_id_for_log)

    async def _synthesize_audio(
        self,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """文本预处理、切分、调度服务器，返回未做收尾处理的音频路径。"""

        # 全局锁确保语音请求按顺序处理，先请求的先完成
        async with self._synthesis_lock:
            servers = self._server_urls()
            if not servers:
                logger.error(f"[{session_id_for_log}] 未配置TTS服务器。")
                return None

            text_for_tts = text

            # 自定义停顿标记 [pause=ms]：开启则保留，交由 Space 精确插入静音；
            # 关闭则在送入 TTS 前剥除，避免把标记本身朗读出来。
            if not self.config.get("enable_custom_pause_marker", False):
                stripped_pause = PAUSE_MARKER_PATTERN.sub(" ", text_for_tts)
                stripped_pause = re.sub(r"\s+", " ", stripped_pause).strip()
                if stripped_pause != text_for_tts:
                    text_for_tts = stripped_pause

            if self.config.get("enable_tts_text_cleaning", False):
                cleaned_text = self._clean_text_for_tts(text_for_tts)
                if cleaned_text != text_for_tts:
                    logger.info(
                        f"[{session_id_for_log}] TTS文本已清洗: '{text_for_tts[:30]}' -> '{cleaned_text[:30]}'"
                    )
                text_for_tts = cleaned_text
                if not text_for_tts:
                    logger.warning(
                        f"[{session_id_for_log}] TTS文本清洗后为空，跳过合成。"
                    )
                    self.stats["skipped_no_speech"] += 1
                    return None

            max_text_length = self.config.get(
                "tts_max_text_length", DEFAULT_MAX_TEXT_LENGTH
            )
            try:
                max_text_length = int(max_text_length)
            except (TypeError, ValueError):
                max_text_length = DEFAULT_MAX_TEXT_LENGTH
            if max_text_length > 0 and len(text_for_tts) > max_text_length:
                truncated_text = self._truncate_text_for_tts(
                    text_for_tts, max_text_length
                )
                if truncated_text and truncated_text != text_for_tts:
                    logger.warning(
                        f"[{session_id_for_log}] TTS文本超过长度上限"
                        f"(tts_max_text_length={max_text_length})，已截断: "
                        f"{len(text_for_tts)} -> {len(truncated_text)} 字符，"
                        f"超长部分不会被朗读。听起来「话没说完」就调大这个上限"
                        f"（0 表示不限制）。"
                    )
                    self.stats["text_truncated"] += 1
                    text_for_tts = truncated_text

            punctuated_text = self._ensure_terminal_punctuation(text_for_tts, language)
            if punctuated_text != text_for_tts:
                logger.info(
                    f"[{session_id_for_log}] 已为TTS文本补充句末标点，降低尾音截断概率。"
                )
                text_for_tts = punctuated_text

            if self.config.get("enable_natural_pauses", False):
                paused_text = inject_natural_pauses(
                    text_for_tts,
                    self.config.get("natural_pause_sentence_ms", 500),
                    self.config.get("natural_pause_clause_ms", 160),
                )
                if paused_text != text_for_tts:
                    text_for_tts = paused_text
                    logger.debug(f"[{session_id_for_log}] 已自动插入自然停顿。")

            # 纯标点 / 纯表情文本会让 Genie 输出整条参考音频，这里直接不合成。
            if not has_pronounceable(text_for_tts):
                logger.info(
                    f"[{session_id_for_log}] 文本没有可发音内容，跳过合成: "
                    f"'{text_for_tts[:30]}'"
                )
                self.stats["skipped_no_speech"] += 1
                return None

            if self.config.get("enable_sentence_splitting", False):
                sentences_per_chunk = self.config.get("sentences_per_chunk", 2)
                text_chunks = self._split_text_into_chunks(
                    text_for_tts, sentences_per_chunk
                )

                if not text_chunks:
                    logger.info(
                        f"[{session_id_for_log}] 切分后没有可合成的语音块，跳过合成。"
                    )
                    self.stats["skipped_no_speech"] += 1
                    return None

                if len(text_chunks) == 1:
                    # 采用归并后的文本：孤立标点已被并入，避免单独送入触发泄漏。
                    text_for_tts = text_chunks[0]
                else:
                    task_queue = asyncio.Queue()
                    for i, chunk in enumerate(text_chunks):
                        task_queue.put_nowait((i, chunk))

                    results_list = [None] * len(text_chunks)
                    retry_counts = {}  # 跟踪每个块的重试次数
                    # Worker 数量不超过服务器数量，避免长文本时创建过多空转协程
                    num_workers = min(len(text_chunks), len(servers))
                    workers = [
                        asyncio.create_task(
                            self._synthesis_worker(
                                worker_id=i,
                                task_queue=task_queue,
                                results_list=results_list,
                                retry_counts=retry_counts,
                                character_name=character_name,
                                ref_audio_path=ref_audio_path,
                                ref_audio_text=ref_audio_text,
                                session_id_for_log=session_id_for_log,
                                language=language,
                            )
                        )
                        for i in range(num_workers)
                    ]

                    logger.info(
                        f"[{session_id_for_log}] 创建了 {num_workers} 个worker来处理 {len(text_chunks)} 个语音块..."
                    )
                    try:
                        await task_queue.join()
                    finally:
                        for worker in workers:
                            worker.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)

                    successful_paths = [path for path in results_list if path]

                    # 如果有部分失败，或者全部失败，都需要清理已经生成的临时文件
                    if len(successful_paths) != len(text_chunks):
                        logger.error(
                            f"[{session_id_for_log}] 部分或全部语音块合成失败，正在清理临时文件。"
                        )
                        for path in successful_paths:
                            self._discard_temp_file(path)
                        return None

                    return (
                        successful_paths[0]
                        if len(successful_paths) == 1
                        else await self._merge_wav_files(
                            successful_paths, text_chunks
                        )
                    )

            # 如果不切分，则使用轮询逻辑
            logger.info(f"[{session_id_for_log}] 使用单块模式进行合成。")
            start_index = self.tts_server_index
            for i in range(len(servers)):
                current_index = (start_index + i) % len(servers)
                server_url = servers[current_index]

                if i == 0:
                    self.tts_server_index = (start_index + 1) % len(servers)

                audio_path = await self._attempt_synthesis_on_server(
                    server_url=server_url,
                    character_name=character_name,
                    ref_audio_path=ref_audio_path,
                    ref_audio_text=ref_audio_text,
                    text=text_for_tts,
                    session_id_for_log=session_id_for_log,
                    language=language,
                )
                if audio_path:
                    return audio_path

            logger.error(f"[{session_id_for_log}] 尝试所有TTS服务器后合成失败。")
            return None

    # ------------------------------------------------------------------ 对外

    def queue_size(self) -> int:
        """当前排队中的合成请求数量。"""
        return self._request_queue.qsize()

    async def probe_servers(self, timeout: float = 10.0) -> list[dict]:
        """并发探测所有已配置的 TTS 服务器，供 /tts-status 展示。"""
        servers = self._server_urls()
        if not servers:
            return []

        async def probe(server_url: str) -> dict:
            info: dict = {
                "url": server_url,
                "ok": False,
                "latency": None,
                "characters": [],
                "error": None,
                "busy": self._get_server_lock(server_url).locked(),
            }
            started = time.perf_counter()
            try:
                response = await self.http_client.get(
                    f"{server_url}/ui/options",
                    headers=_modelscope_auth_headers(server_url, self.config),
                    timeout=timeout
                )
                response.raise_for_status()
                payload = response.json()
                info["ok"] = True
                info["characters"] = list(payload.get("characters") or [])
            except Exception as exc:
                info["error"] = f"{type(exc).__name__}: {exc}"
            info["latency"] = time.perf_counter() - started
            return info

        return list(await asyncio.gather(*(probe(url) for url in servers)))

    async def synthesize(
        self,
        character_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        text: str,
        session_id_for_log: str,
        language: str = None,
    ) -> Optional[str]:
        """将请求放入全局 FIFO 队列，严格先到先得。"""
        loop = asyncio.get_running_loop()
        result_future = loop.create_future()
        request = {
            "character_name": character_name,
            "ref_audio_path": ref_audio_path,
            "ref_audio_text": ref_audio_text,
            "text": text,
            "session_id_for_log": session_id_for_log,
            "language": language,
        }
        self.stats["requests"] += 1
        await self._request_queue.put((request, result_future))
        logger.info(
            f"[{session_id_for_log}] 已进入TTS队列，等待中（当前队列长度: {self._request_queue.qsize()}）"
        )
        result = await result_future
        if result:
            self.stats["succeeded"] += 1
        else:
            self.stats["failed"] += 1
        return result
