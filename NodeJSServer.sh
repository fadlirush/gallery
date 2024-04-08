#!/bin/bash

sudo apt install update -y

sudo apt install upgrade -y

curl -s https://deb.nodesource.com/setup_18.x | sudo bash

sudo apt install nodejs -y

sudo apt-get install -y postgresql

node -v

sudo apt install redis-server -y

npm i pg -y

sudo systemctl enable postgresql

sudo systemctl status redis

sudo systemctl status postgresql

sudo adduser server

sudo usermod -aG sudo server

sudo -u postgres psql



