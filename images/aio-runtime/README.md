# AIO Runtime image

本目录包含 Allox OS Kata VM 使用的 Runtime OCI 镜像定义和启动配置。

```bash
cd images/aio-runtime
./build.sh
```

主要文件：

- `Dockerfile`：镜像定义。
- `build.sh`：构建入口。
- `allox_health_server.py`：Runtime 健康检查服务。
- `nginx.allox_health.conf`：健康检查反向代理配置。
- `supervisord.allox_health.conf`：健康检查进程管理配置。

完整说明见 [Runtime 镜像指南](../../docs/guides/runtime-image.md)。

