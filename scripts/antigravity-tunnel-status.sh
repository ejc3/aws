# Sourced on fcvm-arm. Only the existing service token for this exact host may be
# sent to it. A 302, a denied token or an SSH error does not establish an outage.
antigravity_tunnel_down() (
  set -eu -o pipefail
  case "$1" in
    mac) endpoint=macbook; secret=macbook-ssh-access ;;
    air|mini|colton-mac|colin-mac|colin-pc) endpoint=$1; secret=$1-ssh-access-fcvm ;;
    *) return 1 ;;
  esac
  response=$(
    aws secretsmanager get-secret-value --region us-west-1 --secret-id "$secret" --query SecretString --output text |
      jq -er '"CF-Access-Client-Id: " + .client_id + "\nCF-Access-Client-Secret: " + .client_secret' |
      curl -q -sS --proto '=https' --connect-timeout 5 --max-time 10 --max-filesize 32768 \
        --header @- --header 'Accept: application/json' --write-out '\n%{http_code}' "https://$endpoint-ssh.cc-games.dev/"
  ) || return 1
  [[ ${response##*$'\n'} == 530 ]] || return 1
  body=${response%$'\n'*}
  [[ $body =~ ^'error code: 1033'[[:space:]]*$ ]] || jq -e '.error_code == 1033' <<<"$body" >/dev/null 2>&1
)
