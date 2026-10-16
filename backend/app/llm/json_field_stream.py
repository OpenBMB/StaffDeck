"""JSON 文本流的增量字符串字段提取（**只用于流式预览**）。

Harness 的决策调用是 JSON 模式：用户可见正文写在 ``finish.reply_fragment``
字符串里，整段 JSON 收完才能 ``json.loads``，因此正文天然只能「一次性」到达
前端。本模块提供一个最小提取器：一边收文本增量，一边把目标字段的字符串值
逐步解出来，让调用方可以提前下发。

契约（很重要）：

- 只做「预览」。结构不符、转义被截断、字段顺序意外等情况**一律不抛异常**，
  停止输出即可；最终正文一律以完整 JSON 的解析结果为准。
- 每次 ``feed`` 都从已累积的原文重扫一遍，因此没有跨 chunk 状态，不存在
  「分片边界把转义序列切两半」导致的错位。
- 只认第一个出现的目标字段；``arm_marker`` 用于排除「工具动作里偶然出现同名
  字段」的情况（默认要求正文之前先出现 ``finish``），宁可漏也不要提前泄露。
"""

from __future__ import annotations

from typing import NamedTuple

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_WHITESPACE = " \t\r\n"
# 落单的 UTF-16 高位代理：等待后续低位代理拼成完整字符后再下发。
_SURROGATE_HIGH = (0xD800, 0xDBFF)
_SURROGATE_LOW = (0xDC00, 0xDFFF)


class _FieldState(NamedTuple):
    """一次重扫的结果：已解出的可见文本 / 是否已收尾 / 是否结构不符。"""

    text: str
    complete: bool
    unparseable: bool


class JsonStringFieldStream:
    """从 JSON 文本增量里提取指定字符串字段的值。"""

    def __init__(self, field: str, *, arm_marker: str | None = None) -> None:
        self._needle = f'"{field}"'
        self._arm_marker = arm_marker
        self._raw = ""
        self._value = ""
        self._dead = False
        self.finished = False

    @property
    def text(self) -> str:
        """已解出的可见文本（全量）。"""

        return self._value

    @property
    def dead(self) -> bool:
        """提取通道已放弃（结构不符）；调用方无需再做任何处理。"""

        return self._dead

    def feed(self, delta: str) -> str:
        """喂入原始文本增量，返回本次新解出的可见文本（没有新内容时返回空串）。"""

        if self._dead or self.finished or not delta:
            return ""
        self._raw += delta
        state = _decode_field_value(self._raw, self._needle, self._arm_marker)
        if state is None:
            return ""
        if state.unparseable:
            # 结构不符（例如值不是字符串）：这个通道不可能再对上，直接放弃。
            self._dead = True
            return ""
        text = state.text
        # 落单的高位代理先压住，等低位代理到达后再一起下发；
        # 否则前端会先收到一个非法字符。
        text = _trim_pending_surrogate(text)
        if not text.startswith(self._value):
            # 理论上不可能（重扫结果只会是已有前缀的延长）。真出现说明对不上，
            # 直接放弃预览，交给最终正文兜底。
            self._dead = True
            return ""
        if state.complete:
            self.finished = True
        new_text = text[len(self._value) :]
        self._value = text
        return new_text


def _trim_pending_surrogate(text: str) -> str:
    while text and _SURROGATE_HIGH[0] <= ord(text[-1]) <= _SURROGATE_HIGH[1]:
        text = text[:-1]
    return text


def _decode_field_value(
    raw: str,
    needle: str,
    arm_marker: str | None,
) -> _FieldState | None:
    """解出 ``needle`` 对应的字符串值；``None`` 表示当前还看不出结果。"""

    start = 0
    if arm_marker is not None:
        marker = raw.find(arm_marker)
        if marker < 0:
            return None
        start = marker + len(arm_marker)
    key = raw.find(needle, start)
    if key < 0:
        return None
    cursor = _skip_whitespace(raw, key + len(needle))
    if cursor >= len(raw):
        return None
    if raw[cursor] != ":":
        return _FieldState("", False, True)
    cursor = _skip_whitespace(raw, cursor + 1)
    if cursor >= len(raw):
        return None
    if raw[cursor] != '"':
        return _FieldState("", False, True)
    text, complete = _decode_string(raw, cursor + 1)
    return _FieldState(text, complete, False)


def _skip_whitespace(raw: str, cursor: int) -> int:
    while cursor < len(raw) and raw[cursor] in _WHITESPACE:
        cursor += 1
    return cursor


def _read_unicode_escape(raw: str, cursor: int) -> tuple[int | None, int]:
    """读取 ``cursor`` 处的 ``\\uXXXX``，返回 ``(码点, 新游标)``。"""

    if not raw.startswith("\\u", cursor):
        return None, cursor
    digits = raw[cursor + 2 : cursor + 6]
    if len(digits) < 4 or not all(digit in _HEX_DIGITS for digit in digits):
        return None, cursor
    return int(digits, 16), cursor + 6


def _decode_string(raw: str, cursor: int) -> tuple[str, bool]:
    """从开引号之后开始解码，返回 ``(可见文本, 是否已遇到收尾引号)``。"""

    out: list[str] = []
    while cursor < len(raw):
        char = raw[cursor]
        cursor += 1
        if char == "\\":
            if cursor >= len(raw):
                # 转义序列被分片截断：先返回已解出的部分，等下一片再续。
                return "".join(out), False
            escaped = raw[cursor]
            cursor += 1
            if escaped != "u":
                out.append(_ESCAPES.get(escaped, escaped))
                continue
            digits = raw[cursor : cursor + 4]
            if len(digits) < 4:
                return "".join(out), False
            if not all(digit in _HEX_DIGITS for digit in digits):
                return "".join(out), False
            code_point = int(digits, 16)
            cursor += 4
            if _SURROGATE_HIGH[0] <= code_point <= _SURROGATE_HIGH[1]:
                low, next_cursor = _read_unicode_escape(raw, cursor)
                if low is not None and _SURROGATE_LOW[0] <= low <= _SURROGATE_LOW[1]:
                    out.append(
                        chr(0x10000 + ((code_point - 0xD800) << 10) + (low - 0xDC00))
                    )
                    cursor = next_cursor
                    continue
            out.append(chr(code_point))
            continue
        if char == '"':
            return "".join(out), True
        out.append(char)
    return "".join(out), False
