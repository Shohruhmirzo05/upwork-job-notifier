#!/usr/bin/env bash
set -euo pipefail

# One-time bootstrap for the notifier. It deliberately does not touch nginx,
# PostgreSQL, Redis, firewall rules, or any Prime Truck application files.

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3-venv

if ! getent group upwork-notifier >/dev/null; then
  groupadd --system upwork-notifier
fi
if ! getent passwd upwork-notifier >/dev/null; then
  useradd \
    --system \
    --gid upwork-notifier \
    --home-dir /var/lib/upwork-notifier \
    --shell /usr/sbin/nologin \
    upwork-notifier
fi
if ! getent passwd upwork-deploy >/dev/null; then
  useradd --create-home --shell /bin/bash upwork-deploy
fi

install -d -o upwork-notifier -g upwork-notifier -m 0750 /var/lib/upwork-notifier
install -d -o root -g root -m 0755 /opt/upwork-notifier/releases
install -d -o upwork-deploy -g upwork-deploy -m 0750 /home/upwork-deploy/incoming
install -d -o upwork-deploy -g upwork-deploy -m 0700 /home/upwork-deploy/.ssh
install -o upwork-deploy -g upwork-deploy -m 0600 \
  /tmp/id_ed25519.pub /home/upwork-deploy/.ssh/authorized_keys

install -o root -g root -m 0755 \
  /tmp/deploy-upwork-notifier /usr/local/sbin/deploy-upwork-notifier
install -o root -g root -m 0644 \
  /tmp/upwork-notifier.service /etc/systemd/system/upwork-notifier.service

visudo -cf /tmp/upwork-deploy.sudoers
install -o root -g root -m 0440 \
  /tmp/upwork-deploy.sudoers /etc/sudoers.d/upwork-deploy

systemctl daemon-reload
systemctl enable upwork-notifier.service

rm -f \
  /tmp/id_ed25519.pub \
  /tmp/deploy-upwork-notifier \
  /tmp/upwork-notifier.service \
  /tmp/upwork-deploy.sudoers

echo "notifier server bootstrap complete"
