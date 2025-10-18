## MariaDB Service

Declarative service contract for MariaDB 10.11 aligned with the shared infrastructure framework. The defaults describe host, container, and runtime-agnostic settings that the common roles render into Proxmox LXC, Docker, Podman, Kubernetes, and bare-metal systemd manifests.

### Runtime Coverage
- Proxmox LXC via `common.render_runtime` + `common.apply_runtime`
- Docker Compose v2
- Podman Quadlet (system scope by default)
- Kubernetes Deployment + Service + Secret + PVC
- Bare-metal systemd unit

### Exports
- Global environment exports retain backwards compatibility for single-tenant consumers and now
  include the server version. Set `mariadb_exports_include_credentials: true` in trusted
  environments to append `DATABASE_PASSWORD` to the rendered secret bundle.
- Per-schema exports are now generated in-memory so container images remain immutable. The default
  `mariadb_exports_delivery: stdout` streams deterministic key/content pairs (one per schema user)
  to the play output for operators that capture stdout.
- Set `mariadb_exports_delivery: configmap` to fold the per-user exports into
  `exports.configmaps` using `mariadb_exports_configmap_name`, or
  `mariadb_exports_delivery: secret` to publish them under `exports.secrets` via
  `mariadb_exports_secret_name`.
- Export keys include a SHA-256 hash suffix to avoid collisions between sanitized schema/user
  names and eliminate races with concurrent deployments—no on-host pruning is required.
- When `mariadb_exports_include_credentials` is enabled, prefer the `secret` delivery mode or
  distribute passwords through Vault / sealed secrets to avoid storing credentials in plaintext
  ConfigMaps.

### Multi-tenant Schemas
Define additional databases with the `mariadb_schemas` list. Each item provides a schema name,
desired state, and one or more users with independent privilege levels:

```yaml
mariadb_schemas:
  - name: erpnext
    state: present
    users:
      - name: erpnext_writer
        password_var: vault_erp_db_password
      - name: erpnext_reader
        password_var: vault_erp_ro_password
        privilege_level: read_only
  - name: legacy_app
    state: absent
    users:
      - name: legacy_app
        password_var: vault_legacy_app_password
```

Reserved system schemas such as `mysql`, `information_schema`, `performance_schema`, and `sys` are
rejected. Privilege levels default to full DDL/DML access, can be set to `read_only`, or extended by
supplying an explicit list of privileges. Validation now happens via a custom Jinja filter so
mistakes surface during `ansible-playbook --check`. Setting `state: absent` drops both the schema and
declared users immediately—take ad-hoc dumps before toggling the state if data must be retained. The
role waits for MariaDB to become reachable, creates the requested schemas and scoped users, and
streams export entries for each user. Existing `mariadb_database` / `mariadb_user` variables remain
available for simple single-tenant deployments.

