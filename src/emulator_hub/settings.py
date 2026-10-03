from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUB_")

    namespace: str = "emulator-hub"
    emulator_image: str
    # Comma-separated LAN IPs of slot-0..N LoadBalancer Services, in slot order.
    slot_ips: str
    db_path: str = "/data/hub.db"
    api_token: str
    reap_interval_s: float = 15
    boot_timeout_s: float = 180
    # Hard stop for a lease regardless of heartbeats.
    max_age_s: float = 4 * 3600
    ui_port: int = 8080
    machine_port: int = 8081

    @property
    def slot_ip_list(self) -> tuple[str, ...]:
        return tuple(ip.strip() for ip in self.slot_ips.split(",") if ip.strip())
