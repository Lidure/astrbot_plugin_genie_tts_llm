"""音频兼容层：探测时长、把平台侧语音（SILK/AMR/MP3…）转成可听的 WAV。

这里所有外部依赖都是**可选**的：AstrBot MediaResolver / PyAV / ffmpeg / pysilk
任意一条能走通就够用，全都缺失时抛 AudioConvertError，调用方据此降级。
所以插件的 requirements.txt 不需要为它新增任何硬依赖。

插件自己合成出来的音频本身就是标准 WAV（tts_engine 用 wave 模块写的），
所以「收藏自己发的语音」这条主链路根本不会进到转换分支——转换只服务于
「映射不回本地文件、只能从 QQ 把语音捞回来」的有损兜底路径。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Dict, Optional, Tuple

# QQ 语音的既定采样率；SILK 解码和兜底转码都对齐到它。
TARGET_SAMPLE_RATE = 24000
# 一个空 WAV 的头部就有 44 字节，比这还小的产物一定是失败品。
MIN_WAV_BYTES = 45
# 读文件头做格式嗅探时最多读这么多字节。
SNIFF_BYTES = 32
# 转码子进程的硬超时（秒）。语音一般只有几秒，60s 已经非常宽松。
CONVERT_TIMEOUT = 60


class AudioConvertError(RuntimeError):
    """所有转码通道都失败时抛出。调用方应当降级而不是把异常抛给用户。"""


# ----------------------------------------------------------------- 基础工具


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """流式算文件 sha256。收藏库用它做去重键。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sniff_format(path: str | Path) -> str:
    """靠魔术字节猜音频容器，猜不出返回空串。

    不信扩展名：QQ 落盘的语音经常叫 .amr 其实是 SILK，也经常没有扩展名。
    """
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return ""
    if not head:
        return ""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    # 腾讯 SILK 会在前面塞一个 0x02，标准 SILK 则直接以 #!SILK 开头。
    if head[:6] == b"#!SILK" or head[1:7] == b"#!SILK":
        return "silk"
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3" or (head[:1] == b"\xff" and head[1:2] in (b"\xfb", b"\xf3", b"\xf2")):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    return ""


def probe_wav(path: str | Path) -> Optional[Dict[str, int]]:
    """读 WAV 头拿到精确参数；不是 WAV 或读不动就返回 None。"""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or TARGET_SAMPLE_RATE
            channels = handle.getnchannels() or 1
            width = handle.getsampwidth() or 2
    except Exception:
        return None
    duration_ms = int(round(frames * 1000 / rate)) if rate > 0 else 0
    return {
        "frames": frames,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "duration_ms": duration_ms,
    }


def _probe_duration_via_pyav(path: str | Path) -> Optional[int]:
    """PyAV 兜底探时长。装了 PyAV（AstrBot 常带）才走得通。"""
    try:
        import av  # type: ignore
    except Exception:
        return None
    try:
        with av.open(str(path), mode="r") as container:
            if container.duration:
                return int(round(container.duration / 1000))
            streams = getattr(container.streams, "audio", None) or []
            for stream in streams:
                if stream.duration and stream.time_base:
                    return int(round(float(stream.duration * stream.time_base) * 1000))
    except Exception:
        return None
    return None


def estimate_pcm_duration_ms(size_bytes: int) -> int:
    """按 24kHz / 单声道 / 16bit 反推时长，只在真的探不出时用。"""
    try:
        payload = max(int(size_bytes) - 44, 0)
    except (TypeError, ValueError):
        return 0
    return int(round(payload / (TARGET_SAMPLE_RATE * 2) * 1000))


def probe_duration_ms(path: str | Path) -> int:
    """尽最大努力给出毫秒时长：WAV 头 → PyAV → 按码率估算。"""
    info = probe_wav(path)
    if info and info["duration_ms"] > 0:
        return info["duration_ms"]
    via_av = _probe_duration_via_pyav(path)
    if via_av and via_av > 0:
        return via_av
    try:
        return estimate_pcm_duration_ms(Path(path).stat().st_size)
    except OSError:
        return 0


def format_duration(duration_ms: int) -> str:
    """把毫秒渲染成人看的时长，给指令回复和 WebUI 共用。"""
    try:
        total = max(int(duration_ms), 0)
    except (TypeError, ValueError):
        return "0.0s"
    if total < 60000:
        return f"{total / 1000:.1f}s"
    minutes, seconds = divmod(total // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def format_size(size_bytes: int) -> str:
    try:
        value = float(max(int(size_bytes), 0))
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ----------------------------------------------------------------- 转码通道


def decode_silk_to_wav_bytes(path: str | Path) -> bytes:
    """把 SILK（腾讯变种也吃）解成 mono/16bit/24kHz 的 WAV 字节。"""
    import pysilk  # type: ignore

    pcm_buffer = io.BytesIO()
    with Path(path).open("rb") as source:
        pysilk.decode(source, pcm_buffer, TARGET_SAMPLE_RATE)
    pcm_data = pcm_buffer.getvalue()
    if not pcm_data:
        raise AudioConvertError("SILK 解码结果为空")

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_SAMPLE_RATE)
        wav_file.writeframes(pcm_data)
    return wav_buffer.getvalue()


