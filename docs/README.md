## MariaDB Service

Declarative service contract for MariaDB 10.11 aligned with the shared infrastructure framework. The defaults describe host, container, and runtime-agnostic settings that the common roles render into Proxmox LXC, Docker, Podman, Kubernetes, and bare-metal systemd manifests.

### Runtime Coverage
- Proxmox LXC via `common.render_runtime` + `common.apply_runtime`
- Docker Compose v2
- Podman Quadlet (system scope by default)
- Kubernetes Deployment + Service + Secret + PVC
- Bare-metal systemd unit

### Exports
```
DATABASE_HOST={{ service_ip }}
DATABASE_PORT={{ mariadb_service_port }}
DATABASE_NAME={{ mariadb_database }}
DATABASE_USER={{ mariadb_user }}
```

### Secrets
- `MYSQL_ROOT_PASSWORD` -> root account password
- `MYSQL_PASSWORD` -> application user password

Both default to environment lookups (`MARIADB_ROOT_PASSWORD` / `MARIADB_USER_PASSWORD`) with placeholder fallbacks; override them through inventory or Ansible vault.

### Health Check
`mysqladmin ping -h 127.0.0.1 -P {{ mariadb_service_port }}` with a 10s interval, 5s timeout, and 5 retries. The same command feeds Docker healthchecks, Quadlet probes, Kubernetes readiness/liveness, and the post-deploy gate.

### Key Overrides
| Variable | Default | Purpose |
| --- | --- | --- |
| `mariadb_service_port` | `3306` | Published TCP port |
| `mariadb_database` | `appdb` | Default schema created for workloads |
| `mariadb_user` | `app` | Application database user |
| `mariadb_data_volume` | `mariadb-data` | Named volume for container targets |
| `mariadb_container_ip` | `192.168.100.10` | LXC container address |
| `mariadb_container_vmid` | `200` | Proxmox VMID |
| `mariadb_container_storage_gb` | `50` | Storage allocation for both PVC and LXC disk |
| `mariadb_container_cpu_cores` | `2` | CPU allocation across runtimes |
| `mariadb_container_memory_mb` | `2048` | Memory allocation across runtimes |
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
```
