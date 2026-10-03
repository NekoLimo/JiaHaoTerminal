"""旋转的 ASCII 地球。

**算法直接移植自 aem1k.com/world**（Martin Kleppe 的 JS 代码高尔夫作品），
只做了两件事：把注释剔掉还原成可读逻辑、把绝对时间换成相对时间。

原版源码把注释排成了地球形状，所以"查看源代码"本身就是作品的一部分 ——
这里不复刻那一层，只要球。

## 原算法在干什么

``RAW`` 按字符 ``4`` 切成 15 段，每段是 36 进制数，**各位数字之和恰好都是 20**：

    y=0  46514   -> 4+6+5+1+4 = 20
    y=6  4331162 -> 4+3+3+1+1+6+2 = 20
    ...

每个数字 d 让 ``x`` 以 0.05 为步长漂移 d 次，所以**每行 x 恰好走满 20.0**，
相位 ``o = t - x/π`` 正好扫过一整圈。行与行之间靠
``sin(0.5 + y/7)`` 收窄 —— 这就是球体的纬度轮廓。

``r`` 每换一个数字翻转一次，决定这一笔记 1 还是 2。相邻两格相加后查表
``"   *#"``：只有"1+2"才出 ``*``，只有"2+2"才出 ``#``，于是出现纹理而不是实心块。
"""

from __future__ import annotations

import math
import os
import pathlib
import unicodedata

from rich.text import Text
from textual.widget import Widget

#: aem1k 原串（注释已剔除 —— parseInt 遇空格即停，注释不影响结果）
RAW = ("zw24l6k4e3t4jnt4qj24xh2 x42kty24wrt413n243n"
       "9h243pdxt41csb yz43iyb6k43pk7243nmr24")

#: 每行 x 的漂移步长
STEP = 0.05
#: 查表：相邻两格之和 -> 字符
PALETTE = "   *#"

#: 原生参数。RADIUS **就是球宽（字符数）**，CENTER+1 **就是行宽** ——
#: 两个都是自由参数，所以球能缩放到任意尺寸，而不必硬拉伸/裁剪。
NATIVE_RADIUS = 32
NATIVE_CENTER = 60

#: 原生尺寸
NATIVE_COLS = NATIVE_CENTER + 1
NATIVE_ROWS = len(RAW.split("4")) - 1        # 最后一段是空的，不计
#: 宽高比：字符格是 2:1，球宽 = 2 * 行数 才是正圆
ASPECT = 2.0

#: 每秒推进多少相位（原版用绝对时间，这里给定值，保证可复现）
SPEED = 1.0
FPS = 20
#: 球四周留几格空。
#:
#: 既用来算球的最大尺寸（avail = 面板 - 2*MARGIN），也保证最外一圈干净。
#: 设 1 的话球会贴着边框，2 才有"悬浮"的感觉。
MARGIN = 2
#: 环境变量：临时试缩放效果，不用改代码
SCALE_ENV = "JIAHAO_GLOBE_SCALE"


