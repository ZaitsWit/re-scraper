import os, time
import hvac
from loguru import logger

class VaultDBCreds:
    def __init__(self, lease_id: str, username: str, password: str, lease_duration: int):
        self.lease_id = lease_id
        self.username = username
        self.password = password
        self.lease_duration = lease_duration
        self.obtained_at = time.time()

    @property
    def ttl_left(self) -> float:
        return self.lease_duration - (time.time() - self.obtained_at)

class VaultClient:
    def __init__(self):
        self.addr = os.getenv("VAULT_ADDR", "http://vault:8200")
        self.token = os.getenv("VAULT_TOKEN")
        if not self.token:
            raise RuntimeError("VAULT_TOKEN is not set")
        self.role = os.getenv("VAULT_DB_ROLE", "app-readwrite")
        self.client = hvac.Client(url=self.addr, token=self.token)
        if not self.client.is_authenticated():
            raise RuntimeError("Vault auth failed")
        self._cached: VaultDBCreds | None = None

    def get_db_creds(self) -> VaultDBCreds:
        # обновляем за 10 минут до истечения
        if self._cached and self._cached.ttl_left > 600:
            return self._cached
        logger.info("[VAULT] fetching dynamic DB creds")
        path = f"database/creds/{self.role}"
        resp = self.client.read(path)
        if not resp or "data" not in resp:
            raise RuntimeError(f"Vault empty response for {path}")
        data = resp["data"]
        lease_id = resp.get("lease_id")
        lease_duration = resp.get("lease_duration", 3600)
        self._cached = VaultDBCreds(
            lease_id=lease_id,
            username=data["username"],
            password=data["password"],
            lease_duration=lease_duration,
        )
        return self._cached

vault_client = VaultClient()
