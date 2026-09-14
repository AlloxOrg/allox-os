# Source layout

`src/` 是 `allox-os` 的 Python 源码根目录。Allox CLI 位于独立仓库，不在本目录
构建，也不是该发行包的依赖。

```text
src/
├── README.md
└── allox/
    ├── workspace/       # Agent/Session workspace 与回退
    ├── runtime/         # Guest 执行边界、cgroup 与插件 ABI
    ├── __init__.py      # allox 包入口
    └── __main__.py      # workspace daemon 入口
```

`pyproject.toml` 将 `src/allox` 构建为 `allox` 包，并注册以下命令：

```text
allox-workspace-daemon   -> allox.workspace.daemon:main
allox-guest-bootstrap    -> allox.runtime.bootstrap:main
```

进程树、Session 文件共享和 Session 网络隔离分别由 `plugins/` 下的独立发行包提供。
