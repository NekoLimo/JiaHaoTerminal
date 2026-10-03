"""单文件入口：同一个 exe 既当 TUI，也当内层 shell。

## 为什么要合

原来是两个 exe（``jiahao.exe`` + ``jiahaoshell.exe``），分发时必须保证
两个文件待在一起，少一个内嵌终端就起不来。

合成一个之后，TUI 靠**重新执行自己**（带上 ``--shell-mode``）来拉起内层
shell —— PyInstaller 单文件模式下 ``sys.executable`` 就是 exe 自己的路径，
所以 ``[sys.executable, "--shell-mode"]`` 天然可用，不需要额外的东西。

## 命令行

::

    jiahao.exe                 TUI（默认）
    jiahao.exe --no-con        关动画
    jiahao.exe --speed 2       慢镜头
    jiahao.exe --shell-mode    只跑内层 shell（TUI 内部用它）
"""
import sys

#: 切到"内层 shell"模式。这个开关不进用户文档 —— 它是内部约定。
SHELL_FLAG = "--shell-mode"


def _show_licenses() -> int:
    """把捆在包里的许可/免责声明打出来。

    MIT 和 PSF 都要求再分发时保留声明。单文件 exe 里虽然塞了一份
    （解包在 _MEIPASS），但用户不一定找得到 —— 给条命令最省事。
    """
    import os

    bases = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.abspath(__file__)),
             os.path.dirname(os.path.dirname(os.path.dirname(
                 os.path.abspath(__file__))))]
    wanted = ("LICENSE", "THIRD-PARTY-NOTICES.txt", "DISCLAIMER.md")

    shown = 0
    for base in bases:
        if not base:
            continue
        for name in wanted:
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    print(f"===== {name} =====")
                    print(fh.read())
            except OSError:
                continue
            shown += 1
        if shown:
            break

    if not shown:
        print("没有找到许可文件。见项目仓库的 LICENSE / "
              "THIRD-PARTY-NOTICES.txt / DISCLAIMER.md。")
    return 0


def main(argv=None) -> int | None:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--licenses" in args or "--license" in args:
        return _show_licenses()

    if SHELL_FLAG in args:
        # 先把开关摘掉，剩下的参数原样交给 shell
        # （--no-con / --speed 那些它自己会解析）
        args = [a for a in args if a != SHELL_FLAG]
        sys.argv = [sys.argv[0], *args]

        from jiahao.jiahaoshell import main as shell_main

        return shell_main(args)

    from jiahao.main import main as tui_main

    return tui_main(args)


if __name__ == "__main__":
    # 崩溃兜底放最外层：打包成 exe 双击时出错会一闪而过，
    # 得把回溯留下来（见 jiahaoshell._crash_guard）。
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BaseException:
        from jiahao.jiahaoshell import _crash_guard

        _crash_guard("jiahao", "jiahao_crash.log")
        raise
