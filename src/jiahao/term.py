import os
import sys

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Container

from jiahao.widgets import Terminal, Globe, CodeRain
from jiahao.utils import get_sysinfo
from jiahao.utils.compat import exe_name


def shell_command() -> str:
    """内层 shell 的可执行路径。

    **不能写死 ``./dist/jiahaoshell``** —— 打包成 exe 之后，双击运行时
    当前目录是任意的（可能是桌面、可能是 C:\\Windows）。所以按顺序找：

    1. 可执行文件旁边（打包分发的情形）
    2. 可执行文件旁边的 ``dist/``（开发时跑 ``dist/jiahao`` 的情形）
    3. 源码树里的 ``dist/``（普通开发）
    4. 兜底：交给 PATH 查
    """
    # ★ 打包成**单文件**时：拿自己当 shell 用，带个内部开关。
    #
    #   PyInstaller 单文件模式下 sys.executable 就是 exe 自己的路径，
    #   所以直接重复执行自己即可 —— 不用再分发第二个文件。
    #   走的就是本文件的 __main__（见 jiahao/__main__.py）。
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --shell-mode'

    name = exe_name("jiahaoshell")
    if getattr(sys, "_MEIPASS", None):
        base = os.path.dirname(os.path.abspath(sys.executable))
        cands = [os.path.join(base, name), os.path.join(base, "dist", name)]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        cands = [os.path.join(root, "dist", name)]

    for path in cands:
        if os.path.isfile(path):
            return path
    return name                # 交给 PATH，至少给个像样的错

info = get_sysinfo.sysinfo()


class MyTerminal(App):
    TITLE = "超级牛逼嘉豪终端"
    SUB_TITLE = "Powered by SixSeven"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        overflow: hidden;
    }
    #row1 {
        height: 3;
    }
    #row1 Static {
        width: 1fr;
        height: 3;
        overflow: hidden;
        border: solid $primary;
    }

    /* 主体：左 70% 终端，右 30% 观赏区 */
    #main {
        height: 1fr;
    }
    #term_wrap {
        width: 70%;
        height: 100%;
        overflow: hidden;
        /* 和右边两个面板用同一套边框语言，视觉上才是一整块仪表盘。
           加了边框后内层 Terminal 会窄 2 格，它会自己把新尺寸
           通过 TIOCSWINSZ 通知 pty，内层 shell 跟着重排。 */
        border: solid $primary;
        border-title-color: $accent;
        border-title-align: center;
    }
    #my_terminal {
        height: 1fr;
        width: 100%;
    }
    #side {
        width: 30%;
        height: 100%;
    }
    #globe {
        height: 1fr;
        border: solid $primary;
        border-title-color: $accent;
        border-title-align: center;
    }
    #rain {
        height: 1fr;
        border: solid $primary;
        border-title-color: $accent;
        border-title-align: center;
    }
    """

    BINDINGS = [
        # ★ 唯一可靠的退出键。
        #
        # 必须 priority=True：Terminal 组件在 on_key 里会 stop() 掉所有按键
        # （不然 Ctrl+C 传不进终端），普通的 App 级绑定根本轮不到。
        # 代价是终端收不到 ^Q（XON），对交互式 shell 无所谓。
        #
        # 不要用 ctrl+f1 当主要出路 —— 大量终端根本不发这个键。
        Binding("ctrl+q", "quit", "退出", priority=True, show=True),
        # 覆盖 App 默认的 `ctrl+c`（它是 priority=True，会在事件转发给 widget
        # **之前**就被消费掉，导致 Ctrl+C 永远送不到终端）。
        #
        # 绑定合并在 Textual 里是按 key 覆盖的，所以必须用同一个 key 才能顶掉它；
        # 同时去掉 priority，这样按键会先交给聚焦的 Terminal，被它 stop() 就不再
        # 触发退出。放开焦点之后 ctrl+c 依然可以退出 App。
        #
        # 对终端来说这是必须的：jiahaoshell 有真实作业控制，Ctrl+C 要发 SIGINT。
        Binding("ctrl+c", "quit", "Quit", show=False),
        # 放开焦点后的便捷退出（终止符已经由 ctrl+q 兜底）
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, *args, **kwargs) -> None:
        # ★ 必须在 super().__init__() 之前摘掉 NO_COLOR。
        #
        # Textual 在 App.__init__ 里会读 NO_COLOR，一旦存在就给整个 App
        # 追加一个 Monochrome() 渲染滤镜（textual/app.py:572）：
        #
        #     self.no_color = environ.pop("NO_COLOR", None) is not None
        #     if self.no_color:
        #         self._filters.append(NoColor() if self.ansi_color else Monochrome())
        #
        # 这个滤镜作用在渲染管线的**最后一步**，会把内嵌终端模拟出来的
        # 所有颜色一并抹成单色 —— 于是 shell 明明发了正确的真彩色转义，
        # 屏幕上却完全没有颜色。
        #
        # 本程序明确要求彩色（内嵌跑的就是一个彩色 shell，而且
        # jiahaoshell 自己还会设 FORCE_COLOR），所以这里主动摘掉。
        # 副作用是子进程也不再看到 NO_COLOR，正好让内层 shell 也保持彩色。
        os.environ.pop("NO_COLOR", None)
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="row1"):
            yield Static(info.get("userHost", "UNKNOWN"))
            yield Static(f"SYSTEM: {info.get('system', 'UNKNOWN')}")
            yield Static(info.get("unameRelease", "UNKNOWN"))
            yield Static(info.get("hardware"))
            yield Static("TOR: ENABLE")

        with Horizontal(id="main"):
            with Container(id="term_wrap") as term_wrap:
                # 子进程在 on_mount 时自动启动，不需要手动调 start()
                yield Terminal(command=shell_command(), id="my_terminal")
            term_wrap.border_title = "SHELL"

            with Vertical(id="side"):
                # 地球：算法移植自 aem1k.com/world，见 widgets/globe.py。
                #
                # 画面分三层：
                #   1. 球体（* #）
                #   2. 球周围一圈留白 —— 按球的**外接圆盘**留，所以是平滑的一圈
                #   3. 其余全铺源码文字 —— 四个角也铺满
                # 球因此"浮"在一片代码里。只看光球：Globe(background=False)
                # 留白宽度：Globe(halo=3)。素材换成别处：改 globe.FILLER
                #
                # 它会按面板**自动缩放**，宽高比锁死 2:1（任何尺寸都是正圆），
                # 纵向不超过原生的 15 行 —— 那是算法的分辨率上限，
                # 硬拉伸只会让行参数重复、出现条带。
                #
                # 嫌大就这么调：
                #   * 临时试   JIAHAO_GLOBE_SCALE=0.6 jiahao
                #   * 固定下来  Globe(scale=0.6, id="globe")
                globe = Globe(id="globe")
                globe.border_title = "WORLD"
                yield globe

                # 代码雨：素材真从本项目源码抓，见 widgets/rain.py
                rain = CodeRain(id="rain")
                rain.border_title = "SRC"
                yield rain

        yield Footer()

    def on_terminal_exited(self, event: Terminal.Exited) -> None:
        """内层 shell 结束 → 整个 TUI 一起退出。

        与"关掉一个终端窗口"的直觉一致。这里用显式的消息处理，而不是
        依赖某个副作用，行为可预测也能被测试覆盖。
        """
        self.exit(return_code=event.returncode or 0)


if __name__ == "__main__":
    MyTerminal().run()
