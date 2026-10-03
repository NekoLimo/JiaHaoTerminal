"""动画开关与节奏。

**开关**（把仪式感整个关掉）：

* 命令行 ``--no-con``（别名 ``--no-anim`` / ``--fast`` / ``-q``）
* 环境变量 ``JIAHAO_NO_ANIM=1``

**节奏**（同样的动画，放慢或加快）：

* 命令行 ``--speed 2`` / ``--speed 0.5``
* 环境变量 ``JIAHAO_SPEED=2``

开关和节奏都会**写回环境变量**，所以 TUI 拉起的内层 shell 自动继承 ——
``jiahao --no-con --speed 2`` 只需要在最外层写一次。
"""

from __future__ import annotations

import os
import time

#: 触发关闭的写法
NO_ANIM_FLAGS = frozenset({"--no-con", "--no-anim", "--fast", "-q", "--quiet"})
#: 环境变量名
NO_ANIM_ENV = "JIAHAO_NO_ANIM"
#: 节奏倍率的环境变量名
SPEED_ENV = "JIAHAO_SPEED"
#: 设置节奏的 flag
SPEED_FLAGS = ("--speed", "--pace")

#: None 表示"还没被显式设置过"，此时看环境变量
_state: bool | None = None
#: 节奏倍率。>1 更慢，<1 更快。1.0 = 基准
_pace: float | None = None
#: 用户是否**显式**给过节奏（命令行或 speed 命令）。
#: 用来发现 "--no-con 和 --speed 同时给" 这种自相矛盾的组合。
_pace_explicit = False


def _env_disabled() -> bool:
    raw = os.environ.get(NO_ANIM_ENV, "")
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _env_pace() -> float:
    try:
        value = float(os.environ.get(SPEED_ENV, "") or 1.0)
    except ValueError:
        return 1.0
    return _clamp(value)


def _clamp(value: float) -> float:
    # 别让人手滑写成 0 或者 10000：0 会让动画"看起来没动"，
    # 太大就等于把 shell 卡死。
    return min(20.0, max(0.05, value))


def set_animations(enabled: bool) -> None:
    """显式开关动画。关掉时会写进环境变量让子进程继承。"""
    global _state
    _state = bool(enabled)
    if _state:
        os.environ.pop(NO_ANIM_ENV, None)
    else:
        os.environ[NO_ANIM_ENV] = "1"


def animations_enabled() -> bool:
    """当前动画是否开启。"""
    if _state is not None:
        return _state
    return not _env_disabled()


def set_pace(multiplier: float) -> None:
    """设置节奏倍率。>1 更慢，<1 更快。"""
    global _pace, _pace_explicit
    _pace = _clamp(float(multiplier))
    _pace_explicit = True
    os.environ[SPEED_ENV] = f"{_pace:g}"


def pace_is_explicit() -> bool:
    """用户是否显式设过节奏。

    用于检测 ``--no-con --speed 10`` 这种自相矛盾的组合：
    动画都关了，节奏根本没有意义，但静默忽略会让人以为 --speed 没生效。
    """
    return _pace_explicit


def conflict_warning() -> str | None:
    """参数自相矛盾时返回一句警告，否则 None。"""
    if _pace_explicit and not animations_enabled():
        return ("--no-con 关掉了动画，--speed 不会有任何效果。"
                "想慢放就别加 --no-con，或者在 shell 里用 anim on 打开。")
    return None


def pace() -> float:
    """当前节奏倍率。"""
    if _pace is not None:
        return _pace
    return _env_pace()


def parse_flags(argv) -> list[str]:
    """从参数里摘掉动画开关和节奏设置，返回剩下的参数。

    认这几种写法：``--no-con``、``--speed 2``、``--speed=2``。
    """
    rest: list[str] = []
    i = 0
    argv = list(argv)
    while i < len(argv):
        arg = argv[i]

        if arg in NO_ANIM_FLAGS:
            set_animations(False)
            i += 1
            continue

        if arg in SPEED_FLAGS and i + 1 < len(argv):
            try:
                set_pace(float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
            continue

        for flag in SPEED_FLAGS:
            if arg.startswith(flag + "="):
                try:
                    set_pace(float(arg.split("=", 1)[1]))
                except ValueError:
                    pass
                break
        else:
            rest.append(arg)

        i += 1

    return rest


def pause(seconds: float) -> None:
    """戏剧性停顿。

    动画关掉时**直接跳过**（不做无谓等待）；开着时按当前节奏倍率缩放。
    """
    if seconds <= 0 or not animations_enabled():
        return
    time.sleep(seconds * pace())

