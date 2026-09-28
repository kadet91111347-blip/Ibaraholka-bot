#!/bin/bash
# Run as root on fresh Ubuntu 22.04/24.04
set -e

echo "=== Update system ==="
apt update && apt -y upgrade

echo "=== Install packages ==="
apt install -y python3.12 python3.12-venv python3-pip postgresql postgresql-contrib nginx certbot python3-certbot-nginx git ufw

echo "=== Setup PostgreSQL ==="
sudo -u postgres psql -c "CREATE USER ibaraholka WITH PASSWORD 'CHANGEME';"
sudo -u postgres psql -c "CREATE DATABASE ibaraholka OWNER ibaraholka;"

echo "=== Create app user ==="
useradd -r -m -d /opt/ibaraholka -s /bin/bash ibaraholka

echo "=== Clone repo ==="
sudo -u ibaraholka git clone https://github.com/kadet9111347-blip/Ibaraholka-bot.git /opt/ibaraholka

cd /opt/ibaraholka
sudo -u ibaraholka python3.12 -m venv venv
sudo -u ibaraholka venv/bin/pip install -U pip
sudo -u ibaraholka venv/bin/pip install -r requirements.txt

echo "=== Create .env from example ==="
sudo -u ibaraholka cp .env.example .env
# Edit secrets manually after first run

echo "=== Setup nginx ==="
cp /workspace/deploy/nginx-ibaraholka.conf /etc/nginx/sites-available/ibaraholka
ln -sf /etc/nginx/sites-available/ibaraholka /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "=== Get SSL ==="
# First start without SSL, then run certbot
certbot --nginx -d your-domain --non-interactive --agree-tos -m you@email.com

echo "=== Setup systemd ==="
cp /workspace/deploy/ibaraholka.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable ibaraholka
systemctl start ibaraholka

echo "=== Firewall ==="
ufw allow 22,80,443/tcp
ufw --force enable

echo "=== Done! ==="
systemctl status ibaraholka --no-pager
