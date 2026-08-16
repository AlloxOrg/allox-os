# Source layout

`src/` 是 Python 构建系统使用的源码根目录，`allox/` 是对外发布的 Python 包，
对应统一的 `allox.*` 导入命名空间。

```text
src/
├── README.md
└── allox/
    ├── cli/             # CLI 入口、命令和输出
    ├── vm/              # OpenSandbox 与 Kata VM 生命周期
    ├── workspace/       # Agent/Session workspace 与回退
    ├── runtime/         # VM 内 Runtime 服务适配
    ├── integrations/    # Agent 框架集成
    ├── config.py        # 配置解析
    ├── __init__.py      # allox 包入口
    └── __main__.py      # python -m allox 入口
```

`pyproject.toml` 将 `src/allox` 构建为 `allox` 包，并注册以下命令：

```text
allox                    -> allox.cli.main:cli
allox-workspace-daemon   -> allox.workspace.daemon:main
```

这层结构让 CLI、VM、workspace、runtime 和 integrations 共享同一个产品命名空间，
同时让 `src/` 保持为构建与测试工具统一识别的源码根目录。
