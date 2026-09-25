# Multi-stage build to include mc CLI in RustFS image
# This ensures zero breaking changes for existing provisioning scripts:
# - platform/03.minio/shared_tasks.py (create_app_bucket, mc admin, mc mb)
# - truealpha/10.app/provision_bucket.sh (docker exec platform-minio mc ...)
FROM rustfs/rustfs:1.0.0

ARG TARGETARCH=amd64

USER root
RUN ARCH="${TARGETARCH:-amd64}" && \
    case "$ARCH" in \
        x86_64|amd64) MC_ARCH="amd64" ;; \
        aarch64|arm64) MC_ARCH="arm64" ;; \
        *) MC_ARCH="amd64" ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/mc "https://github.com/minio/mc/releases/download/RELEASE.2025-07-16T15-35-03Z/mc.linux-${MC_ARCH}.RELEASE.2025-07-16T15-35-03Z" && \
    chmod +x /usr/local/bin/mc

# Default to rustfs non-root UID 10001
USER 10001:10001
