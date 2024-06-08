sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y build-essential libpcap-dev libpcre3-dev \
libyaml-dev zlib1g-dev libcap-ng-dev libmagic-dev \
libjansson-dev libnss3-dev libgeoip-dev pkg-config \
libnet1-dev liblz4-dev wget

sudo add-apt-repository ppa:oisf/suricata-stable
sudo apt-get update
sudo apt-get install -y suricata
sudo apt-get install -y elasticsearch kibana
