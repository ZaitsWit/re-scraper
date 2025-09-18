# app/secrets.py
import os, hvac

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
ROLE_ID    = os.getenv("VAULT_ROLE_ID")
SECRET_ID  = os.getenv("VAULT_SECRET_ID")

client = hvac.Client(url=VAULT_ADDR)
resp = client.auth_approle(ROLE_ID, SECRET_ID)
client.token = resp["auth"]["client_token"]

def get_secret(name: str) -> dict:
    data = client.secrets.kv.v2.read_secret_version(
        path=f"app/{name}", mount_point="kv"
    )
    return data["data"]["data"]
