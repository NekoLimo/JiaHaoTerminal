"""输出与配色。

包含两件事：

1. **Rich console** —— 显式关闭 ``NO_COLOR`` 的影响。
   本程序明确要求彩色输出（``jiahaoshell`` 自己就会设 FORCE_COLOR），
   但 Rich 默认遵循 ``NO_COLOR`` 标准，会把颜色全部剥掉 ——
   结果是提示符（走裸 ANSI）有色、命令输出（走 Rich）没色。
2. **渐变色** —— 位置渐变（同一行内色相随字符位置变化）
   与时间渐变（色相随时间推移），可单独或同时启用。
"""

from __future__ import annotations

import os
import sys
import time
from functools import lru_cache

from rich.color import Color as RichColor
from rich.console import Console
from rich.style import Style
from rich.text import Text

# ===========================================================================
# Console
# ===========================================================================
# no_color=False 是**必须的**：环境里只要有 NO_COLOR（哪怕是 NO_COLOR=1），
# Rich 就会把 style 全部丢掉，force_terminal=True 也救不回来。
# ★ 必须在创建 Console 之前把 Windows 控制台的代码页切成 UTF-8。
#   rich 会在这里抓住控制台句柄，之后再改代码页就不生效了 ——
#   结果是输出中文直接 UnicodeEncodeError。
from .compat import setup_console_encoding as _setup_console_encoding

_setup_console_encoding()

console = Console(
    force_terminal=True,
    color_system="truecolor",
    highlight=False,
    no_color=False,
)

# ===========================================================================
# 纯色表
# ===========================================================================
ANSI_FG = {
    "black": "30", "red": "31", "green": "32", "yellow": "33",
    "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    "bright_black": "90", "bright_red": "91", "bright_green": "92",
    "bright_yellow": "93", "bright_blue": "94", "bright_magenta": "95",
    "bright_cyan": "96", "bright_white": "97",
}
ALIASES = {
    "grey": "bright_black", "gray": "bright_black",
    "purple": "magenta", "pink": "bright_magenta",
}


def _parse_hex(value: str) -> tuple[int, int, int] | None:
    """``#rrggbb`` / ``rrggbb`` -> RGB。"""
    v = (value or "").lstrip("#")
    if len(v) != 6:
        return None
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def _sgr_fg(style: str) -> str | None:
    """配色 -> SGR 前景色参数（不含 ``ESC[`` 和 ``m``）。

    支持三种写法：

    ==============  ==========================  ====================
    写法             例子                         产出
    ==============  ==========================  ====================
    具名             ``red`` / ``bright_cyan``   ``31`` / ``96``
    256 色           ``196``                     ``38;5;196``
    真彩色           ``#ff8800`` / ``ff8800``    ``38;2;255;136;0``
    ==============  ==========================  ====================
    """
    if not style:
        return None
    code = ANSI_FG.get(style)
    if code:
        return code
    if style.isdigit():
        n = int(style)
        return f"38;5;{n}" if 0 <= n <= 255 else None
    rgb = _parse_hex(style)
    if rgb:
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}"
    return None

#: 渐变模式的合法取值
GRADIENT_MODES = ("both", "static", "anim")
#: 触发渐变的别名
GRADIENT_ALIASES = ("gradient", "rainbow", "渐变", "彩虹")

# ===========================================================================
# 状态
# ===========================================================================
_current_style = ""      # 纯色名；"" 表示默认
_gradient_mode: str | None = None

# ===========================================================================
# 渐变参数
# ===========================================================================
#: 一行文字跨越几个完整色相周期
GRADIENT_CYCLES = 1.0
#: 每秒推进多少个色相周期（时间渐变的速度）
GRADIENT_SPEED = 0.18
#: 渐变的饱和度与明度（HSV 的 s / v）。
#: 拉满（1.0）出来的是 #ff0000 / #00ff00 这种刺眼的纯色，在终端里"太重"。
#:
#: 实测（720 个色相采样）256 色模式下能出多少种颜色：
#:     s=1.00 -> 30    s=0.85 -> 30    s=0.75 -> 24
#:     s=0.65 -> 24    s=0.55 -> 24    s=0.45 -> 18
#: 0.75~0.55 这一段颜色数一样，所以直接取最柔和的 0.65 —— 
#: 既明显不刺眼，又不损失色阶。
GRADIENT_SATURATION = 0.65
GRADIENT_VALUE = 1.0

