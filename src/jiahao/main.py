import sys

from jiahao.term import MyTerminal
from jiahao.utils import anim


def main(argv=None):
    """TUI 入口。

    支持两个开关（都会写进环境变量，TUI 里拉起的内层 shell 自动继承，
    不需要建 ``Terminal`` 时再传一遍）：

    * ``--no-con`` —— 关掉所有动画
    * ``--speed N`` —— 节奏倍率，>1 更慢，<1 更快

    例：``jiahao --speed 2``（慢镜头）/ ``jiahao --no-con``（测试用）。
    """
    anim.parse_flags(sys.argv[1:] if argv is None else argv)

    # --no-con 和 --speed 一起给是自相矛盾的：动画都关了，节奏没有意义。
    # 静默忽略会让人以为 --speed 没生效，所以在 TUI 接管屏幕之前说一句。
    warn = anim.conflict_warning()
    if warn:
        print(f"jiahao: {warn}", file=sys.stderr)

    MyTerminal().run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BaseException:
        from jiahao.jiahaoshell import _crash_guard
        _crash_guard("jiahao", "jiahao_crash.log")
        raise
