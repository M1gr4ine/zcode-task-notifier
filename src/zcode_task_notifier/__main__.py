"""允许通过 ``python -m zcode_task_notifier`` 运行命令行。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
