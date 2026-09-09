# 容器部署与迁移运维

推荐形态是一个 host 网络容器、`mode: users`、一个 SOCKS5 TCP 端口。固定用户名选择固定线路，`random_users` 中的聚合用户名在同一端口按连接随机选线。部署目录中的配置、密码、WireGuard 私钥和状态文件都应留在自己的私有存储中。

## 宿主 IPv6 前提

WireGuard 的网站出口需要 IPv6，即使隧道底层用 IPv4 UDP 连接 Cloudflare。创建接口前检查：

```sh
sysctl net.ipv6.conf.default.disable_ipv6
```

结果必须为 `0`。如果系统曾设置为 `1`，新 WireGuard 接口会继承禁用状态，添加 IPv6 地址时失败。确认没有其他业务依赖“新接口默认禁用 IPv6”后，为该设置建立持久化覆盖。下面的配置文件名应在原有禁用文件之后加载；先检查同名文件并保留原内容，避免覆盖自己的其他设置。

```sh
sudo tee /etc/sysctl.d/zz-warp-pool-next.conf >/dev/null <<'EOF'
net.ipv6.conf.default.disable_ipv6 = 0
EOF
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
```

这改变后续新接口的默认值，不会主动开启已有物理网卡的 IPv6，也不添加宿主默认路由。host 模式使用宿主网络，不能靠 Compose 的独立容器 `sysctls` 代替这个前提。

如果原配置仍含 `net.ipv6.conf.all.disable_ipv6=1`，在服务运行中全量执行 `sysctl --system` 会禁用已经创建的 WireGuard 接口。需要重新加载这些系统设置时，先正常停止代理池，加载完成并确认 `default.disable_ipv6=0` 后再启动。系统启动时也应让 sysctl 设置先于 Docker 容器生效。不要为了代理池把所有既有网卡的 IPv6 设置一起改掉。

## 迁移步骤和凭证保护

1. 保存旧配置、实际出口、每份业务凭证与线路的对应关系，以及当前容器的网络参数。编号可能缺失或曾经调整，不能只凭容器名重建账号绑定。
2. 暂停会重建旧池的 watchdog、会修改凭证的管理器和相关自动分配任务；保留它们原来的启用状态。
3. 在旧实例完全停止后，才用原私钥启动新实例。同一 WARP 私钥同时建立两个活跃隧道，可能导致 Cloudflare 会话相互干扰。
4. 用单端口用户名更新每份业务凭证，保留账号身份、禁用标记、其他业务字段，以及文件的 uid、gid 和权限。原子替换文件时，临时文件也需要继承这些属性。不要用较早的备份覆盖业务正常刷新的令牌。
5. 逐条验证实际公网 IPv6、固定绑定、聚合入口及业务应用内的代理请求。检查 SOCKS5 拒绝 IPv4 字面地址、IPv4 映射地址和只有 A 记录的域名。
6. 恢复原先启用的管理器和业务巡检，检查它们没有把地址写回旧入口。验收后再移除指定旧容器、旧网络和旧启动入口，保留必要的私有备份。

某些业务配置中，全局代理、单个服务商代理和单凭证代理是不同层级。迁移已有的那一层即可；额外添加全局代理会影响原本直连的其他账号。

如需尽量保留旧公网 IPv6，应同时记录原 WireGuard 密钥、实际 endpoint、宿主外层源地址和 NAT 后的 UDP 源端口。配置文件里的监听端口未必等于 NAT 对外端口。停止旧隧道后，残留的旧 conntrack 记录可能影响端口复用；只处理核对过的旧隧道连接，不要清空全机连接表。这需要针对旧架构的迁移处理，通用配置导入工具不会自动继承 Docker NAT 状态。

复用这些参数提高了保留原出口的可能性，最终仍须通过新线路实际访问 HTTPS trace 后比较。WARP 不提供永久静态公网 IPv6 的保证。

## 运行验收

在部署目录检查：

```sh
docker compose ps
docker inspect --format '{{.HostConfig.NetworkMode}} {{json .HostConfig.PortBindings}}' warp-pool-next
docker inspect --format '{{.State.Health.Status}}' warp-pool-next
docker compose logs --tail 30 warp-pool-next
```

网络应为 `host`，端口映射为空。检查 `state/main/status.json` 的当前时间、`running`、`available_lines`、`unique_exits`、`duplicate_lines` 和 `ready`。状态文件只是一份快照，应同时确认进程存活并测试真实代理连接。`healthy` 表示达到配置的独立出口门槛，不代表所有上游网站都正常。

只更换密码或其他允许热更新的配置后，可执行：

```sh
docker kill --signal HUP warp-pool-next
docker compose logs --tail 20 warp-pool-next
```

确认没有 `configuration_reload_rejected`，再用新凭证测试。用户名、模式、私钥、监听端口等拓扑信息变化需要正常停止和重新启动。更新镜像使用已核对的版本或摘要，保留配置和整个状态目录；不要在 VPS 现场编译。

## 出口变化与隔离

默认 `follow_line` 固定用户名与 WARP 线路的关系。同一线路出现新 IPv6 后，探测通过且不与其他已启用线路重复，才继续放行。没有自动换账号、换密钥或跨线路兜底。

重复公网 IPv6 的线路会被隔离，并在状态中显示 `duplicate_exit`。即使隧道本身握手正常，也不能据此放行重复出口。`min_unique_exits` 只控制整池就绪门槛，不改变单条线路的校验；降低它不会修复重复出口。不要通过改写 pins 或随意轮换生产密钥掩盖问题。

普通网卡、路由修复会保留 endpoint 与 UDP 端口。删除设备、外部 NAT 变化或 Cloudflare 会话变化仍可能影响出口。需要针对某一条线路处理时，先确认其绑定和异常原因，按维护流程验证同一身份的新会话，并接受会中断该线路连接的影响。

## 资源统计口径

只看 `docker stats` 会遗漏宿主上的 `containerd-shim`、`docker-proxy` 和共享的 `dockerd`、`containerd`。建议覆盖至少一个完整 `health_interval`，分别记录：

- 代理池容器自身的 cgroup CPU、内存与线程数；
- 属于旧、新池的 shim 和端口映射进程；
- 全机共享的 Docker 守护进程；
- WireGuard 内核工作线程及整机其他业务。

共享 Docker 守护进程的全部成本不能算给 WARP。PSS 会分摊共享内存页，适合比较大量相似进程；不要把 PSS、RSS 和 cgroup 内存相加。Swap 也要单独记录，避免把被换出的旧进程误认为几乎不占资源。CPU 以一个核心为 100% 时，多核机器的整机百分比需要另列。

单容器消除了每条线路的容器管理和端口映射成本，但每条 WireGuard 隧道的加密、保活和内核工作仍存在，真实并发也会增加线程与 CPU。测量前后流量不同，或旧 watchdog 暂停时，应注明这些条件，不能把短时生产采样视作等负载性能基准。
