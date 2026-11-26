# A Mac's first step onto its Cloudflare route, pasted into Terminal by the Mac's own user (Remote Login already on, for
# that user only). scripts/windows-pc-bootstrap.sh print-mac fills it in on fcvm-arm; nothing in it is secret.
#   1. authorizes fcvm-arm's key for this Mac (~/.ssh/to-@@ALIAS@@ there), the one scripts/mac-ssh-connector-setup.sh logs in with
#   2. pins fcvm-arm's host key for the tunnel, and makes the Mac's own tunnel key, printing its public half for `allow`
#   3. keeps a reverse tunnel to fcvm-arm's 127.0.0.1:@@PORT@@ open as pcboot (never ubuntu), retrying until `allow` has run;
#      close this Terminal window once the connector is installed
mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qF '@@CLIENT_KEY@@' ~/.ssh/authorized_keys || echo '@@CLIENT_KEY@@ fcvm-arm-to-@@ALIAS@@' >> ~/.ssh/authorized_keys
echo '@@BOX_HOST_KEY_LINE@@' > ~/.ssh/known_hosts.@@ALIAS@@-bootstrap
[ -f ~/.ssh/@@ALIAS@@-bootstrap ] || ssh-keygen -q -t ed25519 -N '' -C @@ALIAS@@-bootstrap-tunnel -f ~/.ssh/@@ALIAS@@-bootstrap
echo; echo "Send this line back:"; cat ~/.ssh/@@ALIAS@@-bootstrap.pub; echo
until ssh -N -i ~/.ssh/@@ALIAS@@-bootstrap -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ssh/known_hosts.@@ALIAS@@-bootstrap -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:@@PORT@@:127.0.0.1:22 @@BOX@@; do sleep 10; done
