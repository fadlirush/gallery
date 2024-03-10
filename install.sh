#!/bin/bash

sudo apt install update -y

sudo apt install upgrade -y

sudo apt-get install -y apache2 postgresql php libapache2-mod-php php-pgsql php-fpm libapache2-mod-fcgid

sudo systemctl enable apache2

sudo systemctl enable postgresql

sudo systemctl enable php7.4-fpm

sudo systemctl status apache2

sudo systemctl status postgresql

sudo systemctl status php7.4-fpm

sudo systemctl restart apache2
