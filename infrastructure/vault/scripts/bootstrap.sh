#!/usr/bin/env bash
set -euo pipefail
export VAULT_ADDR="http://127.0.0.1:8200"

docker compose -f "$(dirname "$0")/../docker-compose.yml" up -d

# init (однократно)
if [ ! -f "$(dirname "$0")/../logs/init.out" ]; then
  docker exec vault sh -lc 'vault operator init -key-shares=1 -key-threshold=1 | tee /vault/logs/init.out'
fi

UNSEAL=$(docker exec vault sh -lc 'grep "Unseal Key 1:" /vault/logs/init.out | awk "{print \$NF}"')
ROOT=$(docker exec vault sh -lc 'grep "Initial Root Token:" /vault/logs/init.out | awk "{print \$NF}"')

docker exec vault sh -lc "vault operator unseal ${UNSEAL}"
docker exec vault sh -lc "VAULT_ADDR=http://127.0.0.1:8200 vault login ${ROOT} && \
  vault secrets enable -path=kv kv-v2 || true && \
  vault auth enable approle || true && \
  cat >/tmp/app-policy.hcl <<'EOF'
path \"kv/data/app/*\" { capabilities = [\"read\"] }
EOF
  vault policy write app-policy /tmp/app-policy.hcl && \
  vault write auth/approle/role/devops-app token_ttl=1h token_max_ttl=4h \
    policies=\"app-policy\" secret_id_num_uses=1 secret_id_ttl=30m || true && \
  vault kv put kv/app/telegram token=\"tg-xxx\" chat_id=\"123456\" && \
  vault kv put kv/app/db url=\"postgres://user:pass@host:5432/db\"
  echo 'ROLE_ID:' && vault read -field=role_id auth/approle/role/devops-app/role-id
  echo 'SECRET_ID:' && vault write -f -field=secret_id auth/approle/role/devops-app/secret-id"
