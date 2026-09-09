"""供 CI 生成自带 Python 运行时的 Linux 可执行程序。"""

from warp_pool.__main__ import main

raise SystemExit(main())
