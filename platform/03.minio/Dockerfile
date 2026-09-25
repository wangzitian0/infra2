# Download mc CLI binary into RustFS image at build time.
# This ensures zero breaking changes for existing provisioning scripts:
# - platform/03.minio/shared_tasks.py (create_app_bucket, mc admin, mc mb)
# - truealpha/10.app/provision_bucket.sh (docker exec platform-minio mc ...)
FROM rustfs/rustfs:1.0.0

ARG TARGETARCH=amd64

USER root
# Download the statically linked mc binary from the last GitHub release and
# verify its SHA256 checksum (supply-chain hardening).  The binary is pinned
# to RELEASE.2025-07-16T15-35-03Z — the final open-source release.
# Checksums sourced from the official .sha256sum assets on the same release.
RUN set -eux && \
    case "${TARGETARCH}" in \
        amd64)  MC_ARCH="amd64"; MC_SHA256="a5c3695b6f76ed958c0f099795f32091fced0d748ddc749c8b24e07681bbfbb9" ;; \
        arm64)  MC_ARCH="arm64"; MC_SHA256="4d5f12adc96af56299893306f832cb49fd103cdd844a6a91a8e5de0592c74f5a" ;; \
        *)      echo "ERROR: unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/mc \
        "https://github.com/minio/mc/releases/download/RELEASE.2025-07-16T15-35-03Z/mc.linux-${MC_ARCH}.RELEASE.2025-07-16T15-35-03Z" && \
    echo "${MC_SHA256}  /usr/local/bin/mc" | sha256sum -c - && \
    chmod +x /usr/local/bin/mc

# Default to rustfs non-root UID 10001
USER 10001:10001
