"""代码雨 —— 素材真从工作区里抓。

不是随便撒日文假名：这里会扫一遍项目源码，统计**实际出现过的字符**，
按频率加权当雨滴。所以下下来的每一帧，用的都是你自己代码的"字母表"。

经典 Matrix 观感 + 一点私货：

* 头部亮白，尾部沿绿色渐隐
* 每列速度/长度不同，避免整齐划一的假感
* 一列跑到底就重新投一次，并随机换速
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections import Counter
from itertools import islice
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

#: 帧率。雨比地球慢一点更像"滴落"。
FPS = 14
#: 每列尾巴最长多少格
MAX_TAIL = 22
#: 扫描上限，免得在大仓库里卡住
MAX_FILES = 400
#: 单个字符在池子里的权重上限。不压的话 e/t/a 会占掉大半个池子，
#: 雨滴就只剩几个字母了。
WEIGHT_CAP = 24


def _single_width(ch: str) -> bool:
    """这个字符占一格吗？

    **必须过滤**：源码注释里有中文（全角，占两格），混进雨滴会让整列错位。
    """
    return (ch.isprintable()
            and unicodedata.east_asian_width(ch) in ("Na", "H"))

#: 抓不到的兜底字符集
FALLBACK = list("01<>{}[]()/\\|_+-=*&^%$#@!?;:abcdefABCDEF")

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: 这些符号也按真实出现频率掺进去。只抓标识符的话雨里全是字母，
#: 少了括号和运算符就不像代码了。
_SYMBOLS = frozenset("(){}[]<>=+-*/\\|&^%$#@!?;:.,'\"~`_")


def default_root() -> Path:
    """默认素材目录：本项目源码。"""
    # src/jiahao/widgets/rain.py -> src/jiahao
    return Path(__file__).resolve().parents[1]


def build_glyph_pool(root: Path, limit_files: int = MAX_FILES) -> list[str]:
    """扫源码，返回按出现频率加权后的字符表。

    为什么用"项目里真实出现过的字符"而不是随机字母：
    屏幕上会自然浮现 ``_``、``(``、``self`` 这类痕迹，看着就像在写这个项目。
    """
    counter: Counter[str] = Counter()
    try:
        paths = islice(sorted(root.rglob("*.py")), limit_files)
        for path in paths:
            counter.update(path.stem)                 # 文件名
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            # 只统计**标识符内部**的字符 + 代码符号。
            # 注释里的自然语言（尤其中文）不掺进来，不然雨滴会变成散文。
            for word in _WORD_RE.findall(text):
                counter.update(word)                  # ★ 逐字符，不是整词
            counter.update(ch for ch in text if ch in _SYMBOLS)
    except OSError:
        pass

    # 按真实频率加权展开成"抽签池"，但单个字符的权重压个上限
    pool: list[str] = []
    for ch, n in counter.most_common():
        if not _single_width(ch):
            continue
        pool.extend([ch] * min(n, WEIGHT_CAP))
    return pool or list(FALLBACK)


def _trail_style(age: float) -> Style:
    """age: 0 = 头部，1 = 尾端。亮绿 -> 暗绿。"""
    age = min(1.0, max(0.0, age))
    red = int(0x10 * (1.0 - age))
    green = int(0x30 + 0xC0 * (1.0 - age) ** 1.6)
    blue = int(0x18 + 0x50 * (1.0 - age) ** 2)
    return Style(color=f"#{red:02x}{green:02x}{blue:02x}")


#: 头尾样式预先算好，render 里只做查表 —— 每帧上百个 Style 对象太浪费
_STYLES = [_trail_style(i / 16.0) for i in range(17)]
_HEAD_STYLE = Style(color="bright_white", bold=True)
_DIM_STYLE = Style(color="#0a3a12")


class _Drop:
    """一列雨。"""

    __slots__ = ("head", "speed", "tail", "glyphs")

    def __init__(self, height: int, rng: random.Random, pool: list[str]) -> None:
        self.reset(height, rng, pool, start_anywhere=True)

    def reset(self, height: int, rng: random.Random, pool: list[str],
              start_anywhere: bool = False) -> None:
        self.speed = rng.uniform(0.35, 1.15)
        self.tail = rng.randint(6, MAX_TAIL)
        if start_anywhere:
            # 首帧就要有雨：撒在**可见范围**里。
            # 撒 -height..height 的话一半在屏幕外，开头几秒是空屏。
            self.head = rng.uniform(0, height)
        else:
            # 跑到底后从顶部重新投
            self.head = rng.uniform(-self.tail, 0)
        self.glyphs = [rng.choice(pool) for _ in range(self.tail + 2)]


class CodeRain(Widget):
    """下落中的代码雨。

    素材来源由 ``root`` 决定，默认是本项目源码目录；
    扫不到任何文件时退回一组通用符号，不会开天窗。
    """

    DEFAULT_CSS = """
    CodeRain {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, root: Path | str | None = None, fps: int = FPS,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._root = Path(root) if root is not None else default_root()
        self._fps = max(1, fps)
        self._rng = random.Random(0xC0DE)      # 固定种子：重启后同一套节奏
        self._pool: list[str] = []
        self._drops: list[_Drop] = []
        self._cols = 0
        self._rows = 0

    # ---------------------------------------------------------------- 生命周期
    def on_mount(self) -> None:
        self._pool = build_glyph_pool(self._root)
        self._resize(self.size.width, self.size.height)
        self.set_interval(1.0 / self._fps, self._tick)

    def on_resize(self, event) -> None:
        self._resize(event.size.width, event.size.height)

    def _resize(self, width: int, height: int) -> None:
        if width == self._cols and height == self._rows:
            return
        self._cols, self._rows = width, height
        pool = self._pool or list(FALLBACK)
        # 隔一列下一列：满屏雨点会糊成一坨，留白才看得清
        self._drops = [_Drop(height, self._rng, pool)
                       for _ in range((width + 1) // 2)]

    def _tick(self) -> None:
        for drop in self._drops:
            drop.head += drop.speed
            if drop.head - drop.tail > self._rows:
                drop.reset(self._rows, self._rng, self._pool or list(FALLBACK))
        self.refresh()

    # ------------------------------------------------------------------ 渲染
    def render(self) -> Text:
        width, height = self.size.width, self.size.height
        if width <= 0 or height <= 0:
            return Text()

        # 每格记 (字符, 年龄)；后画的覆盖先画的，同格取"更新"的那滴
        cells: dict[tuple[int, int], tuple[str, int]] = {}
        for idx, drop in enumerate(self._drops):
            col = idx * 2
            if col >= width:
                break
            head = int(drop.head)
            for k in range(drop.tail):
                row = head - k
                if not (0 <= row < height):
                    continue
                ch = drop.glyphs[k % len(drop.glyphs)]
                age = int(k / max(1, drop.tail) * 16)
                key = (row, col)
                prev = cells.get(key)
                if prev is None or age < prev[1]:
                    cells[key] = (ch, age)

        text = Text(no_wrap=True, overflow="crop")
        for row in range(height):
            if row:
                text.append("\n")
            run_chars: list[str] = []
            run_age = -1

            def flush() -> None:
                if not run_chars:
                    return
                style = _HEAD_STYLE if run_age == 0 else (
                    _STYLES[run_age] if run_age <= 16 else _DIM_STYLE)
                text.append("".join(run_chars), style=style)

            for col in range(width):
                got = cells.get((row, col))
                if got is None:
                    flush()
                    run_chars = []
                    run_age = -1
                    text.append(" ")
                    continue
                ch, age = got
                if age != run_age:
                    flush()
                    run_chars = [ch]
                    run_age = age
                else:
                    run_chars.append(ch)
            flush()
        return text