def _pyav_convert(source: Path, target: Path) -> None:
    """用 PyAV 自带的解码器转成 mono PCM WAV。"""
    import av  # type: ignore

    target.parent.mkdir(parents=True, exist_ok=True)
    input_container = av.open(str(source), mode="r")
    output_container = av.open(str(target), mode="w", format="wav")
    try:
        if not input_container.streams.audio:
            raise AudioConvertError("源文件里没有音频流")

        input_stream = input_container.streams.audio[0]
        output_stream = output_container.add_stream(
            "pcm_s16le", rate=TARGET_SAMPLE_RATE
        )
        output_stream.codec_context.layout = "mono"
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=TARGET_SAMPLE_RATE
        )

        def encode_frame(frame) -> None:
            for packet in output_stream.encode(frame):
                output_container.mux(packet)

        for frame in input_container.decode(input_stream):
            for converted in resampler.resample(frame):
                encode_frame(converted)
        for converted in resampler.resample(None):
            encode_frame(converted)
        for packet in output_stream.encode(None):
            output_container.mux(packet)
    finally:
        output_container.close()
        input_container.close()

    if not target.is_file() or target.stat().st_size < MIN_WAV_BYTES:
        raise AudioConvertError("PyAV 产出的 WAV 是空的")


def _ffmpeg_convert(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioConvertError("系统里没有 ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg, "-y", "-i", str(source), "-vn",
            "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(target),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=CONVERT_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg 转码失败").strip()
        raise AudioConvertError(detail[-400:])
    if not target.is_file() or target.stat().st_size < MIN_WAV_BYTES:
        raise AudioConvertError("ffmpeg 产出的 WAV 是空的")


async def _media_resolver_convert(source: Path, target: Path) -> None:
    """优先借 AstrBot 自带的 MediaResolver：它跟宿主环境的编解码器最贴合。"""
    from astrbot.core.utils.media_utils import MediaResolver  # type: ignore

    converted = await MediaResolver(
        str(source),
        media_type="audio",
        default_suffix=source.suffix or ".wav",
    ).to_path(target_format="wav")
    converted_path = Path(converted)
    if not converted_path.is_file():
        raise AudioConvertError(f"MediaResolver 的产物不存在: {converted_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(converted_path, target)
    if target.stat().st_size < MIN_WAV_BYTES:
        raise AudioConvertError("MediaResolver 产出的 WAV 是空的")


async def convert_to_wav(source: str | Path, target: str | Path) -> str:
    """把任意音频转成 WAV，四级降级；全挂就抛 AudioConvertError。

    返回实际使用的通道名，方便日志里说清楚这条语音是怎么来的。
    """
    src = Path(source)
    dst = Path(target)
    if not src.is_file():
        raise AudioConvertError(f"源文件不存在: {src}")

    errors: list[str] = []
    kind = sniff_format(src) or src.suffix.lower().lstrip(".")

    # SILK 只有 pysilk 能吃，放最前面单独试；不是 SILK 就跳过。
    if kind == "silk":
        try:
            data = await asyncio.to_thread(decode_silk_to_wav_bytes, src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(dst.write_bytes, data)
            return "pysilk"
        except Exception as exc:
            errors.append(f"pysilk: {type(exc).__name__}")

    for name, runner in (
        ("MediaResolver", _media_resolver_convert),
        ("PyAV", None),
        ("ffmpeg", None),
    ):
        try:
            if name == "MediaResolver":
                await runner(src, dst)  # type: ignore[misc]
            elif name == "PyAV":
                await asyncio.to_thread(_pyav_convert, src, dst)
            else:
                await asyncio.to_thread(_ffmpeg_convert, src, dst)
            return name
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass

    raise AudioConvertError("全部转码通道失败 → " + "; ".join(errors))


class AudioConverter:
    """带缓存和串行锁的转码器：同一个源文件不会被并发转两遍。

    缓存键 = 路径指纹 + 修订指纹（mtime_ns:size），源文件一改就自然失效。
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _cache_target(self, source: Path) -> Tuple[Path, str]:
        stat = source.stat()
        path_key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        revision = hashlib.sha256(
            f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
        ).hexdigest()[:12]
        return self.cache_dir / f"{path_key}-{revision}.wav", path_key

    def _prune(self, path_key: str, keep: Path) -> None:
        for candidate in self.cache_dir.glob(f"{path_key}-*.wav"):
            if candidate == keep:
                continue
            try:
                candidate.unlink()
            except OSError:
                pass

    async def to_wav(self, path: str | Path) -> Tuple[str, str]:
        """返回 (wav 路径, 通道名)。已经是 WAV 时原样返回，通道名为 "原样"。"""
        source = Path(path).resolve()
        if not source.is_file():
            raise AudioConvertError(f"源文件不存在: {source}")
        if sniff_format(source) == "wav":
            return str(source), "原样"

        key = str(source)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target, path_key = self._cache_target(source)
            if target.is_file() and target.stat().st_size >= MIN_WAV_BYTES:
                return str(target), "缓存"

            temporary = self.cache_dir / f".{target.stem}.tmp.wav"
            try:
                temporary.unlink(missing_ok=True)
                channel = await convert_to_wav(source, temporary)
                if not temporary.is_file() or temporary.stat().st_size < MIN_WAV_BYTES:
                    raise AudioConvertError("转码器没产出有效 WAV")
                temporary.replace(target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            self._prune(path_key, target)
            return str(target), channel
