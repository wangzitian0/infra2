ARG OP_CONNECT_VERSION=1.8.2

FROM golang:1.24-alpine AS healthcheck-builder
WORKDIR /src
COPY healthcheck.go .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /op-connect-healthcheck ./healthcheck.go

FROM 1password/connect-api:${OP_CONNECT_VERSION} AS op-connect-api
COPY --from=healthcheck-builder /op-connect-healthcheck /usr/local/bin/op-connect-healthcheck

FROM 1password/connect-sync:${OP_CONNECT_VERSION} AS op-connect-sync
COPY --from=healthcheck-builder /op-connect-healthcheck /usr/local/bin/op-connect-healthcheck
