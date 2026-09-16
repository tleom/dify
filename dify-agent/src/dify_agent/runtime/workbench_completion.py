"""Reject an unfinished request for clarification before publishing a final answer."""

import re


_CLARIFICATION_INTRO = re.compile(
    r"(?:我(?:需要|想|还需)|请(?:您|你)?|麻烦).{0,120}"
    r"(?:了解|补充|确认|明确|提供|核对|澄清).{0,100}"
    r"(?:信息|问题|事项|细节|情况|需求|条件|背景|要点).{0,100}[:：]$"
)


def unfinished_clarification(text: str) -> bool:
    """A short prose handoff ending at ':' contains neither questions nor results.

    This intentionally targets the observed empty clarification introduction,
    rather than judging whether arbitrary prose completes a user's whole task.
    Code, quoted examples, actual question lists and tool-bearing turns pass.
    """
    text = text.strip()
    if not text.endswith((":", "：")) or "```" in text or "~~~" in text:
        return False
    paragraph = text.rsplit("\n", 1)[-1].strip()
    if not paragraph or paragraph.startswith((">", "-", "*", "#")) or len(paragraph) > 320:
        return False
    if "?" in paragraph or "？" in paragraph:
        return False
    return _CLARIFICATION_INTRO.search(paragraph) is not None


CLARIFICATION_RETRY = (
    "Your reply stopped after introducing clarification, without the questions. "
    "Do not end this turn with an unfinished introduction. If essential user input is missing, "
    "call ask_human with the actual questions when that tool is available, or state the complete questions. "
    "If the task can proceed with reasonable assumptions, continue the requested work now. "
    "Do not repeat the introduction as a final answer."
)
