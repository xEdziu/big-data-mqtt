#!/bin/sh
# Zakłada konta użytkowników Grafany przez API (idempotentnie: istniejących kont nie rusza, hasła nie resetuje).
set -eu

API="http://grafana:3000/api"
AUTH="admin:${GRAFANA_ADMIN_PASSWORD}"

until curl -sf "$API/health" >/dev/null; do sleep 2; done

# login|e-mail|imię i nazwisko
for user in \
  "adrian.goral|adrian.goral@gmail.com|Adrian Goral" \
  "paulinaszulc23|paulinaszulc23@gmail.com|Paulina Szulc"
do
  login=${user%%|*}; rest=${user#*|}; email=${rest%%|*}; name=${rest#*|}

  if curl -sf -u "$AUTH" "$API/users/lookup?loginOrEmail=$email" >/dev/null; then
    echo "konto $email już istnieje - pomijam"
    continue
  fi

  id=$(curl -sf -u "$AUTH" -H "Content-Type: application/json" -X POST "$API/admin/users" \
    -d "{\"name\":\"$name\",\"email\":\"$email\",\"login\":\"$login\",\"password\":\"${GRAFANA_USERS_DEFAULT_PASSWORD}\",\"OrgId\":1}" \
    | sed -n 's/.*"id":\([0-9]*\).*/\1/p')

  # Rola Admin w organizacji: pełna edycja dashboardów, alertów i źródeł danych
  curl -sf -u "$AUTH" -H "Content-Type: application/json" -X PATCH "$API/org/users/$id" -d '{"role":"Admin"}' >/dev/null
  echo "utworzono konto $email (id $id)"
done
