# Path PC Systemd Services

These units keep the Path PC drive stack alive across boot and process crashes.

- `carla-custommaps` is the CARLA Docker container. Set Docker restart policy with:
  `docker update --restart unless-stopped carla-custommaps`
- `v2x-drive.service` runs `scripts/launch-drive.sh` after Docker and CARLA are reachable.
- `v2x-perception.service` runs `apps/perception/process_video.py` against the live street camera streams.
- `scripts/wait-for-carla.sh` is used by `v2x-drive.service` to wait for the existing CARLA container and RPC server.
- `v2x-cloudflared-drive.service` runs the named Cloudflare Tunnel launcher for the Drive WebSocket.
- `v2x-hourly-drive-restart.timer` runs `scripts/restart-drive-stack.sh` at the top of each hour.
- `v2x-drive-link-health.timer` verifies the public frontend `config.json` WebSocket URL every five minutes and republishes the latest Quick Tunnel URL if the advertised link is stale.

Install on the Path PC:

```bash
sudo install -m 0644 scripts/systemd/v2x-drive.service /etc/systemd/system/v2x-drive.service
sudo install -m 0644 scripts/systemd/v2x-perception.service /etc/systemd/system/v2x-perception.service
sudo install -m 0644 scripts/systemd/v2x-cloudflared-drive.service /etc/systemd/system/v2x-cloudflared-drive.service
sudo install -m 0644 scripts/systemd/v2x-drive-link-health.service /etc/systemd/system/v2x-drive-link-health.service
sudo install -m 0644 scripts/systemd/v2x-drive-link-health.timer /etc/systemd/system/v2x-drive-link-health.timer
sudo install -m 0644 scripts/systemd/v2x-hourly-drive-restart.service /etc/systemd/system/v2x-hourly-drive-restart.service
sudo install -m 0644 scripts/systemd/v2x-hourly-drive-restart.timer /etc/systemd/system/v2x-hourly-drive-restart.timer
sudo systemctl daemon-reload
docker update --restart unless-stopped carla-custommaps
sudo systemctl enable --now v2x-drive.service v2x-cloudflared-drive.service v2x-drive-link-health.timer v2x-hourly-drive-restart.timer
```

Install the perception Python environment before enabling perception:

```bash
python3.10 -m venv /home/path/V2XCarla/perception-venv
/home/path/V2XCarla/perception-venv/bin/pip install --upgrade pip
/home/path/V2XCarla/perception-venv/bin/pip install -r /home/path/V2XCarla/v2x-backend/apps/perception/requirements.txt
sudo systemctl enable --now v2x-perception.service
journalctl -u v2x-perception.service -f
```

`v2x-perception.service` defaults to `V2X_PERCEPTION_UPLOAD=false` while validating camera calibration
and model behavior. Set `V2X_PERCEPTION_UPLOAD=true` in the unit or a systemd override once the output
is ready to populate the Objects DB table.

Verify the local stream server:

```bash
curl http://127.0.0.1:8090/health
curl -I http://127.0.0.1:8090/streams/ch1.mjpg
```

Point the dashboard at the perception streams with either explicit `PERCEPTION_STREAM_URLS` or:

```bash
export PERCEPTION_STREAM_BASE_URL="https://perception.path2v2x.net"
export PERCEPTION_STREAM_PATH_TEMPLATE="/streams/{camera_id}.mjpg"
```

## Named Cloudflare Tunnel

The Drive frontend expects the public WebSocket endpoint to be stable:

```text
wss://drive.path2v2x.net
```

Use a named Cloudflare Tunnel for this endpoint. Quick Tunnels (`*.trycloudflare.com`) are process-scoped and should only be used as a temporary break-glass path.

There are two supported named-tunnel modes:

- Remotely managed tunnel: create the tunnel and public hostname in the Cloudflare Zero Trust dashboard, then put the tunnel token in `/etc/v2x-drive-tunnel.env` as `TUNNEL_TOKEN=...`.
- Locally managed tunnel: run `scripts/provision-cloudflare-drive-tunnel.sh` after `cloudflared tunnel login`; it creates/updates the tunnel, routes `drive.path2v2x.net`, writes `/etc/cloudflared/v2x-drive.yml`, and writes `/etc/v2x-drive-tunnel.env`.

Expected local origin:

```text
http://localhost:8765
```

Install the launcher on the Path PC:

```bash
sudo install -m 0755 scripts/launch-cloudflared-drive-tunnel.sh /home/path/V2XCarla/v2x-backend/scripts/launch-cloudflared-drive-tunnel.sh
sudo install -m 0644 scripts/systemd/v2x-cloudflared-drive.service /etc/systemd/system/v2x-cloudflared-drive.service
sudo systemctl daemon-reload
sudo systemctl restart v2x-cloudflared-drive.service
```

Verify:

```bash
systemctl status v2x-cloudflared-drive.service --no-pager
curl -I https://drive.path2v2x.net
/home/path/venvs/vw-scenario/bin/python - <<'PY'
import asyncio
import websockets

async def main():
    async with websockets.connect("wss://drive.path2v2x.net"):
        print("WS_OK")

asyncio.run(main())
PY
```

If DNS cannot be moved to Cloudflare, keep Quick Tunnel as the public transport and publish the
current URL through Amplify branch environment variables:

```bash
/home/path/V2XCarla/v2x-backend/scripts/publish-drive-amplify-config.sh
curl https://path2v2x.net/config.json
```

The health check can be run manually and will test the same URL the frontend receives:

```bash
DRIVE_LINK_HEALTH_REPAIR=true /home/path/V2XCarla/v2x-backend/scripts/check-drive-frontend-link.sh
systemctl status v2x-drive-link-health.timer --no-pager
journalctl -u v2x-drive-link-health.service -n 80 --no-pager
```

To publish automatically from the hourly restart flow, set this environment value on
`v2x-hourly-drive-restart.service` or in the service manager environment:

```text
PUBLISH_DRIVE_FRONTEND_CONFIG=true
```

Break-glass Quick Tunnel mode is still available by setting `ALLOW_QUICK_TUNNEL=1` in `/etc/v2x-drive-tunnel.env`.
