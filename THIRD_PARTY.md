本项目控制程序使用 Python 标准库，代理转发使用独立的 3proxy 程序，隧道使用 Linux 内核 WireGuard。没有修改 WireGuard 或 3proxy 的协议实现。

0.2.0 的 CI 从所附固定源码编译 3proxy，镜像中的程序不再依赖现场提取官方 deb。Python 基础镜像使用 Debian bookworm，Digest 见 Dockerfile。原生发行包使用 PyInstaller 6.16.0 打包 Python 运行时，其授权为 GPL 并附带允许分发打包应用的例外；打包所带 Python 及各运行库保留原有授权。PyInstaller 仅用于 CI，不是运行中的 Python 控制器依赖。

- 3proxy 固定为 1.0.0。上游：https://github.com/3proxy/3proxy/tree/1.0.0 。源码包 SHA256：`35b07de1046f3aaeac4a7085101b7e5c453efa3527cbdc42a84690366c7ecfa8`。
- `vendor/3proxy-1.0.0-source.tar.gz` 为上游原始源码归档，包含完整版权与许可证。许可证另附于 `vendor/3proxy-LICENSE.txt`；其文件标题仍写 0.9，保留上游原文。
- 3proxy 提供 BSD 风格授权以及其他可选授权，详见所附许可证。使用本包并不代表与 Cloudflare 或 3proxy 官方有关联。
- `ip` 来自 iproute2，`wg` 来自 wireguard-tools，Python 和 CA 证书来自操作系统的软件包；本源码包不包含这些系统组件。
- 本包不附账号注册程序、实际 WARP 私钥、代理密码或生产配置。

`prepare_3proxy.py` 使用已核对的 GitHub 1.0.0 release asset 摘要：ARM64 deb 为 `dc2f217251d1f25514b69aa8310cebd89a9070950745326b05ac7908f4fd7a41`，x86-64 deb 为 `a27525c24a7240895d5de8d5fa6b7489638a2b98aa1cee81cf1030163ab3d92b`。只提取其中的 ELF 程序，不运行包安装脚本。甲骨文实测使用 ARM64；DMIT 本轮不安装新服务。

主要实现依据：

- https://www.wireguard.com/netns/
- https://github.com/3proxy/3proxy/blob/1.0.0/man/3proxy.cfg.5
- https://github.com/3proxy/3proxy/blob/1.0.0/src/conf.c
- https://github.com/3proxy/3proxy/blob/1.0.0/src/3proxy.c
- https://www.rfc-editor.org/rfc/rfc1928.txt
- https://www.rfc-editor.org/rfc/rfc1929.txt