def default_scale() -> float:
    """默认缩放系数。

    想临时看效果就 ``JIAHAO_GLOBE_SCALE=0.6 jiahao``，
    不想每次都设就在 ``term.py`` 里写死。
    """
    raw = os.environ.get(SCALE_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 1.0


def _parse_int36(text: str) -> int | None:
    """JS 的 ``parseInt(text, 36)``：从头解析，遇到非法字符就停。"""
    out = ""
    for ch in text:
        if ch.isdigit() or ("a" <= ch.lower() <= "z"):
            out += ch
        else:
            break
    return int(out, 36) if out else None


#: 每行的 36 进制参数，启动时算一次
ROW_PARAMS = tuple(_parse_int36(p) for p in RAW.split("4"))
ROW_PARAMS = tuple(p for p in ROW_PARAMS if p is not None)


# ---------------------------------------------------------------------------
# 底纹
# ---------------------------------------------------------------------------
# ★ 球体**不是画在空白上的**。
#
# 原版把源码文字铺在球体以外的格子里，靠这层噪点把中间的 *# 衬托出来。
# 少了它，球边缘的锯齿直接暴露在白底上，怎么看都不圆 —— 这是视觉上的
# 对比度问题，不是几何问题，光调半径没用。
#
# 素材就用本模块自己的源码：和原版"拿自己源码当底"是同一个梗。
def _build_filler() -> str:
    """底纹字符表：源码里所有**单宽**的非空白字符。

    必须滤掉全角：中文注释占两格，混进去整张网格会错位。
    """
    try:
        src = pathlib.Path(__file__).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        src = RAW * 12
    chars = [c for c in src if not c.isspace() and _single_width(c)]
    return "".join(chars) or RAW * 12


def _single_width(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("Na", "H")


FILLER = _build_filler()


def _row_cells(y, value, radius, center, t):
    """算一行里被点亮的格子。``y`` 可以是小数（缩放时用）。"""
    cells = {}
    x = 0.0
    # ★ r 的初值是**假值**。
    #
    # 原版 JS 里写的是 r=[]（数组，真值），首次 !r 得 false，对应 -~false = 1。
    # 翻成 Python 就是 r=False -> not r -> True -> 记 1。
    #
    # 这里踩过坑：一度"想当然"改成 True，整个纹理反相，
    # 和 node 跑的原版逐字对不上。下面 verify_against_node 就是为此留的。
    r = False
    for digit in str(value):
        r = not r
        i = 0.0
        limit = int(digit)
        while i < limit:
            x -= STEP
            o = t - x / math.pi
            if math.cos(o) > 0:
                idx = int(radius * math.sin(o) * math.sin(0.5 + y / 7)) + center
                cells[idx] = 1 if r else 2
            i += STEP
    return cells


def margin_for(cols: int, rows: int) -> int:
    """该留几格空。

    面板够大就留 ``MARGIN``（球"悬浮"着好看）；太小就别硬留了，
    否则球被挤成一个点 —— 14x8 那种尺寸下留白比球还占地方。
    """
    if cols >= 14 and rows >= 9:
        return MARGIN
    if cols >= 6 and rows >= 5:
        return 1
    return 0


def _fit(cols, rows, scale):
    """定出球宽 ``w`` 和行数 ``h``：放得下、且锁死 2:1 正圆。"""
    m = margin_for(cols, rows)
    avail_w = max(4, cols - 2 * m)
    avail_h = max(3, rows - 2 * m)

    h = min(avail_h, NATIVE_ROWS)          # 纵向不超过原生 15 行
    w = int(round(h * ASPECT))
    if w > avail_w:                        # 面板太窄就按宽度反推
        w = avail_w
        h = int(round(w / ASPECT))

    if scale != 1.0:
        w = int(round(w * scale))
        h = int(round(h * scale))

    h = max(3, min(h, avail_h))
    w = max(4, min(w, avail_w))
    # 两步取整可能破坏比例，最后再锁一次
    h = max(3, min(h, int(round(w / ASPECT))))
    w = max(4, min(w, int(round(h * ASPECT))))
    return w, h


def _resample(count):
    """把 15 组行参数重采样到 ``count`` 行。

    ``y`` 保持浮点，让 ``sin(0.5 + y/7)`` 的纬度轮廓平滑；
    参数本身是离散数字，取最近的一组。
    """
    if count >= NATIVE_ROWS:
        return [(float(y), ROW_PARAMS[y]) for y in range(NATIVE_ROWS)]
    if count <= 1:
        return [(7.0, ROW_PARAMS[7])]
    step = (NATIVE_ROWS - 1) / (count - 1)
    out = []
    for i in range(count):
        y = i * step
        out.append((y, ROW_PARAMS[int(round(y))]))
    return out


def disc(cols: int, rows: int, scale: float = 1.0, pad: float = 0.0):
    """球的**外接椭圆盘**的判定函数。

    为什么不用逐帧的 ``*#`` 来算"周围一圈"：那只是球的一个切片，
    边界坑坑洼洼，照着它留白会得到一圈锯齿。用几何圆盘才平滑。

    ``pad`` 往外扩几格 —— 这就是"球周围空出的那一圈"。
    """
    w, h = _fit(cols, rows, scale)
    cx = cols / 2.0
    cy = (rows - h) / 2.0 + h / 2.0
    rx = max(0.5, w / 2.0 + pad)
    ry = max(0.5, h / 2.0 + pad)

    def inside(c: int, r: int) -> bool:
        dx = (c + 0.5 - cx) / rx
        dy = (r + 0.5 - cy) / ry
        return dx * dx + dy * dy <= 1.0
    return inside


def grid(cols: int, rows: int, t: float, spin: float = SPEED,
         scale: float = 1.0, background: bool = True, halo: int = 1):
    """返回 ``rows`` x ``cols`` 的网格，每格是 ``(字符, 是否属于球体)``。

    两层：先按需铺底纹，再把球盖上去。``是否属于球体`` 交给调用方决定配色 ——
    有了它才能把球点亮、把底纹压暗，对比度全靠这个。

    ``background=True`` 时，球**以外**的格子铺上源码文字（四个角也铺满），
    但紧贴球的一圈（``halo`` 格）保持干净 —— 球是"浮"在代码里的，
    直接贴上去会糊成一团。
    """
    cols, rows = max(0, int(cols)), max(0, int(rows))
    if cols == 0 or rows == 0:
        return []

    w, h = _fit(cols, rows, scale)
    radius = w                             # 球宽 == 半径（半格单位）
    center = cols - 1                      # 每行 cols 个字符

    # ---- 第一层：底纹 ----
    # 只在球的外接圆盘 + halo 之外铺字，圆盘之内留白。
    inside = disc(cols, rows, scale, pad=halo) if background else None
    out = []
    for r in range(rows):
        row = []
        for c in range(cols):
            if inside is None or inside(c, r):
                row.append((" ", False))
            else:
                row.append((FILLER[(r * cols + c) % len(FILLER)], False))
        out.append(row)

    # ---- 第二层：球盖上去 ----
    top = (rows - h) // 2
    for i, (y, value) in enumerate(_resample(h)):
        r = top + i
        if not (0 <= r < rows):
            continue
        cells = _row_cells(y, value, radius, center, t * spin)
        for k in range(0, 2 * cols, 2):
            ch = PALETTE[min(cells.get(k, 0) + cells.get(k + 1, 0),
                             len(PALETTE) - 1)]
            c = k // 2
            if ch != " " and 0 <= c < cols:
                out[r][c] = (ch, True)
    return out


def render(cols: int, rows: int, t: float, spin: float = SPEED,
           scale: float = 1.0, background: bool = True, halo: int = 1):
    """纯文本版本（测试和预览用）。

    默认铺底纹：球**以外**的格子填源码文字（四个角也填满），
    紧贴球的一圈留白 —— 球"浮"在代码里，直接贴上去会糊成一团。
    ``background=False`` 只要光秃秃一个球。

    无论尺寸多离谱都返回 ``max(0, rows)`` 行、每行 ``max(0, cols)`` 格，
    调用方可以无脑拼接。
    """
    cols, rows = max(0, int(cols)), max(0, int(rows))
    g = grid(cols, rows, t, spin, scale, background, halo)
    if not g:
        return [" " * cols] * rows
    if not background:
        g = [[(ch if is_globe else " ", is_globe) for ch, is_globe in row]
             for row in g]
    return ["".join(ch for ch, _ in row) for row in g]


def native_frame(t):
    """按原生分辨率（61x15）算一帧 —— 和原版逐字一致，**不带底纹**。"""
    rows = []
    for y, value in enumerate(ROW_PARAMS):
        cells = _row_cells(float(y), value, NATIVE_RADIUS, NATIVE_CENTER, t)
        line = []
        for k in range(0, 2 * NATIVE_COLS, 2):
            total = cells.get(k, 0) + cells.get(k + 1, 0)
            line.append(PALETTE[min(total, len(PALETTE) - 1)])
        rows.append("".join(line))
    return rows


#: 球体配色：``#`` 比 ``*`` 亮；底纹压到很暗，靠对比度把球"推"出来
GLOBE_STYLES = {"#": "bold #7dffa0", "*": "#2fbf5f"}
BG_STYLE = "#12401f"


class Globe(Widget):
    """会转的 ASCII 地球。

    画面分三层：

    1. **球体** —— ``*`` / ``#``，算法算出来的
    2. **周围一圈留白** —— ``halo`` 格，照着球的外接圆盘留，
       所以是平滑的一圈；紧贴着 ``*#`` 的锯齿留会得到一圈毛边
    3. **其余全部铺源码文字** —— 四个角也铺满（``background=True``）

    球因此是"浮"在一片代码里的。想只看光球就 ``Globe(background=False)``。

    特性：

    * 按面板**自动缩放**，宽高比锁死 2:1，任何尺寸都是正圆
    * 纵向不超过原生的 15 行（算法分辨率上限，硬拉伸会出现条带）

    ``scale`` 用来手动调：
        Globe()                        尽量填满面板
        Globe(scale=0.6)               缩到 6 成，留出呼吸空间
        JIAHAO_GLOBE_SCALE=0.6 jiahao  不改代码临时试
    """

    DEFAULT_CSS = """
    Globe {
        width: 1fr;
        height: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, scale: float | None = None, fps: int = FPS,
                 background: bool = True, halo: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        # scale=None 表示"看环境变量"，方便临时调；显式传值则覆盖
        self._scale = default_scale() if scale is None else max(0.05, float(scale))
        self._fps = max(1, fps)
        self._background = background
        self._halo = max(0, int(halo))
        self._t = 0.0

    @property
    def scale(self) -> float:
        return self._scale

    @scale.setter
    def scale(self, value: float) -> None:
        self._scale = max(0.05, float(value))
        self.refresh()

    def on_mount(self) -> None:
        self.set_interval(1.0 / self._fps, self._tick)

    def _tick(self) -> None:
        self._t += 1.0 / self._fps
        self.refresh()

    def render(self) -> Text:
        g = grid(self.size.width, self.size.height, self._t, scale=self._scale,
                 background=self._background, halo=self._halo)

        text = Text(no_wrap=True, overflow="crop")
        for r, row in enumerate(g):
            if r:
                text.append("\n")
            # 合并同色相邻格，少产生一堆 span
            buf: list[str] = []
            style: str | None = None
            for ch, is_globe in row:
                want = (GLOBE_STYLES.get(ch, "green") if is_globe
                        else BG_STYLE)
                if want != style:
                    if buf:
                        text.append("".join(buf), style=style)
                    buf, style = [ch], want
                else:
                    buf.append(ch)
            if buf:
                text.append("".join(buf), style=style)
        return text