### Secrets
- `MYSQL_ROOT_PASSWORD` -> root account password
- `MYSQL_PASSWORD` -> application user password
- `RESTIC_PASSWORD` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` -> credentials for the automated Restic backup jobs

All secrets must be provided through inventory, Vault, or an external secret manager. The role asserts that the placeholder defaults are never used at runtime.
Root and application credentials must be at least 16 characters long and include mixed case, digits,
and special characters.

### Security & Networking
- Host publishing of TCP/3306 remains disabled by default (`mariadb_publish_port: false`). If a host
  mapping is required, explicitly opt-in and define a narrow `mariadb_allowed_cidrs` list; leaving the
  variable empty now triggers a hard failure to force deliberate firewall configuration.
- Container runtimes attach the database to a dedicated internal bridge network
  (`mariadb_internal_network`) that is not exposed publicly. Kubernetes runtimes render a
  `ClusterIP` Service alongside a restrictive `NetworkPolicy` that limits ingress to in-namespace
  workloads unless overridden.
- Post-deploy tasks enforce a strict whitelist for the `root` account. Any `root@host` entries not in
  `mariadb_root_allowed_hosts` are removed automatically. For one-time remote maintenance, forward the
  socket over SSH instead of relaxing host grants:
  1. `ssh -i ~/.ssh/admin_id -J bastion@example.com -L 13306:127.0.0.1:{{ mariadb_service_port }} mariadb-admin@database-host`
  2. Connect with your local client against `127.0.0.1:13306` using the root credentials.
  3. Close the tunnel when finished; no server-side configuration changes are required.
- Default server configuration enforces `{{ mariadb_character_set }}` / `{{ mariadb_collation }}` and
  applies rate limiting (`max_connect_errors`, `max_password_errors`) to reduce brute-force windows.
  Performance Schema and the slow query log are enabled to feed query monitoring. Post-deploy
  hygiene continues to drop any `test%` schemas detected during initialization.
- The bundled health check script (see below) optionally notifies an external webhook when failures
  occur. Provide `mariadb_health_webhook_url` to receive JSON alerts from the health probe.

The wait task leverages `community.mysql.mysql_ping`, which requires `PyMySQL` to be installed on the
control node. Install `python3-pymysql` (or the equivalent package for your platform) before running
the role, or swap the module for a shell-based `mysqladmin ping` check if that dependency is
undesirable.

### Backups & PITR
- `mariadb_backups.logical` enables nightly `mysqldump` exports that are piped into Restic for
  deduplicated storage in object stores (S3, MinIO, etc.). System schemas are excluded by default via
  `extra_args` to keep dump sizes lean.
- Run the optional repository verification with `ansible-playbook ... --tags verify-backups`; the
  Restic preflight is no longer gated by a boolean variable. Retention knobs must satisfy
  `keep_monthly >= keep_weekly >= keep_daily`. Changing this hierarchy later requires manual
  pruning of existing snapshots to avoid lingering data in higher retention tiers.
- Populate `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` secrets to
  authenticate to the Restic repository specified via `mariadb_backup_restic_repository`.
- `mariadb_backups.binlog_shipping` now uploads a lightweight preflight object to the configured S3
  bucket to confirm write access before streaming binary logs. The helper respects
  `mariadb_backup_aws_region` when provided and deletes the probe object afterwards. Checksums remain
  enabled via `checksum: md5` by default.

### Health Check
Runtimes execute `{{ mariadb_health_check_script }}`—sourced from `files/mariadb-healthcheck.sh`—every
10 seconds with a 5 second timeout. The script performs several layered validations:

1. `mysqladmin ping` verifies that the server is responsive on `127.0.0.1:{{ mariadb_service_port }}`.
2. `SHOW GLOBAL STATUS LIKE 'Threads_connected'` fails the probe when connection usage exceeds the
   configured `max_connections` or crosses the derived
   `mariadb_health_connection_threshold_count` (90% by default), passed in via
   `MARIADB_HEALTH_CONNECTION_THRESHOLD`.
3. `df` monitors `/var/lib/mysql` and trips when disk utilization breaches
   `mariadb_health_disk_usage_threshold` (90% by default).
4. When `mariadb_replication_enabled: true`, the probe inspects `SHOW REPLICA STATUS` /
   `SHOW SLAVE STATUS` to ensure the IO and SQL threads are both running.
5. Failures optionally trigger a JSON webhook POST to `mariadb_health_webhook_url`. The helper retries
   delivery a few times but ultimately treats notifications as best-effort so an unreachable webhook
   does not mask database issues.

The script prints `ok` on success and exits non-zero on any failure, feeding Docker, Podman, and
Kubernetes liveness/readiness gates.

### Connection Pooling
The service contract intentionally keeps MariaDB close to upstream defaults and does not ship an
embedded connection pooler. For applications that generate spiky workloads or hold many idle
connections, deploy ProxySQL, MaxScale, or PgBouncer-in-MySQL-mode in front of the service. Expose
the pooler address to consumers instead of the raw database endpoint and tune pool sizing to match
application concurrency. The README in the pooler role should document how to register it as an
additional dependency alongside this service.

### Zero-downtime Upgrades
MariaDB container upgrades inherently restart the server. For production deployments that cannot
tolerate downtime, perform blue/green rollouts or temporarily introduce a replica:

1. Stand up a new instance with the desired version and configure asynchronous replication from the
   primary.
2. Allow replicas to catch up, then redirect application traffic (or promote the replica) via DNS or
   pooler configuration.
3. Decommission the old node once replication lag is zero and clients have drained.

Document the chosen procedure in runbooks so operators do not rely on disruptive in-place restarts.

### Key Overrides
| Variable | Default | Purpose |
| --- | --- | --- |
| `mariadb_service_port` | `3306` | Internal TCP port |
| `mariadb_publish_port` | `false` | Opt-in host publishing of TCP/3306 |
| `mariadb_database` | `appdb` | Default schema created for workloads |
| `mariadb_user` | `app` | Application database user |
| `mariadb_schemas` | `[]` | Declarative schema list (supports per-user privileges and `state`) |
| `mariadb_admin_host` | `{{ service_ip }}` | Hostname/IP used for administrative connections |
| `mariadb_exports_delivery` | `stdout` | Delivery target for per-user exports (`stdout` / `configmap` / `secret` / `none`) |
| `mariadb_exports_configmap_name` | `{{ service_id }}-schema-exports` | ConfigMap name when `mariadb_exports_delivery: configmap` |
| `mariadb_exports_secret_name` | `{{ service_id }}-schema-exports` | Secret name when `mariadb_exports_delivery: secret` |
| `mariadb_exports_include_credentials` | `false` | Include `DATABASE_PASSWORD` in exports when true (prefer `secret` delivery or Vault) |
| `mariadb_data_volume` | `mariadb-data` | Named volume for container targets |
| `mariadb_allowed_cidrs` | *(required)* | Firewall source CIDRs; must be set per deployment |
| `mariadb_character_set` / `mariadb_collation` | `utf8mb4` / `utf8mb4_unicode_ci` | Server character set & collation |
| `mariadb_innodb_buffer_pool_size` | `256M` | Buffer pool size exposed via `MARIADB_EXTRA_FLAGS` |
| `mariadb_max_connections` | `151` | Connection limit exposed via `MARIADB_EXTRA_FLAGS` |
| `mariadb_max_connect_errors` / `mariadb_max_password_errors` | `20` / `6` | Authentication rate limiting |
| `mariadb_tx_isolation` | `READ-COMMITTED` | Server transaction isolation level |
| `mariadb_performance_schema_enabled` | `true` | Enable performance schema for query observability |
| `mariadb_slow_query_log_enabled` | `true` | Toggle slow query log (`mariadb_slow_query_log_file`) |
| `mariadb_root_allowed_hosts` | `localhost`, `127.0.0.1`, `::1` | Loopback-only root access |
| `mariadb_remove_test_database` | `true` | Drop default `test%` schemas post-deploy |
| `mariadb_backups.logical.schedule` | `0 2 * * *` | Nightly logical dump cadence |
| `mariadb_backups.logical.verify_repository` | `true` | Legacy knob retained for compatibility; use `--tags verify-backups` to run `restic snapshots` |
| `mariadb_backups.binlog_shipping.checksum` | `md5` | Upload checksum for each shipped binlog |
| `mariadb_backup_aws_region` | *unset* | Optional AWS region for binlog shipping and Restic uploads |
| `mariadb_health_disk_usage_threshold` | `90` | Disk utilization percentage that fails health checks |
| `mariadb_health_connection_utilization_threshold` | `0.9` | Fraction of `max_connections` that trips alerts |
| `mariadb_health_webhook_url` | `""` | Optional webhook notified on health-check failure |
| `mariadb_replication_enabled` | `false` | Extends health check to validate replica status |
| `mariadb_kubernetes_namespace` | `databases` | Namespace for Deployment/Service/PVC |

Adjust these in inventory to tune runtime specifics. Any additional runtime template parameters can be supplied by extending the defaults with extra keys consumed by the shared templates.

### Usage
```yaml
- hosts: db_hosts
  roles:
    - role: svc-mariadb
      vars:
        runtime: docker       # or proxmox / podman / kubernetes / baremetal
        mariadb_database: erpnext
        mariadb_user: erpnext
        mariadb_root_password: "{{ vault_mariadb_root_password }}"
        mariadb_user_password: "{{ vault_mariadb_user_password }}"
        mariadb_backup_restic_password: "{{ vault_mariadb_restic_password }}"
        mariadb_backup_aws_access_key_id: "{{ vault_mariadb_backup_access_key }}"
        mariadb_backup_aws_secret_access_key: "{{ vault_mariadb_backup_secret_key }}"
        mariadb_binlog_s3_path: s3://prod-backups/mariadb/binlog
        mariadb_allowed_cidrs:
          - 10.10.0.0/24
        mariadb_schemas:
          - name: erpnext
            users:
              - name: erpnext_writer
                password_var: vault_erpnext_db_password
              - name: erpnext_reader
                password_var: vault_erpnext_ro_password
                privilege_level: read_only
          - name: legacy
            state: absent
            users:
              - name: legacy
                password_var: vault_legacy_password
```
