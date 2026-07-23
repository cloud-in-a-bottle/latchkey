import os
import secrets
from pathlib import Path

import attr


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@attr.s(auto_attribs=True, frozen=True)
class AppConfig:
    app_name: str
    app_data_dir: Path
    router_url: str
    app_token: str
    zone_domain: str
    gateway_host: str
    gateway_port: int
    # Generated fresh each boot; shared between the spawned gateway process and our client.
    gateway_password: str
    vnc_host: str
    vnc_port: int
    display: str
    screen_geometry: str
    # Idle seconds before the reaper stops the gateway / display stack; <= 0 keeps them running.
    gateway_idle_seconds: float
    display_idle_seconds: float
    # Hard cap on one VNC viewer connection, so a stuck client can't hold the display stack forever.
    vnc_max_session_seconds: float

    @property
    def own_url(self) -> str:
        return f"https://{self.app_name}.{self.zone_domain}"

    @property
    def gateway_url(self) -> str:
        return f"http://{self.gateway_host}:{self.gateway_port}"

    @property
    def latchkey_home(self) -> Path:
        return self.app_data_dir / "latchkey_home"

    @property
    def encryption_key_path(self) -> Path:
        return self.app_data_dir / "encryption_key"

    @property
    def consumer_permissions_dir(self) -> Path:
        return self.app_data_dir / "consumer_permissions"


def load_config() -> AppConfig:
    return AppConfig(
        app_name=_require_env("OPENHOST_APP_NAME"),
        app_data_dir=Path(_require_env("OPENHOST_APP_DATA_DIR")),
        router_url=_require_env("OPENHOST_ROUTER_URL"),
        app_token=_require_env("OPENHOST_APP_TOKEN"),
        zone_domain=_require_env("OPENHOST_ZONE_DOMAIN"),
        gateway_host="127.0.0.1",
        gateway_port=int(os.environ.get("LATCHKEY_GATEWAY_LISTEN_PORT", "1989")),
        gateway_password=secrets.token_urlsafe(32),
        vnc_host="127.0.0.1",
        vnc_port=int(os.environ.get("LATCHKEY_VNC_PORT", "5900")),
        display=os.environ.get("DISPLAY", ":99"),
        # 8:5, matching #vnc-screen's aspect-ratio.
        screen_geometry=os.environ.get("LATCHKEY_SCREEN_GEOMETRY", "1600x1000x24"),
        gateway_idle_seconds=float(os.environ.get("LATCHKEY_GATEWAY_IDLE_SECONDS", "300")),
        display_idle_seconds=float(os.environ.get("LATCHKEY_DISPLAY_IDLE_SECONDS", "60")),
        vnc_max_session_seconds=float(os.environ.get("LATCHKEY_VNC_MAX_SESSION_SECONDS", "1800")),
    )


def novnc_dir() -> Path:
    return Path(os.environ.get("LATCHKEY_NOVNC_DIR", "/opt/novnc"))
