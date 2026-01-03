# ClickHouse (Storage Backend for SigNoz)

> **Category**: Databases (01-09)

Time-series database for observability data storage (logs, metrics, traces).

## Dependencies

- Vault must be available (for future auth if needed)
- ZooKeeper for coordination

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | Docker Compose (init + zookeeper + clickhouse) |
| `config.xml` | ClickHouse server config template (rendered per env) |
| `users.xml` | User and permissions configuration |
| `deploy.py` | ClickHouseDeployer |
| `shared_tasks.py` | Health check status() |

## Architecture

```
┌─────────────────┐
│ init-clickhouse │ ──setup──> user_scripts/histogramQuantile
└─────────────────┘

┌─────────────────┐
│ zookeeper       │ ──coordinate──> ClickHouse cluster
└─────────────────┘

┌─────────────────┐
│ clickhouse      │ ──store──> ${DATA_PATH}/
└─────────────────┘
```

## Deployment

```bash
# Full setup
invoke clickhouse.setup

# Or step-by-step
invoke clickhouse.pre-compose
invoke clickhouse.composing
invoke clickhouse.post-compose

# Check status
invoke clickhouse.status
```

**pre-compose** will:
1. Create data directories (`${DATA_PATH}/{data,logs,user_scripts,zookeeper}`)
2. Set permissions (uid=101, gid=101 for ClickHouse)
3. Render `${DATA_PATH}/config.xml` with env-scoped hostnames
4. Persist `config.xml` under `${DATA_PATH}` so redeploys keep the same config

## Data Path

`${DATA_PATH}` (uid=101, gid=101 for clickhouse, root for zookeeper; staging uses `/data/platform/clickhouse-staging`)
- `data/` - ClickHouse database files
- `logs/` - Server logs
- `user_scripts/` - Custom functions (histogram-quantile)
- `zookeeper/` - ZooKeeper coordination data

## Containers

- **init-clickhouse**: Downloads histogram-quantile binary (one-time)
  - Restart: on-failure
- **zookeeper**: Cluster coordination
  - Port: 2181 (internal)
  - Health: `echo ruok | nc localhost 2181`
- **clickhouse**: Main database server
  - Port: 9000 (native), 8123 (HTTP) - internal only
  - Health: `wget --spider -q localhost:8123/ping`

## Access

**Internal only** - no public domain configured.

Services connect via:
- Native protocol: `platform-clickhouse${ENV_SUFFIX}:9000`
- HTTP API: `platform-clickhouse${ENV_SUFFIX}:8123`

**Default credentials**:
- User: `default`
- Password: (empty)

## Configuration

### Server (config.xml)
- HTTP port: 8123
- TCP port: 9000
- Max connections: 4096
- Uncompressed cache: 8GB
- Mark cache: 5GB

### Cluster (config.xml)
- Cluster name: `cluster_1S_1R` (1 shard, 1 replica)
- ZooKeeper: `platform-clickhouse-zookeeper${ENV_SUFFIX}:2181`
  - Single-node ZK only; multi-node clusters must override `ZOO_SERVERS` to list all nodes.

### Users (users.xml)
- Profile: default (10GB memory limit)
- Network: allow all (`::/0`)
- Quota: unlimited

## Used By

- [SigNoz](../11.signoz/README.md) - Observability platform
