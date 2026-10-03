"""pyte 屏幕 → Rich 文本，带脏行缓存与区段化样式。

性能设计（针对 ``textual-terminal`` "每收到一块数据就全屏重建" 的毛病）:

旧实现每来一个 chunk 就做的事::

    for y in range(40):              # 全屏
        for x in range(120):         # 4800 次
            line_text.append(char.data)
            line_text.stylize(style, ...)   # 每字符一次 stylize

新实现:

1. **脏行缓存** —— pyte 自己维护 ``Screen.dirty``（精确到行），只重建变化的
   行，其余行直接复用上一次的 ``Text`` 对象。
2. **区段化样式** —— 把连续同款的字符合并成一段再 ``append(text, style)``，
   而不是逐字符 ``stylize()``。
3. **尾部裁剪** —— 行尾默认样式的空格不渲染（终端屏幕上大片是空白）。
4. **样式对象缓存** —— ``Style`` 按属性元组缓存，比较用 ``is`` 而非 ``==``。
5. **不污染 buffer** —— 用 ``line.get(x, default)`` 而不是 ``line[x]``，
   避免 pyte 的 ``StaticDefaultDict`` 为每个空单元格插入对象。
"""

from __future__ import annotations

import pyte
from rich.style import Style
from rich.text import Text

__all__ = ["TerminalEmulator"]


# ---------------------------------------------------------------------------
# 颜色映射
# ---------------------------------------------------------------------------
# pyte 用的是它自己的颜色名（实测 SGR 30-37 / 90-97）：
#   black red green brown blue magenta cyan white
#   brightblack brightred brightgreen brightbrown brightblue
#   brightmagenta brightcyan brightwhite
# 注意 "brown"/"brightbrown" 在 Rich 里叫 yellow/bright_yellow。
_COLOR_MAP: dict[str, str | None] = {
    "default": None,
    "black": "black",
    "red": "red",
    "green": "green",
    "brown": "yellow",
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightyellow": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}

_UNSET = object()


def _color(name: str) -> str | None:
    """pyte 颜色名 → Rich 颜色。

    pyte 会把 256 色和 truecolor 解成不带 ``#`` 的 6 位小写 hex
    （实测 ``\\x1b[38;5;196m`` → ``'ff0000'``）。
    """
    mapped = _COLOR_MAP.get(name, _UNSET)
    if mapped is not _UNSET:
        return mapped  # type: ignore[return-value]
    if len(name) == 6:
        try:
            int(name, 16)
        except ValueError:
            return None
        return "#" + name
    return None


_STYLE_CACHE: dict[tuple, Style] = {}

#: "主动渐变"时色相的量化档数。动画每帧都会算新色相，
#: 量化后缓存命中率高很多，128 档肉眼已看不出台阶。
HUE_ANIM_STEPS = 128

#: xterm 16 色的标准 RGB。
#: 只用于动画时旋转色相 —— 最终显示时 Textual 的 ANSIToTruecolor
#: 滤镜还会把它们映射到主题调色板。
_NAMED_RGB: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "brown": (205, 205, 0),
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "brightblack": (127, 127, 127),
    "brightred": (255, 0, 0),
    "brightgreen": (0, 255, 0),
    "brightbrown": (255, 255, 0),
    "brightblue": (92, 92, 255),
    "brightmagenta": (255, 0, 255),
    "brightcyan": (0, 255, 255),
    "brightwhite": (255, 255, 255),
}


def _rgb_of(name: str) -> tuple[int, int, int] | None:
    """pyte 颜色名 -> RGB。``default`` / 未知返回 ``None``。"""
    if not name or name == "default":
        return None
    named = _NAMED_RGB.get(name)
    if named is not None:
        return named
    if len(name) == 6:
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
        except ValueError:
            return None
    return None


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i %= 6
    r, g, b = (
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    )[i]
    return int(r * 255), int(g * 255), int(b * 255)