#: 256 色模式下希望一行分成多少档。
#: 一圈色相在 256 立方体里只有 ~24 种颜色，档数超过它就是纯重复。
_TARGET_256_STOPS = 24
#: 色相量化档数（越大越平滑，越小缓存命中率越高）
_HUE_STEPS = 720
#: 多少个字符共用一档颜色。
#: 真彩色转义序列每个约 20 字节，逐字符上色会让输出体积膨胀 20 倍；
#: 每 2 个字符一档视觉上看不出区别，体积和 CPU 都直接减半。
#: 注意：256 色模式下这个值会自动放大，见 _effective_step。
GRADIENT_STEP = 2

#: 渐变的色彩深度：``256`` 用 xterm-256 调色板（``38;5;N``），
#: ``24`` 用真彩色（``38;2;r;g;b``）。
#:
#: 默认 256 —— 兼容性好得多，很多终端（尤其 SSH / tmux / 老终端）
#: 不认 ``38;2;r;g;b``，真彩色序列会被整段丢掉，看起来就是"没颜色"。
#: 代价是 256 立方体每通道只有 6 级，一圈色相只能量化出 24-30 种颜色。
GRADIENT_COLOR_DEPTH = 256

#: xterm 256 色立方体（16-231）每通道的 6 级取值
_XTERM_LEVELS = (0, 95, 135, 175, 215, 255)


def _rgb_to_ansi256(r: int, g: int, b: int) -> int:
    """RGB -> xterm 256 色索引（量化到 6x6x6 立方体，即 16-231）。"""
    def near(v: int) -> int:
        return min(range(6), key=lambda i: abs(_XTERM_LEVELS[i] - v))
    return 16 + 36 * near(r) + 6 * near(g) + near(b)


def _hue_to_ansi256(hue: float) -> int:
    return _rgb_to_ansi256(
        *_hsv_to_rgb(hue, GRADIENT_SATURATION, GRADIENT_VALUE)
    )


def _effective_step(total: int) -> int:
    """当前该用多大的"每档字符数"。

    256 色一圈只有 ~30 种颜色，档数超过它就是纯粹重复，
    只会让长行看起来一段一段的。所以按总长把档数压到 30 以内。
    """
    if GRADIENT_COLOR_DEPTH != 256:
        return GRADIENT_STEP
    return max(GRADIENT_STEP, round(total / _TARGET_256_STOPS))


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """HSV -> RGB，各分量 0..255。"""
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i %= 6
    r, g, b = (
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    )[i]
    return int(r * 255), int(g * 255), int(b * 255)


@lru_cache(maxsize=1024)
def _style_for_hue_step(step: int) -> Style:
    """按量化后的色相取 Style（带缓存，避免每字符都造对象）。

    色彩深度由 ``GRADIENT_COLOR_DEPTH`` 决定：256 色走 xterm 调色板
    （Rich 会渲染成 ``38;5;N``），否则走真彩色。
    """
    hue = (step % _HUE_STEPS) / _HUE_STEPS
    if GRADIENT_COLOR_DEPTH == 256:
        return Style(color=RichColor.from_ansi(_hue_to_ansi256(hue)))
    r, g, b = _hsv_to_rgb(hue, GRADIENT_SATURATION, GRADIENT_VALUE)
    return Style(color=f"#{r:02x}{g:02x}{b:02x}")


def _hue_to_sgr(hue: float) -> str:
    """色相 -> SGR 前景色参数（不含 ``ESC[`` 和 ``m``）。"""
    if GRADIENT_COLOR_DEPTH == 256:
        return f"38;5;{_hue_to_ansi256(hue)}"
    r, g, b = _hsv_to_rgb(hue, GRADIENT_SATURATION, GRADIENT_VALUE)
    return f"38;2;{r};{g};{b}"


def _time_offset() -> float:
    """时间渐变：色相随挂钟时间推进。"""
    return (time.monotonic() * GRADIENT_SPEED) % 1.0


