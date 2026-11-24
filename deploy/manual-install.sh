#!/usr/bin/env bash
# Install a prepared notifier release using the server's existing credentials.
set -euo pipefail

if [[ ${EUID} -ne 0 || $# -ne 2 ]]; then
  echo 'Run as root: manual-install.sh PACKAGE_DIRECTORY RELEASE_ID' >&2
  exit 64
fi
package=$(realpath "$1")
release=$2
[[ "$release" =~ ^[0-9a-f]{7,40}$ ]] || { echo 'Invalid release ID' >&2; exit 64; }
root=/opt/upwork-notifier
target="$root/releases/$release"
service=upwork-notifier.service
[[ -f /etc/upwork-notifier/env.json && -L "$root/current" ]] || {
  echo 'Existing notifier installation not found; no server changes made' >&2; exit 66;
}
[[ $(systemctl show -p User --value "$service") == upwork-notifier ]] || {
  echo 'Unexpected service identity; no server changes made' >&2; exit 66;
}
[[ ! -e "$target" ]] || { echo 'Release already exists; inspect before retrying' >&2; exit 73; }

# Validate the package before stopping the existing service. No credentials are output.
(cd "$package" && sha256sum -c MANIFEST.sha256)
python3 - "$package/notifier.py" <<'PY'
import ast, sys
from pathlib import Path
s = Path(sys.argv[1]).read_text()
ast.parse(s)
for banned in ('VisitorJobSearch', 'visitor_gql_token', 'api/graphql/v1', 'impersonate='):
    if banned in s:
        raise SystemExit('Refusing a release containing retired scraping code')
PY

# Stop only this bot. If installation fails, keep it stopped; never restart the
# previous scraper automatically. All previous releases and credentials remain intact.
systemctl stop "$service"
install -d -o upwork-notifier -g upwork-notifier -m 0750 "$target"
cp -a "$package"/. "$target/"
chown -R upwork-notifier:upwork-notifier "$target"
runuser -u upwork-notifier -- python3 -m venv "$target/.venv"
runuser -u upwork-notifier -- "$target/.venv/bin/pip" install --disable-pip-version-check -q -r "$target/requirements.txt"
(cd "$target" && runuser -u upwork-notifier -- "$target/.venv/bin/python" -m unittest -q)

previous=$(readlink -f "$root/current")
printf '%s\n' "$previous" > "$target/PREVIOUS_RELEASE"
ln -s "$target" "$root/.current-$release"
mv -Tf "$root/.current-$release" "$root/current"
systemctl start "$service"
systemctl is-active --quiet "$service"
pid=$(systemctl show -p MainPID --value "$service")
[[ "$pid" != 0 && $(readlink -f "/proc/$pid/cwd") == "$target" ]]
printf 'release=%s service=active pid=%s\n' "$release" "$pid"
sha256sum "$target/notifier.py" "$target/official_bridge.py"
