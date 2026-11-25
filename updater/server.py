#!/usr/bin/env python3
"""Minimal HTTP updater sidecar for SEGA-HUB Compose services."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
TAG_RE = re.compile(r"^(latest|v?\d+\.\d+\.\d+)$")


def parse_semver(tag: str) -> Optional[Tuple[int, int, int]]:
    match = SEMVER_RE.match(tag.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_image_map(raw: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        svc, image = item.split("=", 1)
        svc = svc.strip()
        image = image.strip()
        if svc and image:
            mapping[svc] = image
    return mapping


def _fetch_ghcr_token(repo: str) -> str:
    url = f"https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io"
    req = urllib.request.Request(url, headers={"User-Agent": "sega-hub-updater"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("token") or "")


def fetch_remote_ghcr_info(image: str) -> Dict[str, Any]:
    """Query GHCR Registry v2 API for highest semver tag and latest digest."""
    repo = image.removeprefix("ghcr.io/").strip("/")
    token = _fetch_ghcr_token(repo)
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "sega-hub-updater",
    }

    tags_url = f"https://ghcr.io/v2/{repo}/tags/list?n=100"
    tags_req = urllib.request.Request(tags_url, headers=headers)
    highest_tag = ""
    highest_ver: Optional[Tuple[int, int, int]] = None
    with urllib.request.urlopen(tags_req, timeout=10) as resp:
        tags_data = json.loads(resp.read().decode("utf-8"))
        for raw_tag in tags_data.get("tags") or []:
            parsed = parse_semver(str(raw_tag))
            if parsed is None:
                continue
            if highest_ver is None or parsed > highest_ver:
                highest_ver = parsed
                highest_tag = f"v{parsed[0]}.{parsed[1]}.{parsed[2]}"

    manifest_url = f"https://ghcr.io/v2/{repo}/manifests/latest"
    manifest_headers = {
        **headers,
        "Accept": ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]),
    }
    head_req = urllib.request.Request(manifest_url, headers=manifest_headers, method="HEAD")
    latest_digest = ""
    with urllib.request.urlopen(head_req, timeout=10) as resp:
        latest_digest = resp.headers.get("Docker-Content-Digest", "")

    return {
        "image": image,
        "latest_version": highest_tag,
        "latest_digest": latest_digest,
    }


@dataclass
class UpdaterConfig:
    host: str = "0.0.0.0"
    port: int = 8090
    token: str = ""
    compose_file: str = "/compose/docker-compose.yml"
    project_dir: str = "/compose"
    allowed_services: Tuple[str, ...] = ("sega", "bas-gateway")
    image_map: Dict[str, str] = field(default_factory=lambda: {
        "sega": "ghcr.io/pt-perkasa-pilar-utama/sega",
        "bas-gateway": "ghcr.io/pt-perkasa-pilar-utama/bas-iot-gateway",
    })

    @classmethod
    def from_env(cls) -> "UpdaterConfig":
        allowed = tuple(
            s.strip()
            for s in os.environ.get("UPDATER_ALLOWED_SERVICES", "sega,bas-gateway").split(",")
            if s.strip()
        )
        raw_map = os.environ.get(
            "UPDATER_IMAGE_MAP",
            "sega=ghcr.io/pt-perkasa-pilar-utama/sega,"
            "bas-gateway=ghcr.io/pt-perkasa-pilar-utama/bas-iot-gateway",
        )
        return cls(
            host=os.environ.get("UPDATER_HOST", "0.0.0.0"),
            port=int(os.environ.get("UPDATER_PORT", "8090")),
            token=os.environ.get("UPDATER_TOKEN", ""),
            compose_file=os.environ.get("UPDATER_COMPOSE_FILE", "/compose/docker-compose.yml"),
            project_dir=os.environ.get("UPDATER_PROJECT_DIR", "/compose"),
            allowed_services=allowed,
            image_map=parse_image_map(raw_map),
        )


class UpdaterService:
    """Coordinates status checks and Docker Compose pull/recreate operations."""

    def __init__(
        self,
        config: UpdaterConfig,
        runner: Optional[Callable[[list[str]], subprocess.CompletedProcess[str]]] = None,
        remote_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self._runner = runner or self._default_runner
        self._remote_fetcher = remote_fetcher or fetch_remote_ghcr_info
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}

    def _default_runner(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=self.config.project_dir,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def _compose_cmd(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            self.config.compose_file,
            *args,
        ]

    def get_status(self, service: str, current_version: str = "") -> Dict[str, Any]:
        if service not in self.config.allowed_services:
            raise ValueError(f"Service {service!r} is not allowed")

        image = self.config.image_map.get(service, "")
        remote: Dict[str, Any] = {"image": image, "latest_version": "", "latest_digest": ""}
        remote_error = ""
        if image:
            try:
                remote = self._remote_fetcher(image)
            except Exception as exc:  # pragma: no cover - network/registry failure
                remote_error = str(exc)

        cur_ver = parse_semver(current_version) if current_version else None
        lat_ver = parse_semver(remote.get("latest_version") or "")
        update_available = bool(cur_ver and lat_ver and lat_ver > cur_ver)

        with self._lock:
            svc_state = dict(self._state.get(service, {}))

        return {
            "service": service,
            "image": image,
            "current_version": current_version,
            "latest_version": remote.get("latest_version", ""),
            "latest_digest": remote.get("latest_digest", ""),
            "update_available": update_available,
            "updating": bool(svc_state.get("updating", False)),
            "last_error": remote_error or svc_state.get("last_error", ""),
            "last_updated_at": svc_state.get("last_updated_at", ""),
        }

    def trigger_update(self, service: str, tag: str = "latest", *, sync_recreate: bool = False) -> Dict[str, Any]:
        if service not in self.config.allowed_services:
            raise ValueError(f"Service {service!r} is not allowed")
        tag = (tag or "latest").strip()
        if not TAG_RE.match(tag):
            raise ValueError(f"Invalid tag {tag!r}")

        with self._lock:
            svc_state = self._state.setdefault(service, {})
            if svc_state.get("updating"):
                raise RuntimeError(f"Update already in progress for {service}")
            svc_state["updating"] = True
            svc_state["last_error"] = ""

        # Pull synchronously so registry/auth errors are returned immediately while caller is alive.
        pull_res = self._runner(self._compose_cmd("pull", service))
        if pull_res.returncode != 0:
            err = (pull_res.stderr or pull_res.stdout or "docker compose pull failed").strip()
            with self._lock:
                self._state[service]["updating"] = False
                self._state[service]["last_error"] = err
            raise RuntimeError(err)

        def _recreate() -> None:
            if not sync_recreate:
                time.sleep(0.3)
            try:
                up_res = self._runner(self._compose_cmd("up", "-d", "--no-deps", service))
                with self._lock:
                    self._state[service]["updating"] = False
                    if up_res.returncode != 0:
                        self._state[service]["last_error"] = (
                            up_res.stderr or up_res.stdout or "docker compose up failed"
                        ).strip()
                    else:
                        self._state[service]["last_error"] = ""
                        self._state[service]["last_updated_at"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
            except Exception as exc:
                with self._lock:
                    self._state[service]["updating"] = False
                    self._state[service]["last_error"] = str(exc)

        if sync_recreate:
            _recreate()
        else:
            threading.Thread(target=_recreate, daemon=True).start()

        return {
            "status": "redeploying",
            "service": service,
            "tag": tag,
        }


def make_handler(service: UpdaterService, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {token}"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if parsed.path == "/v1/status":
                qs = parse_qs(parsed.query)
                svc = (qs.get("service") or [""])[0].strip()
                cur = (qs.get("current_version") or [""])[0].strip()
                try:
                    data = service.get_status(svc, current_version=cur)
                    self._send_json(HTTPStatus.OK, data)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if parsed.path == "/v1/update":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                    return
                svc = str(body.get("service") or "").strip()
                tag = str(body.get("tag") or "latest").strip()
                try:
                    data = service.trigger_update(svc, tag=tag)
                    self._send_json(HTTPStatus.ACCEPTED, data)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def main() -> None:
    cfg = UpdaterConfig.from_env()
    svc = UpdaterService(cfg)
    server = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(svc, cfg.token))
    print(f"SEGA-HUB updater listening on {cfg.host}:{cfg.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
