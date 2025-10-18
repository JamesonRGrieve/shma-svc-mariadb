from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping
from urllib.parse import urlparse

from ansible.errors import AnsibleFilterError

_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(value: str) -> str:
    return _SAFE_PATTERN.sub("_", value)


def _ensure_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [segment.strip() for segment in value.split(",") if segment.strip()]
    if isinstance(value, Iterable):
        result: List[str] = []
        for item in value:
            item_str = str(item).strip()
            if item_str:
                result.append(item_str)
        return result
    raise AnsibleFilterError(
        "Schema user privilege definitions must be a string or iterable of privilege names"
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AnsibleFilterError(message)


class FilterModule:
    def filters(self) -> Dict[str, Any]:
        return {
            "mariadb_normalize_schemas": self.mariadb_normalize_schemas,
            "mariadb_resolve_schemas": self.mariadb_resolve_schemas,
            "mariadb_schema_exports": self.mariadb_schema_exports,
            "mariadb_schema_export_map": self.mariadb_schema_export_map,
            "mariadb_password_is_strong": self.mariadb_password_is_strong,
            "mariadb_parse_s3_url": self.mariadb_parse_s3_url,
            "mariadb_preflight_key": self.mariadb_preflight_key,
        }

    def mariadb_normalize_schemas(
        self,
        schemas: Any,
        default_state: str,
        reserved_names: Iterable[str] | None,
        privilege_levels: Mapping[str, Mapping[str, Any]] | None,
    ) -> List[Dict[str, Any]]:
        if schemas in (None, ""):
            return []
        if not isinstance(schemas, list):
            raise AnsibleFilterError(
                "mariadb_schemas must be a list of schema definitions"
            )

        reserved = set(reserved_names or [])
        valid_states = {"present", "absent"}
        normalized: List[Dict[str, Any]] = []

        for index, schema in enumerate(schemas):
            if not isinstance(schema, Mapping):
                raise AnsibleFilterError(
                    f"Schema entry #{index + 1} must be a mapping of schema attributes"
                )

            name_raw = str(schema.get("name", "")).strip()
            _require(
                name_raw, f"Schema entry #{index + 1} must define a non-empty name"
            )
            _require(
                name_raw not in reserved,
                f"Schema '{name_raw}' is reserved and cannot be managed by this role",
            )

            state = str(schema.get("state", default_state) or default_state)
            _require(
                state in valid_states,
                f"Schema '{name_raw}' must set state to one of {sorted(valid_states)}",
            )

            users = schema.get("users")
            _require(
                isinstance(users, list) and users,
                f"Schema '{name_raw}' must declare at least one user",
            )

            normalized_users: List[Dict[str, Any]] = []
            for user_index, user in enumerate(users):
                if not isinstance(user, Mapping):
                    raise AnsibleFilterError(
                        f"Schema '{name_raw}' user #{user_index + 1} must be a mapping"
                    )

                user_name = str(user.get("name", "")).strip()
                _require(
                    user_name,
                    f"Schema '{name_raw}' user #{user_index + 1} must define a non-empty name",
                )

                password = user.get("password")
                password_var = user.get("password_var")
                if password is not None and password_var is not None:
                    raise AnsibleFilterError(
                        f"Schema '{name_raw}' user '{user_name}' cannot define both password and password_var"
                    )
                _require(
                    (password is not None and str(password).strip())
                    or (password_var is not None and str(password_var).strip()),
                    f"Schema '{name_raw}' user '{user_name}' must define password or password_var",
                )

                privilege_level = str(user.get("privilege_level", "standard"))
                privilege_template: Mapping[str, Any] | None = None
                if privilege_levels is not None:
                    _require(
                        privilege_level in privilege_levels,
                        f"Schema '{name_raw}' user '{user_name}' must use a known privilege_level ({', '.join(privilege_levels.keys())})",
                    )
                    privilege_template = privilege_levels[privilege_level]

                grant_option_default = False
                privilege_default: Iterable[str] = []
                if privilege_template:
                    grant_option_default = bool(
                        privilege_template.get("grant_option", False)
                    )
                    privilege_default = privilege_template.get("privileges", [])

                privilege_value = user.get("privileges")
                privileges = (
                    _ensure_list(privilege_value)
                    if privilege_value is not None
                    else list(privilege_default)
                )
                privileges = [priv.strip() for priv in privileges if priv.strip()]
                _require(
                    bool(privileges),
                    f"Schema '{name_raw}' user '{user_name}' must expand to at least one privilege",
                )

                grant_option_value = user.get("grant_option")
                grant_option = (
                    bool(grant_option_value)
                    if grant_option_value is not None
                    else grant_option_default
                )

                normalized_users.append(
                    {
                        "name": user_name,
                        "password": password,
                        "password_var": password_var,
                        "privileges": privileges,
                        "grant_option": grant_option,
                    }
                )

            normalized.append(
                {
                    "name": name_raw,
                    "state": state,
                    "users": normalized_users,
                }
            )

        return normalized

    def mariadb_resolve_schemas(
        self,
        schemas: Iterable[Mapping[str, Any]],
        variables: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        if not schemas:
            return resolved
        if not isinstance(variables, Mapping):
            raise AnsibleFilterError(
                "vars must be a mapping when resolving schema credentials"
            )

        for schema in schemas:
            schema_name = schema["name"]
            schema_state = schema["state"]
            resolved_users: List[Dict[str, Any]] = []
            for user in schema.get("users", []):
                password = user.get("password")
                if password is not None:
                    password_value = str(password).strip()
                else:
                    password_var = user.get("password_var")
                    password_value = (
                        str(variables.get(password_var, "")).strip()
                        if password_var
                        else ""
                    )
                _require(
                    password_value,
                    f"Schema '{schema_name}' user '{user['name']}' password reference resolved to an empty value",
                )
                resolved_users.append(
                    {
                        "name": user["name"],
                        "password": password_value,
                        "privileges": list(user.get("privileges", [])),
                        "grant_option": bool(user.get("grant_option", False)),
                    }
                )

            resolved.append(
                {
                    "name": schema_name,
                    "state": schema_state,
                    "users": resolved_users,
                }
            )

        return resolved

    def mariadb_schema_exports(
        self,
        schemas: Iterable[Mapping[str, Any]],
        service_id: str,
        host: str,
        port: Any,
        version: str,
        include_credentials: bool = False,
    ) -> List[Dict[str, Any]]:
        exports: List[Dict[str, Any]] = []
        if not schemas:
            return exports

        port_str = str(port)
        for schema in schemas:
            if schema.get("state") != "present":
                continue
            schema_name = str(schema.get("name", ""))
            safe_schema = _safe_name(schema_name)
            for user in schema.get("users", []):
                user_name = str(user.get("name", ""))
                safe_user = _safe_name(user_name)
                digest_source = f"{schema_name}::{user_name}".encode("utf-8")
                digest = hashlib.sha256(digest_source).hexdigest()[:12]
                key = f"{service_id}-{safe_schema}-{safe_user}-{digest}.env"

                lines = [
                    f"DATABASE_HOST={host}",
                    f"DATABASE_PORT={port_str}",
                    f"DATABASE_NAME={schema_name}",
                    f"DATABASE_USER={user_name}",
                ]
                if include_credentials:
                    lines.append(f"DATABASE_PASSWORD={user.get('password')}")
                lines.append(f"DATABASE_VERSION={version}")
                exports.append(
                    {
                        "schema": schema_name,
                        "user": user_name,
                        "key": key,
                        "content": "\n".join(lines) + "\n",
                    }
                )

        return exports

    def mariadb_schema_export_map(
        self, exports: Iterable[Mapping[str, Any]]
    ) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        if not exports:
            return mapping
        for item in exports:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key")
            if key is None:
                continue
            content = item.get("content", "")
            mapping[str(key)] = "" if content is None else str(content)
        return mapping

    def mariadb_password_is_strong(self, password: Any, min_length: int = 16) -> bool:
        if password is None:
            return False
        password_str = str(password)
        if len(password_str) < int(min_length):
            return False

        has_lower = any(char.islower() for char in password_str)
        has_upper = any(char.isupper() for char in password_str)
        has_digit = any(char.isdigit() for char in password_str)
        has_special = any(not char.isalnum() for char in password_str)
        return has_lower and has_upper and has_digit and has_special

    def mariadb_parse_s3_url(self, url: str) -> Dict[str, str]:
        if not url:
            raise AnsibleFilterError("mariadb_binlog_s3_path must be defined")
        parsed = urlparse(url)
        _require(
            parsed.scheme == "s3", "mariadb_binlog_s3_path must use the s3:// scheme"
        )
        bucket = parsed.netloc
        _require(bucket, "mariadb_binlog_s3_path must include a bucket name")
        prefix = parsed.path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"
        return {"bucket": bucket, "prefix": prefix}

    def mariadb_preflight_key(self, prefix: str | None) -> str:
        safe_prefix = prefix or ""
        if safe_prefix and not safe_prefix.endswith("/"):
            safe_prefix = f"{safe_prefix}/"
        return f"{safe_prefix}.preflight-{uuid.uuid4().hex}"