def _hue_at(index: int, total: int, mode: str,
            time_offset: float | None = None) -> float:
    """算出第 ``index`` 个字符当前的色相。

    * ``static`` —— 只随位置变（位置渐变）
    * ``anim``   —— 只随时间变（时间渐变，整行同色但不断流动）
    * ``both``   —— 两者叠加：彩虹沿行铺开，同时整体向前流动

    位置分量按 ``_effective_step(total)`` 分档，相邻同档字符共用一个颜色，
    这样渲染时可以合并成一段，转义序列数量成倍减少。
    """
    hue = 0.0
    if mode in ("static", "both") and total > 1:
        step = _effective_step(total)
        slot = index // step
        slots = max(1, -(-total // step))               # ceil(total / step)
        hue += (slot / slots) * GRADIENT_CYCLES
    if mode in ("anim", "both"):
        hue += _time_offset() if time_offset is None else time_offset
    return hue % 1.0


# ===========================================================================
# 状态读写
# ===========================================================================
def normalize(name: str):
    """'' -> 复位；规范名 -> 合法；None -> 非法

    合法形式：具名色、256 色索引（0-255）、真彩色（``#rrggbb`` / ``rrggbb``）。
    """
    n = (name or "").strip().lower()
    if n in ("", "reset", "default", "off", "none"):
        return ""
    n = ALIASES.get(n, n)
    if n in ANSI_FG:
        return n
    if n.isdigit() and 0 <= int(n) <= 255:
        return n
    if _parse_hex(n):
        return n if n.startswith("#") else "#" + n
    return None


def set_style(style: str) -> None:
    """设置纯色（会关掉渐变）。"""
    global _current_style, _gradient_mode
    _current_style = style or ""
    _gradient_mode = None


def set_gradient(mode: str = "both") -> None:
    """开启渐变。``mode`` ∈ GRADIENT_MODES。"""
    global _current_style, _gradient_mode
    mode = (mode or "both").strip().lower()
    if mode not in GRADIENT_MODES:
        raise ValueError(f"渐变模式只能是 {'/'.join(GRADIENT_MODES)}，收到 {mode!r}")
    _gradient_mode = mode
    _current_style = ""


def reset_style() -> None:
    """恢复默认（同时关掉渐变）。"""
    set_style("")


def get_style() -> str:
    """当前纯色名；渐变开启时返回 ``'gradient'``。"""
    if _gradient_mode:
        return "gradient"
    return _current_style


def is_gradient() -> bool:
    return _gradient_mode is not None


def gradient_mode() -> str | None:
    return _gradient_mode


# ---------------------------------------------------------------------------
# 通知宿主终端
# ---------------------------------------------------------------------------
#: 私有 OSC 号。pyte 对未知 OSC 是"读完即丢"（既不显示也不报错），
#: 正好拿来当 shell -> 宿主终端 的私有信道，不会污染屏幕内容。
OSC_GRADIENT = 777


def gradient_osc(mode: str | None = None,
                 speed: float | None = None) -> str:
    """构造通知宿主终端的私有 OSC。
    宿主（比如我们的 Textual 组件）收到后会开启"主动渐变"：
    持续重绘整屏，让所有带颜色的文字随时间流动。
    普通终端不认识这个序列，会直接忽略，不影响任何行为。
    """
    if mode is None:
        mode = _gradient_mode
    if mode is None:
        payload = "off"
    else:
        payload = f"{mode};{GRADIENT_SPEED if speed is None else speed}"
    return f"\033]{OSC_GRADIENT};jiahao;gradient={payload}\007"


def emit_gradient_osc(mode: str | None = None,
                      speed: float | None = None) -> None:
    """把渐变状态写到 stdout。

    只在 stdout 是 TTY 时发 —— 管道/重定向场景下没有宿主终端，
    发了只会污染输出（也会让测试输出变脏）。
    """
    import sys
    try:
        if not sys.stdout.isatty():
            return
        sys.stdout.write(gradient_osc(mode, speed))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ===========================================================================
# 给子进程的输出上色
# ===========================================================================
# SGR 是**终端状态**而不是进程状态：只要在 fork 之前把颜色写进终端，
# 子进程（ls / cat / 任何外部命令）的输出就会自动继承这个颜色。
# 这是唯一能给任意外部命令输出上色、又不需要接管它的 stdout 的办法。
ANSI_RESET = "\033[0m"


def ansi_prefix() -> str:
    """返回"让后续输出带上当前配色"的转义序列（默认配色时返回空串）。"""
    if _gradient_mode:
        # 渐变模式下取**当前时刻**的色相：于是每条外部命令的输出
        # 拿到彩虹里的一个颜色，跟着时间走，与时间渐变一致。
        # 局限：SGR 是终端状态，一次只能存一个颜色，所以外部命令的
        # 输出**没有空间渐变**（提示符和内置命令有）。
        return f"\033[{_hue_to_sgr(_time_offset())}m"
    params = _sgr_fg(_current_style)
    return f"\033[{params}m" if params else ""


def _write_raw(seq: str, fd: int = 1) -> None:
    """绕过 Python 缓冲直接写 fd，保证与子进程输出的先后顺序。"""
    if not seq:
        return
    import sys
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        os.write(fd, seq.encode("ascii"))
    except OSError:
        pass


def _stdout_is_tty() -> bool:
    """stdout 是不是终端。

    管道/重定向时**不能**往输出里塞转义序列，否则
    ``jiahaoshell | grep ...`` 会收到一堆 ESC 垃圾。
    测试可以覆盖这个函数来模拟 TTY。
    """
    import sys
    try:
        return bool(sys.stdout.isatty())
    except (OSError, ValueError):
        return False


def _stderr_is_tty() -> bool:
    """stderr 是不是终端。

    重定向到文件时（``jiahaoshell 2>err.log``）不该往里塞转义序列。
    测试可以覆盖这个函数来模拟 TTY。
    """
    import sys
    try:
        return bool(sys.stderr.isatty())
    except (OSError, ValueError):
        return False


def child_style_prefix() -> str:
    """子进程自己报错时要用的转义前缀。

    子进程**自带颜色**是必要的：前台还能靠父进程设好的环境 SGR 状态，
    后台任务没有那东西（父进程不能为异步子进程维持终端状态）。
    stderr 不是终端时返回空串，避免污染重定向的文件。
    """
    return ansi_prefix() if _stderr_is_tty() else ""


def emit_style_prefix(fd: int = 1) -> None:
    """外部命令执行**前**调用，让后续写到 ``fd`` 的输出继承当前配色。

    ``fd=2`` 是给**子进程的 stderr** 用的：stdout 走管道转发、颜色由
    StreamPainter 逐字符负责，那个环境 SGR 只会往 stdout 里塞多余转义。
    """
    if not _stdout_is_tty():
        return
    if fd != 1 and not os.isatty(fd):
        return                      # 目标不是终端，别把转义写进文件
    _write_raw(ansi_prefix(), fd)


def emit_style_reset(fd: int = 1) -> None:
    """外部命令执行**后**调用，复位终端颜色状态。

    没设配色时不写 —— 默认模式下输出保持逐字节干净，
    不会平白多出 ``ESC[0m``。
    """
    if not has_active_color() or not _stdout_is_tty():
        return
    if fd != 1 and not os.isatty(fd):
        return
    _write_raw(ANSI_RESET, fd)


#: 流式渐变一圈跨越多少个字符。
#: 流式场景（外部命令输出）事先不知道行长，做不到"整行铺满一个周期"，
#: 只能固定波长 —— 每 N 个字符走完一圈彩虹。
STREAM_CYCLE_CHARS = 60


def has_active_color() -> bool:
    """当前有没有设置配色（纯色或渐变）。"""
    return _gradient_mode is not None or bool(_current_style)


def wants_stdout_relay() -> bool:
    """是否该接管外部命令的 stdout。

    只要设了配色就**必须**接管 —— 不是为了让渐变更好看，而是因为
    ``tree`` / ``ls --color`` / ``grep --color`` 这类程序在 stdout 是终端时
    会自己上色，并且**每条都发 ``\\x1b[0m`` 复位**，把我们的 SGR 状态
    整个清掉：

        ^[[01;34m.^[[0m          <- tree 自己的颜色 + 复位
        ^[[01;34mjiahao^[[0m

    结果就是"设了颜色但输出没颜色"。把它们接到管道上，它们就不再
    自作主张（看到非 tty 会自动关掉着色），我们的配色才真正生效。

    代价是子进程的 stdout 不再是 tty，所以交互式程序要跳过 ——
    见 ``exec.RELAY_SKIP``。
    """
    return has_active_color() and _stdout_is_tty()


class StreamPainter:
    """把流式文本刷上当前配色。

    用在外部命令上：它们的 stdout 被接到管道，我们读到之后上色再转发。

    两种模式：

    * **渐变** —— 逐字符按列号上色，列号跨 chunk 维护、遇换行归零，
      所以每一行都从彩虹开头起步。
    * **纯色** —— 发一次颜色；如果子进程自己发了复位序列（有些程序
      即使输出到管道也会用 ``--color=always`` 上色），把我们的颜色补回去。
    """

    __slots__ = ("mode", "cycle", "column", "_last_sgr", "_time", "_solid")

    def __init__(self, mode: str | None = None,
                 cycle: int = STREAM_CYCLE_CHARS) -> None:
        #: 渐变模式；``None`` 表示用纯色
        self.mode = _gradient_mode if mode is None else mode
        self.cycle = max(1, int(cycle))
        self.column = 0
        self._last_sgr: str | None = None
        # 时间相位在**一次命令内固定**：否则输出到一半颜色会整体跳一下
        self._time = _time_offset() if self.mode in ("anim", "both") else 0.0
        #: 纯色模式下要用的 SGR 参数
        self._solid = None if self.mode else _sgr_fg(_current_style)

    # ------------------------------------------------------------------
    def paint(self, text: str) -> str:
        if not text:
            return ""
        if self.mode is None:
            return self._paint_solid(text)
        return self._paint_gradient(text)

    # ------------------------------------------------------------------
    def _paint_solid(self, text: str) -> str:
        if not self._solid:
            return text
        seq = f"\033[{self._solid}m"
        # 子进程自己发的复位会清掉我们的颜色，补回去
        if ANSI_RESET in text:
            text = text.replace(ANSI_RESET, ANSI_RESET + seq)
        if self._last_sgr is None:
            self._last_sgr = self._solid
            return seq + text
        return text

    def _paint_gradient(self, text: str) -> str:
        spatial = self.mode in ("static", "both")
        parts: list[str] = []

        for ch in text:
            if ch == "\n":
                self.column = 0
                self._last_sgr = None      # 新的一行，重新发色
                parts.append(ch)
                continue

            hue = (self.column / self.cycle) if spatial else 0.0
            hue = (hue * GRADIENT_CYCLES + self._time) % 1.0
            sgr = _hue_to_sgr(hue)
            if sgr != self._last_sgr:
                parts.append(f"\033[{sgr}m")
                self._last_sgr = sgr
            parts.append(ch)
            self.column += 1

        return "".join(parts)

    def finish(self) -> str:
        """收尾：需要的话发一个复位。"""
        return ANSI_RESET if self._last_sgr is not None else ""


# ===========================================================================
# 渐变渲染
# ===========================================================================
def gradient_text(text: str, mode: str | None = None) -> Text:
    """把文本渲染成渐变的 Rich ``Text``。

    相邻同色字符合并成一个 span（而不是逐字符 append），
    这是渲染开销和输出体积的大头。
    """
    mode = mode or _gradient_mode or "both"
    total = len(text)
    offset = _time_offset() if mode in ("anim", "both") else 0.0

    out_text = Text(no_wrap=False)
    run: list[str] = []
    run_style: Style | None = None

    for i, ch in enumerate(text):
        hue = _hue_at(i, total, mode, offset)
        style = _style_for_hue_step(int(hue * _HUE_STEPS))
        if style is not run_style:
            if run:
                out_text.append("".join(run), run_style)
            run = [ch]
            run_style = style
        else:
            run.append(ch)

    if run:
        out_text.append("".join(run), run_style)
    return out_text


def _gradient_ansi(text: str, mode: str, readline: bool = False) -> str:
    """渐变的裸 ANSI 形式。

    ``readline=True`` 时每个转义序列都用 ``\\001..\\002`` 包住，
    告诉 readline 这段不占显示宽度（提示符需要）；
    末尾**不复位**，让颜色延续到用户输入的内容上。

    相邻同色字符同样合并，减少转义序列数量。
    """
    total = len(text)
    offset = _time_offset() if mode in ("anim", "both") else 0.0

    def wrap(seq: str) -> str:
        return f"\001{seq}\002" if readline else seq

    parts: list[str] = []
    run: list[str] = []
    run_hue: float | None = None

    def flush() -> None:
        if not run:
            return
        parts.append(wrap(f"\033[{_hue_to_sgr(run_hue)}m"))
        parts.append("".join(run))

    for i, ch in enumerate(text):
        hue = _hue_at(i, total, mode, offset)
        if hue != run_hue:
            flush()
            run = [ch]
            run_hue = hue
        else:
            run.append(ch)
    flush()
    return "".join(parts)


# ===========================================================================
# 输出
# ===========================================================================
def _rich_style(style: str) -> Style | None:
    """内部配色字符串 -> Rich ``Style``。

    要同时吃下三种东西：

    * 具名色 / 复合样式 —— ``red``、``bold red``、``italic cyan``
    * 256 色索引 —— ``196``。**不能**直接丢给 Rich，它只认
      ``color(196)`` 那种写法，裸数字会抛 ``MissingStyle``，
      于是 `color 196` 之后每一条内置命令输出都会崩。
    * 真彩色 —— ``#ff8800`` / ``ff8800``

    最后一步用 ``Style.parse`` 而不是 ``Style(color=...)``：
    后者只接受**颜色**，遇到 ``bold red`` 会直接抛 ColorParseError
    （启动横幅用的就是它）。
    """
    if not style:
        return None
    # 别名兜底：normalize() 通常会做，但 set_style() 也可能被直接调用
    style = ALIASES.get(style, style)

    if style.isdigit():
        n = int(style)
        if 0 <= n <= 255:
            return Style(color=RichColor.from_ansi(n))

    if len(style) == 6:
        try:
            int(style, 16)
        except ValueError:
            pass
        else:
            return Style(color="#" + style)

    try:
        return Style.parse(style)
    except Exception:                       # noqa: BLE001
        # 样式写坏了不该把整个 shell 带崩，退回默认色就行
        return None


def out(*args, style=None):
    """打印一行。

    * ``style`` 显式给出 -> 用该样式（错误提示、作业消息等用它做语义区分）
    * 否则渐变开启   -> 整行渐变
    * 否则           -> 当前纯色
    """
    if style is None and _gradient_mode:
        text = " ".join(str(a) for a in args)
        console.print(gradient_text(text, _gradient_mode))
        return

    s = style if style is not None else _current_style
    console.print(*args, style=_rich_style(s))


def _readline_active() -> bool:
    """真的启用了 readline 吗？

    ★ 不能只看"能不能 import"。判断的是**实际接管 input() 的是谁**：
      有 readline 时 GNU readline 处理提示符；没有时（Windows）走
      ``_PyOS_WindowsConsoleReadline``，那条路**不认** ``\001``/``\002``。
    """
    return "readline" in sys.modules or "pyreadline3" in sys.modules


def prompt(readline_safe: bool | None = None) -> str:
    """提示符字符串，颜色/渐变延续到输入行。

    ``readline_safe`` 决定要不要用 ``\001``/``\002`` 把转义序列包起来 ——
    那是 **GNU readline 专用**的"这段不占显示宽度"标记。

    默认自动判断（见 ``_readline_active``）。**Windows 上必须是 False**：
    那边没有 GNU readline，``input()`` 走的是
    ``_PyOS_WindowsConsoleReadline``，它用 ``WriteConsoleW`` 原样写出提示符，
    收到裸的 ``\001``/``\002`` 控制字符就乱了。
    """
    if readline_safe is None:
        readline_safe = _readline_active()
    cwd = os.getcwd()

    if _gradient_mode:
        return _gradient_ansi(cwd, _gradient_mode, readline=readline_safe) + " # "

    params = _sgr_fg(_current_style)
    if params:
        seq = f"\033[{params}m"
        if readline_safe:
            return f"\001{seq}\002{cwd} # "
        return f"{seq}{cwd} # "
    if readline_safe:
        return f"\001\033[34m\002{cwd}\001\033[0m\002 # "
    return f"\033[34m{cwd}\033[0m # "


def write_prompt(readline_safe: bool | None = None) -> None:
    """把提示符写到 stdout（**不换行**）并 flush。

    ★ **不要用 ``input(prompt)``。**

    真 Windows 上踩过这个坑：``input()`` 带提示符参数时会走
    ``_PyOS_WindowsConsoleReadline``，用 ``WriteConsoleW`` 直接往控制台写，
    而且不认 readline 那套 ``\001``/``\002`` 标记 —— 结果是
    **提示符完全不显示**，但命令照常能跑，所以特别难发现
    （以为是 ConPTY 数据没通，其实是提示符自己没写出来）。

    自己写出来就稳了：颜色和渐变都还在，内嵌终端（走 pyte 渲染）里也正常。

    输出失败也**不抛异常** —— 顶多没提示符，不该把 shell 弄死。
    """
    text = prompt(readline_safe)
    if not text:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeError):
        pass
