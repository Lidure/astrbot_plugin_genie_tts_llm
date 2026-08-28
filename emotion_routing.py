import re
from typing import Iterable, Optional, Tuple


_EMOTION_DIRECTIVE_RE = re.compile(
    r"[\[［]\s*emotion\s*[=＝]\s*(.*?)\s*[\]］]",
    re.IGNORECASE | re.DOTALL,
)

_PROVIDER_BRACKET_SUFFIX_RE = re.compile(
    r"\s*(?:\[\s*(?:emotion\s*[=＝]\s*)?([^\[\]]+?)\s*\]"
    r"|［\s*(?:emotion\s*[=＝]\s*)?([^［］]+?)\s*］"
    r"|【\s*(?:emotion\s*[=＝]\s*)?([^【】]+?)\s*】)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_PROVIDER_LABEL_SUFFIX_RE = re.compile(
    r"\s*(?:情感|emotion)\s*[:：=＝]\s*([^\s]+)\s*$",
    re.IGNORECASE,
)


def _normalize_visible_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_emotion_directive(text: str) -> Tuple[str, Optional[str]]:
    """Remove [emotion=xxx]-style directives and return the last emotion value."""
    working = str(text or "")
    matches = list(_EMOTION_DIRECTIVE_RE.finditer(working))
    emotion = matches[-1].group(1).strip() if matches else None
    cleaned = _EMOTION_DIRECTIVE_RE.sub(" ", working)
    return _normalize_visible_text(cleaned), emotion or None


def parse_provider_emotion_result(
    text: str, emotion_names: Iterable[str]
) -> Tuple[str, Optional[str]]:
    """Parse common provider suffix formats without accepting unregistered emotions."""
    working = str(text or "").strip()
    allowed = {str(name).strip() for name in emotion_names if str(name).strip()}
    if not working or not allowed:
        return working, None

    match = _PROVIDER_BRACKET_SUFFIX_RE.search(working)
    if match:
        candidate = next((group for group in match.groups() if group is not None), "")
        candidate = candidate.strip()
        if candidate in allowed:
            return working[: match.start()].strip(), candidate

    match = _PROVIDER_LABEL_SUFFIX_RE.search(working)
    if match:
        candidate = match.group(1).strip()
        if candidate in allowed:
            return working[: match.start()].strip(), candidate

    return working, None


def should_provider_autofill_emotion(
    *,
    enabled: bool,
    translation_workflow: str,
    has_manual_emotion: bool,
    has_injected_emotion: bool,
    w_mode_active: bool,
) -> bool:
    """Return whether provider_translation should infer an emotion for this request.

    Explicit emotion choices always win. The legacy /tts-w mode keeps its existing
    provider emotion behavior even when the new opt-in autofill switch is disabled.
    """
    if str(translation_workflow or "").strip().lower() != "provider_translation":
        return False
    if has_manual_emotion or has_injected_emotion:
        return False
    return bool(enabled or w_mode_active)
