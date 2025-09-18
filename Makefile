VAULT_DIR=infrastructurevault

vault-up
tdocker compose -f $(VAULT_DIR)docker-compose.yml up -d

vault-bootstrap
tbash $(VAULT_DIR)scriptsbootstrap.sh

put-secrets
tVAULT_ADDR=http127.0.0.18200 vault login $${ROOT_TOKEN}; 
tvault kv put kvappdb url=postgresuserpass@host5432db; 
