"""Framework CLI entry point, shared by source, Docker and native distributions.

The client speaks the public API; execution stays in the server's Harness v3
runtime and its permission, persistence and scheduling contracts.
"""


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == 'cli':
        args = args[1:]
    if args and args[0] == 'serve' and any(arg in {'-h', '--help'} for arg in args[1:]):
        print('用法：staffdeck serve\n启动本地服务。使用 staffdeck setup 配置网络。')
        return 0
    if args and args[0] in {'serve', 'setup'}:
        from staffdeck_harness.distribution.bootstrap import configure_environment
        configure_environment()
        from desktop_launcher import main as launch
        return launch(args)
    from staffdeck.console import main as run
    return run(args)


def api_main(argv: list[str] | None = None) -> int:
    from staffdeck.cli import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
