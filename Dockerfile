# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
FROM ${PYTHON_IMAGE} AS compiler
RUN apt-get update && apt-get install -y --no-install-recommends build-essential binutils \
    && rm -rf /var/lib/apt/lists/*
COPY vendor/3proxy-1.0.0-source.tar.gz /source/3proxy-source.tar.gz
COPY tools/build_3proxy.py /source/build_3proxy.py
RUN python /source/build_3proxy.py --archive /source/3proxy-source.tar.gz --output /build --jobs 2 \
    || (tail -n 100 /build/build.log && exit 1)
RUN strip /build/3proxy-1.0.0/bin/3proxy

FROM compiler AS binary-builder
ARG TARGETARCH
ARG VERSION=0.2.0
RUN pip install --no-cache-dir pyinstaller==6.16.0
WORKDIR /source/pool
COPY . .
RUN python -m unittest discover -s tests -q
RUN python tools/build_binary_bundle.py --version "${VERSION}" --arch "${TARGETARCH}" \
    --proxy-binary /build/3proxy-1.0.0/bin/3proxy --output /release

FROM scratch AS binaries
COPY --from=binary-builder /release/ /

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.2.0
LABEL org.opencontainers.image.source="https://github.com/vcshixin/warp-pool-next" \
      org.opencontainers.image.description="Kernel WireGuard IPv6 pool with one SOCKS5 port and fixed username routing" \
      org.opencontainers.image.version="${VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 wireguard-tools ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 41001 warpnext \
    && useradd --uid 41001 --gid 41001 --no-create-home --shell /usr/sbin/nologin warpnext
WORKDIR /opt/warp-pool-next
COPY --from=compiler /build/3proxy-1.0.0/bin/3proxy ./bin/3proxy
COPY warp_pool/ ./warp_pool/
COPY tools/import_profiles.py ./tools/import_profiles.py
COPY README.md THIRD_PARTY.md ./
COPY vendor/3proxy-LICENSE.txt ./vendor/3proxy-LICENSE.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python3", "-m", "warp_pool.healthcheck"]
ENTRYPOINT ["python3", "-m", "warp_pool"]
CMD ["--config", "/etc/warp-pool-next/config.json", "run"]
