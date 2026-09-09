本版本提供单 host 网络容器、单 SOCKS5 端口的 WARP IPv6 代理池。

- 普通用户名固定线路；可另设随机聚合用户名，只选择当前已验证可用的线路。
- 默认固定账号和线路，验证并去重后允许接受同一线路的新公网 IPv6。
- GitHub Actions 分别在原生 amd64、arm64 runner 上编译；服务器无需现场构建。
- GHCR 镜像包含运行环境、已编译 3proxy、iproute2 和 wireguard-tools，不包含编译器。
- Linux 二进制压缩包包含自带 Python 运行时的控制器以及 3proxy；仍需要宿主内核 WireGuard、iproute2、wireguard-tools 和 CA 证书。原生包适用 glibc 2.36 或更新系统；Docker 部署自带匹配运行库。
- 提供 SHA256 校验清单。账号、私钥、代理密码和生产配置不随制品发布。

部署使用仓库内 `deploy/compose.yaml`。正式切换前保存自己的配置和状态，先确认旧隧道已停止，避免同一私钥同时用于两个活跃隧道。固定公网 IPv6 无法由 WARP 保证。