def _rotate_hue(rgb: tuple[int, int, int], offset: float) -> tuple[int, int, int]:
    """只旋转色相，保留饱和度与明度。"""
    r, g, b = rgb
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0 or mx == 0:
        return rgb                      # 灰阶没有色相可转
    rr, gg, bb = r / 255.0, g / 255.0, b / 255.0
    if mx == r:
        h = ((gg - bb) / (d / 255.0)) % 6
    elif mx == g:
        h = (bb - rr) / (d / 255.0) + 2
    else:
        h = (rr - gg) / (d / 255.0) + 4
    return _hsv_to_rgb(h / 6.0 + offset, d / mx, mx / 255.0)


def _resolve_color(name: str, hue_shift: float | None) -> str | None:
    """pyte 颜色名 -> Rich 颜色字符串，可选色相旋转。

    ``hue_shift`` 为 None/0 时走普通映射；否则把颜色转到 RGB、
    旋转色相后再交回去。``default`` 与未知颜色不参与旋转。
    """
    if hue_shift:
        rgb = _rgb_of(name)
        if rgb is not None:
            r, g, b = _rotate_hue(rgb, hue_shift)
            return f"#{r:02x}{g:02x}{b:02x}"
    return _color(name)


def _style_for(char, cursor: bool = False,
               hue_shift: float | None = None) -> Style:
    """按字符属性取 Style（带缓存）。

    ``hue_shift`` 非零时做"主动渐变"：所有带颜色的文字整体旋转色相，
    于是屏幕上原有的渐变会随时间流动起来。
    """
    # 只有"真的带颜色"的字符才把色相并进缓存键。
    # 否则默认色字符会因为色相不同而生成新对象，破坏
    # "尾部空白裁剪"依赖的 `is _default_style` 身份比较。
    hue_key = 0
    if hue_shift and (char.fg != "default" or char.bg != "default"):
        hue_key = (int(hue_shift * HUE_ANIM_STEPS) % HUE_ANIM_STEPS) + 1
    key = (
        char.fg,
        char.bg,
        char.bold,
        char.italics,
        char.underscore,
        char.strikethrough,
        char.reverse,
        char.blink,
        cursor,
        hue_key,
    )
    style = _STYLE_CACHE.get(key)
    if style is not None:
        return style

    shift = (hue_key - 1) / HUE_ANIM_STEPS if hue_key else None

    fg = _resolve_color(char.fg, shift)
    bg = _resolve_color(char.bg, shift)
    if char.reverse:
        # pyte 只置 reverse 标志，不交换 fg/bg（实测确认），渲染层自己换
        fg, bg = bg, fg

    style = Style(
        color=fg,
        bgcolor=bg,
        bold=char.bold,
        italic=char.italics,
        underline=char.underscore,
        strike=char.strikethrough,
        blink=char.blink,
        reverse=cursor,
    )
    _STYLE_CACHE[key] = style
    return style


# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------
class TerminalEmulator:
    """维护一个 pyte 屏幕，并把它增量渲染成 Rich ``Text`` 行。"""

    __slots__ = (
        "_screen",
        "_stream",
        "_cache",
        "_cursor_line",
        "_cursor_hidden",
        "_cursor_valid",
        "_default_style",
        "_pending",
        "gradient_mode",
        "gradient_speed",
        "gradient_rev",
        "hue_shift",
        "lines_rebuilt",
        "frames",
    )

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        columns = max(1, int(columns))
        rows = max(1, int(rows))
        self._screen = pyte.Screen(columns, rows)
        self._stream = pyte.ByteStream(self._screen)

        self._cache: list[Text | None] = [None] * rows
        self._cursor_line = -1
        self._cursor_hidden = False
        self._cursor_valid = False
        self._default_style = _style_for(self._screen.default_char)

        # 私有 OSC 的跨包残留
        self._pending = b""
        # 宿主终端渐变状态（由 shell 通过私有 OSC 通知）
        self.gradient_mode: str | None = None
        self.gradient_speed: float = 0.18
        #: 每次渐变状态变化 +1，宿主据此决定要不要开动画定时器
        self.gradient_rev = 0
        #: 主动渐变的当前色相偏移（由宿主每帧设置）
        self.hue_shift = 0.0

        # 统计（基准测试用）
        self.lines_rebuilt = 0
        self.frames = 0

    # ------------------------------------------------------------------
    @property
    def screen(self) -> pyte.Screen:
        return self._screen

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    @property
    def cursor(self) -> tuple[int, int, bool]:
        cur = self._screen.cursor
        return cur.x, cur.y, cur.hidden

    @property
    def display(self) -> list[str]:
        """纯文本快照（测试/断言用）。"""
        return self._screen.display

    # ------------------------------------------------------------------
    # 私有 OSC：shell -> 宿主终端 的信道
    # ------------------------------------------------------------------
    #: 序列形如 ``ESC ] 777 ; jiahao ; gradient=<mode>;<speed> BEL``
    _OSC_PREFIX = b"\x1b]777;jiahao;"
    _OSC_TERMINATORS = (b"\x07", b"\x1b\\")

    @classmethod
    def _partial_prefix_len(cls, buf: bytes) -> int:
        """返回 buf 末尾属于 ``_OSC_PREFIX`` 前缀的最长长度（跨包用）。"""
        limit = min(len(buf), len(cls._OSC_PREFIX) - 1)
        for k in range(limit, 0, -1):
            if buf[-k:] == cls._OSC_PREFIX[:k]:
                return k
        return 0

    def _handle_private_osc(self, payload: bytes) -> None:
        """处理 ``gradient=<mode>;<speed>`` 载荷。"""
        try:
            text = payload.decode("ascii", "replace")
        except Exception:                       # pragma: no cover - 防御
            return
        if not text.startswith("gradient="):
            return
        value = text[len("gradient="):]
        if value == "off" or not value:
            if self.gradient_mode is not None:
                self.gradient_mode = None
                self.gradient_rev += 1
            return
        parts = value.split(";")
        mode = parts[0].strip() or "both"
        speed = self.gradient_speed
        if len(parts) > 1:
            try:
                speed = float(parts[1])
            except ValueError:
                pass
        if mode not in ("both", "static", "anim"):
            return
        self.gradient_mode = mode
        self.gradient_speed = max(0.0, speed)
        self.gradient_rev += 1

    # ------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """喂原始字节。

        ``pyte.ByteStream`` 内部做增量 UTF-8 解码，跨 chunk 边界的
        多字节字符和 ANSI 序列都不会丢。

        另外这里会**先摘掉私有 OSC**：pyte 对未知 OSC 是读完即丢，
        但我们得先看到它，所以在喂给 pyte 之前拦下来。
        序列可能被切成两半，所以用一个残留缓冲兜住。
        """
        if not data:
            return

        buf = self._pending + data if self._pending else data
        self._pending = b""

        out = bytearray()
        while buf:
            i = buf.find(self._OSC_PREFIX)
            if i < 0:
                keep = self._partial_prefix_len(buf)
                if keep:
                    out += buf[: len(buf) - keep]
                    self._pending = buf[len(buf) - keep:]
                else:
                    out += buf
                break

            out += buf[:i]
            rest = buf[i + len(self._OSC_PREFIX):]

            end = -1
            elen = 1
            for term in self._OSC_TERMINATORS:
                j = rest.find(term)
                if j >= 0 and (end < 0 or j < end):
                    end, elen = j, len(term)
            if end < 0:
                # 序列还没收完，整段挂起等下一包
                self._pending = buf[i:]
                break

            self._handle_private_osc(rest[:end])
            buf = rest[end + elen:]

        if out:
            self._stream.feed(bytes(out))

    def feed_text(self, text: str) -> None:
        self.feed(text.encode("utf-8", "replace"))

    def resize(self, rows: int, cols: int) -> None:
        rows = max(1, int(rows))
        cols = max(1, int(cols))
        if rows == self._screen.lines and cols == self._screen.columns:
            return
        self._screen.resize(rows, cols)
        self._cache = [None] * rows
        self._cursor_valid = False

    def reset(self) -> None:
        self._screen.reset()
        self._cache = [None] * self._screen.lines
        self._cursor_valid = False

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(self) -> list[Text]:
        """返回每行的 ``Text``，**只重建 pyte 标记为脏的行**。"""
        screen = self._screen
        rows = screen.lines
        cols = screen.columns

        # pyte 的 dirty 精确到行：光标移动+写入只标记那一行
        dirty = set(screen.dirty)
        screen.dirty.clear()

        cur = screen.cursor
        cx, cy, hidden = cur.x, cur.y, cur.hidden

        # 光标所在行变了 → 旧行和新行都要重画
        if not self._cursor_valid:
            dirty.update(range(rows))
            self._cursor_valid = True
        else:
            if cy != self._cursor_line:
                if 0 <= self._cursor_line < rows:
                    dirty.add(self._cursor_line)
                dirty.add(cy)
            if hidden != self._cursor_hidden:
                dirty.add(cy)

        self._cursor_line = cy
        self._cursor_hidden = hidden

        if len(self._cache) != rows:
            self._cache = [None] * rows
            dirty.update(range(rows))

        # 主动渐变：每帧所有颜色都在变，脏行缓存失效，整屏重建。
        # 代价是每帧多花一点 CPU，换来整屏颜色持续流动。
        if self.hue_shift:
            dirty.update(range(rows))

        self.frames += 1
        default_char = screen.default_char

        for y in dirty:
            if 0 <= y < rows:
                self._cache[y] = self._render_line(
                    y, cols, default_char, cx, cy, hidden, self.hue_shift
                )
                self.lines_rebuilt += 1

        # 理论上不会有 None 残留，兜底避免下游拿到 None
        for y in range(rows):
            if self._cache[y] is None:
                self._cache[y] = Text(no_wrap=True, end="")
        return self._cache  # type: ignore[return-value]

    def _render_line(self, y, cols, default_char, cx, cy, hidden,
                     hue_shift: float | None = None) -> Text:
        buf = self._screen.buffer[y]
        cursor_here = (y == cy) and not hidden

        # ---- 尾部裁剪：找到最后一个"可见"单元格 ----
        # 默认色字符在带 hue_shift 时仍复用同一个 Style 对象，
        # 所以这里的身份比较依然成立。
        last = -1
        for x in range(cols - 1, -1, -1):
            ch = buf.get(x, default_char)
            if ch.data != " " or _style_for(ch, hue_shift=hue_shift) is not self._default_style:
                last = x
                break
        if cursor_here and cx > last:
            last = cx
        if last < 0:
            return Text(no_wrap=True, end="")

        # ---- 区段化：把连续同款的字符合并成一段 ----
        line = Text(no_wrap=True, end="")
        run: list[str] = []
        run_style: Style | None = None

        for x in range(last + 1):
            ch = buf.get(x, default_char)
            style = _style_for(ch, cursor=cursor_here and x == cx,
                               hue_shift=hue_shift)
            if style is not run_style:
                if run:
                    line.append("".join(run), run_style)
                run = [ch.data]
                run_style = style
            else:
                run.append(ch.data)

        if run:
            line.append("".join(run), run_style)
        return line

    def render_text(self) -> Text:
        """把所有行拼成一个 ``Text``（Textual 的 render() 用）。"""
        lines = self.render()
        out = Text(no_wrap=True, end="")
        for i, line in enumerate(lines):
            if i:
                out.append("\n")
            out.append_text(line)
        return out

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TerminalEmulator {self.columns}x{self.rows} "
            f"rebuilt={self.lines_rebuilt}>"
        )
