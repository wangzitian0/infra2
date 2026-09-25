# Multi-stage build to include mc CLI in RustFS image
# This ensures zero breaking changes for existing provisioning scripts:
# - platform/03.minio/shared_tasks.py (create_app_bucket, mc admin, mc mb)
# - truealpha/10.app/provision_bucket.sh (docker exec platform-minio mc ...)
FROM minio/mc:RELEASE.2025-04-16T18-13-29Z AS mc-source

FROM rustfs/rustfs:1.0.0

USER root
COPY --from=mc-source /usr/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# Default to rustfs non-root UID 10001
USER 10001:10001
