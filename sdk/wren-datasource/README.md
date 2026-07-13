# wren-datasource

Datasource management REST service for Wren. Manages **project registration** + **connection profiles** + **connection resolution**, enabling a single `wren-mcp` process to serve multiple Wren projects by resolving connections per-request.

## Why

`wren-mcp` pins one project + profile at startup (`ServerState`). To serve multiple projects from one process, the datasource management layer (profile storage + connection resolution) is extracted into this standalone REST service. `wren-mcp` calls it per-request to resolve which project + connection to use, routing by the `X-Wren-Project` header.

```
┌─────────────────┐     GET /projects/{id}/connection      ┌──────────────────┐
│   wren-mcp      │ ─────────────────────────────────────► │ wren-datasource  │
│ (1 process,     │     X-Wren-Project: sales              │ (REST + SQLite)  │
│  N projects)    │ ◄───────────────────────────────────── │                  │
└─────────────────┘     {datasource, connection_info,      └──────────────────┘
                         memory_collection_prefix}
```

## Run

```bash
pip install wren-datasource
wren-datasource --token $WREN_DATASOURCE_TOKEN --port 8766
```

Import existing `~/.wren/profiles.yml` on first start:

```bash
wren-datasource --token $TOKEN --import-profiles
```

## API

All endpoints require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness |
| `POST` | `/projects` | Register a project `{name, project_path, profile_name?}` |
| `GET` | `/projects` | List projects |
| `GET` | `/projects/{id}` | Get project |
| `DELETE` | `/projects/{id}` | Remove project |
| `GET` | `/projects/{id}/connection` | Resolve `{datasource, connection_info, memory_collection_prefix, project_path}` |
| `GET` | `/projects/{id}/manifest` | Read-through `target/mdl.json` |
| `POST` | `/projects/{id}/validate` | `SELECT 1` through the connector |
| `POST` | `/profiles` | Add a profile `{name, datasource, ...fields}` |
| `GET` | `/profiles` | List profiles (redacted) |
| `GET` | `/profiles/{name}` | Get a profile (redacted) |
| `DELETE` | `/profiles/{name}` | Remove a profile |

## Storage

SQLite at `~/.wren/datasource.db` (override with `--db-path` or `WREN_DATASOURCE_DB`). Profiles store `${ENV}` placeholders **unexpanded**; secrets are expanded only at resolution time, so the DB never persists real credentials. The DB file is `0600`, matching `profiles.yml`'s permission model.

## Security

The `/projects/{id}/connection` response carries expanded DB credentials. It must only travel to an authenticated `wren-mcp` over a trusted network (same-host unix socket or mTLS). The token gate is mandatory - never deploy without `--token` on a network.
