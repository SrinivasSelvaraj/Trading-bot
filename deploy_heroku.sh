#!/usr/bin/env bash
# Deploy the paper bot + dashboard to Heroku without the Heroku CLI.
#
#   export HEROKU_API_KEY=...          # from `heroku authorizations:create` or Account Settings
#   ./deploy_heroku.sh my-btc-paper-bot [git-ref]
#
# Creates the app if it doesn't exist, sets safe config, pushes the code and starts one web dyno.
set -euo pipefail

APP="${1:?usage: $0 <app-name> [git-ref]}"
REF="${2:-HEAD}"
: "${HEROKU_API_KEY:?HEROKU_API_KEY is not set}"

API=https://api.heroku.com
H=(-H "Accept: application/vnd.heroku+json; version=3" -H "Authorization: Bearer ${HEROKU_API_KEY}" -H "Content-Type: application/json")

if ! curl -fsS "${H[@]}" "$API/apps/$APP" >/dev/null 2>&1; then
  echo "Creating Heroku app '$APP'..."
  curl -fsS "${H[@]}" -X POST "$API/apps" -d "{\"name\":\"$APP\",\"stack\":\"heroku-24\"}" >/dev/null
fi

echo "Setting config vars (paper mode only)..."
curl -fsS "${H[@]}" -X PATCH "$API/apps/$APP/config-vars" \
  -d '{"PAPER_MODE":"True","TIMEZONE":"Asia/Kolkata"}' >/dev/null

# The dashboard's trading buttons need a key on a public URL. Create one the first time only.
if ! curl -fsS "${H[@]}" "$API/apps/$APP/config-vars" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('DASHBOARD_KEY') else 1)"; then
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
  curl -fsS "${H[@]}" -X PATCH "$API/apps/$APP/config-vars" -d "{\"DASHBOARD_KEY\":\"$KEY\"}" >/dev/null
  echo "Dashboard key (needed for the trading buttons): $KEY"
fi

echo "Pushing $REF..."
git push "https://heroku:${HEROKU_API_KEY}@git.heroku.com/${APP}.git" "${REF}:refs/heads/main"

echo "Starting one web dyno..."
curl -fsS "${H[@]}" -X PATCH "$API/apps/$APP/formation/web" -d '{"quantity":1}' >/dev/null

URL=$(curl -fsS "${H[@]}" "$API/apps/$APP" | python3 -c "import json,sys; print(json.load(sys.stdin)['web_url'])")
echo "Deployed: $URL"
