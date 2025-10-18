## MariaDB Service

Declarative service contract for MariaDB 10.11 aligned with the shared infrastructure framework. The defaults describe host, container, and runtime-agnostic settings that the common roles render into Proxmox LXC, Docker, Podman, Kubernetes, and bare-metal systemd manifests.

### Runtime Coverage
- Proxmox LXC via `common.render_runtime` + `common.apply_runtime`
- Docker Compose v2
- Podman Quadlet (system scope by default)
- Kubernetes Deployment + Service + Secret + PVC
- Bare-metal systemd unit

### Exports
- Global environment exports retain backwards compatibility for single-tenant consumers:
  ```
  DATABASE_HOST={{ service_ip }}
  DATABASE_PORT={{ mariadb_service_port }}
  DATABASE_NAME={{ mariadb_database }}
  DATABASE_USER={{ mariadb_user }}
  ```
- Passwords are intentionally not exported. Dependents must declare a `MYSQL_PASSWORD` secret
  requirement in the dependency registry and fetch it through the shared secret adapter used by
  their runtime.
- When `mariadb_schemas` is populated, the role renders one export file per schema at
  `{{ mariadb_exports_directory }}/<schema>.env` containing host/port/name/user pairs. Consumers
  can source the file that matches their schema without the provider knowing about individual
  applications.

### Multi-tenant Schemas
Define additional databases and users with the `mariadb_schemas` list. Each item must provide a
schema name, user, and password (directly or indirectly via another variable):

```yaml
mariadb_schemas:
  - name: erpnext
    user: erpnext
    password_var: vault_erp_db_password
  - name: wordpress
    user: wp
    password: "{{ vault_wordpress_db_password }}"
```

The role waits for MariaDB to become reachable, creates the requested schemas and scoped users,
and drops export files for each entry. Existing `mariadb_database` / `mariadb_user` variables
remain available for simple single-tenant deployments.

### Secrets
- `MYSQL_ROOT_PASSWORD` -> root account password
- `MYSQL_PASSWORD` -> application user password
- `RESTIC_PASSWORD` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` -> credentials for the automated Restic backup jobs

All secrets must be provided through inventory, Vault, or an external secret manager. The role asserts that the placeholder defaults are never used at runtime.

### Security & Networking
- Host publishing of TCP/3306 is disabled by default (`mariadb_publish_port: false`). If a host mapping is really required, explicitly opt-in and review firewall policy.
- Container runtimes attach the database to a dedicated internal bridge network (`mariadb_internal_network`) that is not exposed publicly.
- `service_firewall` seeds iptables and Proxmox firewall rules that only permit RFC1918 source CIDRs by default (`mariadb_allowed_cidrs`); tailor to your private address space.
- Kubernetes runtimes render a `ClusterIP` Service alongside a restrictive `NetworkPolicy` that limits ingress to in-namespace workloads unless overridden.
- Post-deploy tasks scope the `root` account to loopback and automation hosts, remove remote wildcards, and drop the default `test` schema to eliminate insecure defaults.

### Backups & PITR
- `mariadb_backups.logical` enables nightly `mysqldump` exports that are piped into Restic for deduplicated storage in object stores (S3, MinIO, etc.). Adjust the cron `schedule` and retention knobs as needed.
- Populate `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` secrets to authenticate to the Restic repository specified via `mariadb_backup_restic_repository`.
- Optional `mariadb_backups.physical` toggles Percona `innobackupex` workflows for larger datasets that need hot physical copies.
- `mariadb_backups.binlog_shipping` streams MariaDB binary logs to S3 for point-in-time recovery; customize the S3 path and retention per compliance needs.

### Health Check
`mysqladmin ping -h 127.0.0.1 -P {{ mariadb_service_port }}` with a 10s interval, 5s timeout, start period of 30s, failure threshold of 3, success threshold of 1, and 5 retries. The same command feeds Docker healthchecks, Quadlet probes, Kubernetes readiness/liveness, and the post-deploy gate.

### Key Overrides
| Variable | Default | Purpose |
| --- | --- | --- |
| `mariadb_service_port` | `3306` | Internal TCP port |
| `mariadb_publish_port` | `false` | Opt-in host publishing of TCP/3306 |
| `mariadb_database` | `appdb` | Default schema created for workloads |
| `mariadb_user` | `app` | Application database user |
| `mariadb_schemas` | `[]` | Optional list of additional schema/user definitions |
| `mariadb_admin_host` | `{{ service_ip }}` | Hostname/IP used for administrative connections |
| `mariadb_exports_directory` | `/srv/db/exports` | Directory for per-schema export files |
| `mariadb_data_volume` | `mariadb-data` | Named volume for container targets |
| `mariadb_container_ip` | `192.168.100.10` | LXC container address |
| `mariadb_container_vmid` | `200` | Proxmox VMID |
| `mariadb_container_storage_gb` | `50` | Storage allocation for both PVC and LXC disk |
| `mariadb_container_cpu_cores` | `2` | CPU allocation across runtimes |
| `mariadb_container_memory_mb` | `2048` | Memory allocation across runtimes |
| `mariadb_allowed_cidrs` | RFC1918 ranges | Sources allowed through firewall policy |
| `mariadb_innodb_buffer_pool_size` | `256M` | Buffer pool size exposed via `MARIADB_EXTRA_FLAGS` |
| `mariadb_max_connections` | `151` | Connection limit exposed via `MARIADB_EXTRA_FLAGS` |
| `mariadb_root_allowed_hosts` | `localhost`, `127.0.0.1`, `::1`, admin IPs | Root account scope for automation |
| `mariadb_root_disallowed_hosts` | `%` | Remote root hosts removed after bootstrap |
| `mariadb_remove_test_database` | `true` | Drop default `test` schema post-deploy |
| `mariadb_backups.logical.schedule` | `0 2 * * *` | Nightly logical dump cadence |
| `mariadb_backups.binlog_shipping.retention_hours` | `168` | Retention for shipped binary logs |
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
        mariadb_schemas:
          - name: erpnext
            user: erpnext
            password_var: vault_erpnext_db_password
          - name: wordpress
            user: wp
            password_var: vault_wordpress_db_password
```
