# Deployment

This document covers the three supported deployment paths: Render
(managed), Docker, and on-premise behind nginx.

## 1. Render (managed)

The repository ships with `render.yaml` describing a Render web
service plus a worker for the poller.

1. Connect the repository to Render.
2. Choose "New > Blueprint" and select the repository.
3. Set the environment variables (`MAXIMO_*`, `LM_*`) in the Render
   service settings.
4. Apply.

The web service uses `python -m duplicate_monitor web`. The worker
runs `python -m duplicate_monitor poller`. The dashboard health check
hits the `/health` endpoint.

## 2. Docker

Build a production image:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY pyproject.toml .
COPY data/ data/
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 8502
CMD ["python", "-m", "duplicate_monitor", "both"]
```

Build and run:

```powershell
docker build -t incident-duplicate-monitor:1.0.0 .
docker run --rm -p 8502:8502 --env-file .env incident-duplicate-monitor:1.0.0
```

For docker-compose, split poller and web into two services that share
a volume for the SQLite database.

## 3. On-premise behind nginx

Run the poller and the dashboard as separate `systemd` units so each
can be restarted independently.

### Poller unit (`/etc/systemd/system/duplicate-monitor-poller.service`)

```ini
[Unit]
Description=Duplicate Monitor Poller
After=network.target

[Service]
Type=simple
User=duplicate-monitor
WorkingDirectory=/opt/duplicate-monitor
EnvironmentFile=/etc/duplicate-monitor.env
ExecStart=/opt/duplicate-monitor/.venv/bin/python -m duplicate_monitor poller
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Dashboard unit (`/etc/systemd/system/duplicate-monitor-web.service`)

```ini
[Unit]
Description=Duplicate Monitor Dashboard
After=network.target duplicate-monitor-poller.service

[Service]
Type=simple
User=duplicate-monitor
WorkingDirectory=/opt/duplicate-monitor
EnvironmentFile=/etc/duplicate-monitor.env
ExecStart=/opt/duplicate-monitor/.venv/bin/python -m duplicate_monitor web
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### nginx location block

```nginx
location /dup/ {
    proxy_pass http://127.0.0.1:8502/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 120s;
}
```

## Environment variables in production

The recommended order of precedence:

1. Platform-managed secrets (Azure Key Vault, AWS Secrets Manager)
   injected as environment variables.
2. `EnvironmentFile=` for systemd.
3. `.env` files only for local development.

See [`security.md`](security.md) for the planned migration to a
managed secrets backend.

## State and rollback

- The service holds state in `monitor.db` (SQLite) and `live_scan.pkl`.
  Both are local files in the package directory.
- Rolling back the code does not require touching either file; the
  schema is forward-compatible across the 1.x line.
- To wipe state: stop the service, delete the two files, restart. The
  next scan will repopulate.
