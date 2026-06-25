#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   Spine-Leaf SDN Topology — QoS TCP Testing                  ║
║   Topologi  : 2 Spine × 4 Leaf × 8 Host                      ║
║   Skenario  : S1 Baseline + S3 Incast                        ║
║   Controller: ONOS via Incus                                 ║
╚══════════════════════════════════════════════════════════════╝

Cara pakai:
    sudo python3 qos_test.py

Pastikan sebelumnya:
    - ONOS sudah berjalan (Incus) dan dapat diakses di port 6653
    - App ONOS yang aktif: openflow, fwd, hostprovider
    - Tools terinstall: iperf3, hping3, tcpdump

Hasil pengukuran (file teks iperf3, ping, hping3, pcap) disimpan di:
    /tmp/qos_YYYYMMDD_HHMMSS/
"""

import os
import time
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

# ══════════════════════════════════════════════════════════════
#  KONFIGURASI
# ══════════════════════════════════════════════════════════════
CONTROLLER_IP   = '10.10.20.230'
CONTROLLER_PORT = 6653

OUTDIR = f'/tmp/qos_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

# Durasi pengujian iperf3 (detik)
DURATION = 30

# Parameter link
SPINE_LEAF_BW    = 1000   # Mbps
LEAF_HOST_BW     = 100    # Mbps
SPINE_LEAF_DELAY = '1ms'
LEAF_HOST_DELAY  = '0.5ms'
QUEUE_SIZE       = 200

# IP host
HOST_IPS = {
    'h1': '10.0.0.1', 'h2': '10.0.0.2',
    'h3': '10.0.0.3', 'h4': '10.0.0.4',
    'h5': '10.0.0.5', 'h6': '10.0.0.6',
    'h7': '10.0.0.7', 'h8': '10.0.0.8',
}
# ══════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────
#  TOPOLOGI
# ─────────────────────────────────────────────────────────────
def build_topology():
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=False,
        autoStaticArp=True
    )

    net.addController('c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)

    # Spine switches — DPID eksplisit (hindari konflik dengan Leaf)
    sp1 = net.addSwitch('s1', protocols='OpenFlow13', dpid='0000000000000001')
    sp2 = net.addSwitch('s2', protocols='OpenFlow13', dpid='0000000000000002')

    # Leaf switches — DPID 0x11–0x14
    lf1 = net.addSwitch('l1', protocols='OpenFlow13', dpid='0000000000000011')
    lf2 = net.addSwitch('l2', protocols='OpenFlow13', dpid='0000000000000012')
    lf3 = net.addSwitch('l3', protocols='OpenFlow13', dpid='0000000000000013')
    lf4 = net.addSwitch('l4', protocols='OpenFlow13', dpid='0000000000000014')

    # Host
    for name, ip in HOST_IPS.items():
        idx = int(name[1])
        net.addHost(name, ip=f'{ip}/24', mac=f'00:00:00:00:00:0{idx}')

    # Link Spine ↔ Leaf: 1Gbps, 1ms (full mesh — ECMP)
    sl = dict(bw=SPINE_LEAF_BW, delay=SPINE_LEAF_DELAY, max_queue_size=QUEUE_SIZE)
    for spine in [sp1, sp2]:
        for leaf in [lf1, lf2, lf3, lf4]:
            net.addLink(spine, leaf, **sl)

    # Link Leaf ↔ Host: 100Mbps, 0.5ms (bottleneck)
    lh = dict(bw=LEAF_HOST_BW, delay=LEAF_HOST_DELAY, max_queue_size=QUEUE_SIZE)
    net.addLink(lf1, net.get('h1'), **lh)
    net.addLink(lf1, net.get('h2'), **lh)
    net.addLink(lf2, net.get('h3'), **lh)
    net.addLink(lf2, net.get('h4'), **lh)
    net.addLink(lf3, net.get('h5'), **lh)
    net.addLink(lf3, net.get('h6'), **lh)
    net.addLink(lf4, net.get('h7'), **lh)
    net.addLink(lf4, net.get('h8'), **lh)

    return net


# ─────────────────────────────────────────────────────────────
#  HELPER
# ─────────────────────────────────────────────────────────────
def mkdir(path):
    os.makedirs(path, exist_ok=True)

def save(path, content):
    with open(path, 'w') as f:
        f.write(content)

def cap_start(host):
    """Mulai tcpdump di host (background) — hasilkan file .pcap"""
    iface  = f'{host.name}-eth0'
    output = f'{_cur_dir}/{host.name}.pcap'
    host.cmd(f'pkill -f "tcpdump.*{iface}"; tcpdump -i {iface} -w {output} &')
    time.sleep(0.3)

def cap_stop(*hosts):
    """Hentikan tcpdump"""
    for h in hosts:
        h.cmd('pkill -f tcpdump')
    time.sleep(0.8)

def srv_start(*hosts):
    if hosts:
        hosts[0].cmd('pkill -9 -f iperf3 2>/dev/null')
        time.sleep(0.3)
    for h in hosts:
        h.cmd('iperf3 -s -D --logfile /dev/null')
    time.sleep(0.5)

def srv_stop(*hosts):
    for h in hosts:
        h.cmd('pkill -f iperf3')

def cleanup_all(net):
    """Hentikan semua proses testing di semua host"""
    for name in HOST_IPS:
        h = net.get(name)
        h.cmd('pkill -f iperf3; pkill -f tcpdump; pkill -f ping; pkill -f hping3')

def push_proactive_spine_leaf_calcium():
    print("[*] Mengirimkan proaktif flow rules spesifik ke ODL Calcium (RFC 8040)...")
    headers = {"Content-Type": "application/json"}
    auth = HTTPBasicAuth('admin', 'admin')
    
    # URL template standar ODL Calcium
    url_template = "http://10.10.20.230:8181/rests/data/opendaylight-inventory:nodes/node=openflow:{}/flow-node-inventory:table=0/flow={}"
    
    # Helper untuk membuat struktur JSON pencocokan MAC Address tujuan
    def make_mac_payload(flow_id, mac_dst, out_port):
        return {
            "flow-node-inventory:flow": [{
                "id": str(flow_id),
                "table_id": 0,
                "priority": 500,
                "match": {
                    "ethernet-match": {
                        "ethernet-destination": {"address": mac_dst}
                    }
                },
                "instructions": {
                    "instruction": [{
                        "order": 0,
                        "apply-actions": {
                            "action": [{
                                "order": 0,
                                "output-action": {"output-node-connector": str(out_port)}
                            }]
                        }
                    }]
                }
            }]
        }

    # Helper untuk mengalirkan paket ARP agar tidak berputar-putar (Loop)
    def make_arp_payload(flow_id, out_port):
        return {
            "flow-node-inventory:flow": [{
                "id": str(flow_id),
                "table_id": 0,
                "priority": 400,
                "match": {
                    "ethernet-match": {
                        "ethernet-type": {"type": 2054} # EtherType 2054 = ARP
                    }
                },
                "instructions": {
                    "instruction": [{
                        "order": 0,
                        "apply-actions": {
                            "action": [{
                                "order": 0,
                                "output-action": {"output-node-connector": str(out_port)}
                            }]
                        }
                    }]
                }
            }]
        }

    # ─── 1. KONFIGURASI PADA SPINE 1 (DPID Desimal: 1) ───
    # s1 terhubung ke l1(port 1), l2(port 2), l3(port 3), l4(port 4)
    spine1_id = 1
    mac_to_port_s1 = {
        "00:00:00:00:00:01": 1, "00:00:00:00:00:02": 1, # Jalur ke l1
        "00:00:00:00:00:03": 2, "00:00:00:00:00:04": 2, # Jalur ke l2
        "00:00:00:00:00:05": 3, "00:00:00:00:00:06": 3, # Jalur ke l3
        "00:00:00:00:00:07": 4, "00:00:00:00:00:08": 4, # Jalur ke l4
    }
    f_id = 1
    for mac, port in mac_to_port_s1.items():
        url = url_template.format(spine1_id, f_id)
        requests.put(url, json=make_mac_payload(f_id, mac, port), headers=headers, auth=auth)
        f_id += 1

    # ─── 2. KONFIGURASI PADA TIAP LEAF SWITCH (DPID Desimal: 17, 18, 19, 20) ───
    # Port 1 mengarah ke s1. Port 3 & 4 mengarah ke Host lokal masing-masing.
    leaf_configs = {
        17: {"local": {"00:00:00:00:00:01": 3, "00:00:00:00:00:02": 4}, "name": "l1"},
        18: {"local": {"00:00:00:00:00:03": 3, "00:00:00:00:00:04": 4}, "name": "l2"},
        19: {"local": {"00:00:00:00:00:05": 3, "00:00:00:00:00:06": 4}, "name": "l3"},
        20: {"local": {"00:00:00:00:00:07": 3, "00:00:00:00:00:08": 4}, "name": "l4"},
    }
    all_macs = [f"00:00:00:00:00:0{i}" for i in range(1, 9)]

    for dpid, cfg in leaf_configs.items():
        flow_id_leaf = 100
        for mac in all_macs:
            url = url_template.format(dpid, flow_id_leaf)
            # Jika MAC tujuan ada di port lokal, keluarkan ke host. Jika tidak, lempar ke Spine 1 (Port 1)
            out_port = cfg["local"].get(mac, 1)
            requests.put(url, json=make_mac_payload(flow_id_leaf, mac, out_port), headers=headers, auth=auth)
            flow_id_leaf += 1
        
        # Amankan jalur paket ARP khusus lewat Port 1 (ke Spine 1) agar tidak macet
        url_arp = url_template.format(dpid, 999)
        requests.put(url_arp, json=make_arp_payload(999, 1), headers=headers, auth=auth)
        print(f"    [✓] Flow murni proaktif sukses di-push ke {cfg['name']} (ID: openflow:{dpid})")
# ─────────────────────────────────────────────────────────────
#  SKENARIO 1 — BASELINE (Single TCP flow h1 → h5)
# ─────────────────────────────────────────────────────────────
def skenario_1_baseline(net):
    """
    Satu aliran TCP tunggal dari h1 ke h5 tanpa interferensi
    trafik lain. Berfungsi sebagai titik referensi kuantitatif
    untuk seluruh metrik QoS.

    Pengukuran:
      - Throughput & Goodput & Retransmission : iperf3 TCP
      - Jitter & Packet Loss                 : iperf3 UDP
      - RTT & Delay                          : ping
      - CET                                  : hping3
      - Pcap (Out-of-Order, Duplicate,
        Window Behavior, Overhead)           : tcpdump → Wireshark
    """
    global _cur_dir
    _cur_dir = f'{OUTDIR}/s1_baseline'
    mkdir(_cur_dir)

    info('\n' + '═'*60 + '\n')
    info(' SKENARIO 1: Baseline — Single flow h1 → h5\n')
    info('═'*60 + '\n')

    h1 = net.get('h1')
    h5 = net.get('h5')

    # Mulai capture
    cap_start(h1)
    cap_start(h5)

    # ── iperf3 TCP: Throughput, Goodput, Retransmission ──────
    info(' > [1/4] iperf3 TCP (Throughput / Goodput / Retransmission)...\n')
    srv_start(h5)
    out = h1.cmd(f'iperf3 -c 10.0.0.5 -t {DURATION} -i 1 -f m 2>&1')
    save(f'{_cur_dir}/iperf3_tcp.txt', out)
    srv_stop(h5)
    time.sleep(1)

    # ── iperf3 UDP: Jitter, Packet Loss ──────────────────────
    info(' > [2/4] iperf3 UDP (Jitter / Packet Loss)...\n')
    srv_start(h5)
    out = h1.cmd(f'iperf3 -c 10.0.0.5 -u -b 100M -t {DURATION} 2>&1')
    save(f'{_cur_dir}/iperf3_udp.txt', out)
    srv_stop(h5)
    time.sleep(1)

    # ── ping: RTT, Delay, Packet Loss ────────────────────────
    info(' > [3/4] ping (RTT / Delay / Packet Loss)...\n')
    out = h1.cmd('ping -c 500 -i 0.05 10.0.0.5 2>&1')
    save(f'{_cur_dir}/ping_rtt.txt', out)

    # ── hping3: Connection Establishment Time ────────────────
    info(' > [4/4] hping3 (Connection Establishment Time)...\n')
    out = h1.cmd('hping3 -S -p 5201 -c 50 10.0.0.5 2>&1')
    save(f'{_cur_dir}/hping3_cet.txt', out)

    cap_stop(h1, h5)
    srv_stop(h1, h5)

    info(f' Selesai. Hasil disimpan di: {_cur_dir}/\n')
    info(f'   - iperf3_tcp.txt  → Throughput, Goodput, Retransmission\n')
    info(f'   - iperf3_udp.txt  → Jitter, Packet Loss\n')
    info(f'   - ping_rtt.txt    → RTT, Delay\n')
    info(f'   - hping3_cet.txt  → Connection Establishment Time\n')
    info(f'   - h1.pcap, h5.pcap → buka di Wireshark untuk:\n')
    info(f'       Out-of-Order, Duplicate Packets, Window Behavior,\n')
    info(f'       Protocol Overhead, RTT graph, Window Scaling graph\n')


# ─────────────────────────────────────────────────────────────
#  SKENARIO 2 — INCAST (4 sender → h5)
# ─────────────────────────────────────────────────────────────
def skenario_2_incast(net):
    """
    Empat sender (h1, h2, h3, h4) mengirimkan data ke satu
    receiver (h5) secara bersamaan — mensimulasikan fenomena
    TCP Incast Congestion khas data center.

    Setiap sender terhubung ke port server iperf3 yang berbeda
    (5201–5204) agar keempat koneksi benar-benar simultan dan
    bottleneck yang terukur adalah link akses h5 (100 Mbps),
    bukan keterbatasan aplikasi iperf3.

    Pengukuran:
      - Throughput & Goodput & Retransmission : iperf3 TCP (per sender)
      - Jitter & Packet Loss                 : iperf3 UDP (h1 → h5)
      - RTT & Delay                          : ping (h1 → h5)
      - CET                                  : hping3 (h1 → h5)
      - Pcap sisi receiver                   : tcpdump di h5 → Wireshark
    """
    global _cur_dir
    _cur_dir = f'{OUTDIR}/s2_incast'
    mkdir(_cur_dir)

    info('\n' + '═'*60 + '\n')
    info(' SKENARIO 2: Incast — h1, h2, h3, h4 → h5 sekaligus\n')
    info('═'*60 + '\n')

    senders = [net.get(f'h{i}') for i in range(1, 5)]
    h1      = net.get('h1')
    h5      = net.get('h5')

    # Port server terpisah per sender (5201–5204)
    ports = {1: 5201, 2: 5202, 3: 5203, 4: 5204}

    # Mulai capture di semua sender + receiver
    for h in senders + [h5]:
        cap_start(h)

    # Start 4 instance iperf3 server di h5, port berbeda
    h5.cmd('pkill -9 -f iperf3 2>/dev/null')
    time.sleep(0.3)
    for i in range(1, 5):
        h5.cmd(f'iperf3 -s -p {ports[i]} -D --logfile /dev/null')
    time.sleep(0.5)

    # ── iperf3 TCP: 4 sender bersamaan ───────────────────────
    info(' > [1/4] iperf3 TCP — 4 sender → h5 sekaligus (Incast)...\n')
    for i, h in enumerate(senders, start=1):
        h.cmd(
            f'iperf3 -c 10.0.0.5 -p {ports[i]} -t {DURATION} -i 1 -f m '
            f'> {_cur_dir}/iperf3_tcp_{h.name}.txt 2>&1 &'
        )

    time.sleep(DURATION + 5)
    h5.cmd('pkill -9 -f iperf3 2>/dev/null')
    time.sleep(1)

    # ── iperf3 UDP: Jitter & Packet Loss (h1 → h5) ───────────
    info(' > [2/4] iperf3 UDP (Jitter / Packet Loss — h1 → h5)...\n')
    srv_start(h5)
    out = h1.cmd(f'iperf3 -c 10.0.0.5 -u -b 100M -t {DURATION} 2>&1')
    save(f'{_cur_dir}/iperf3_udp_h1.txt', out)
    srv_stop(h5)
    time.sleep(1)

    # ── ping: RTT & Delay (h1 → h5) ──────────────────────────
    info(' > [3/4] ping (RTT / Delay — h1 → h5)...\n')
    out = h1.cmd('ping -c 500 -i 0.05 10.0.0.5 2>&1')
    save(f'{_cur_dir}/ping_rtt_h1.txt', out)

    # ── hping3: CET (h1 → h5) ────────────────────────────────
    info(' > [4/4] hping3 (Connection Establishment Time — h1 → h5)...\n')
    out = h1.cmd('hping3 -S -p 5201 -c 50 10.0.0.5 2>&1')
    save(f'{_cur_dir}/hping3_cet_h1.txt', out)

    for h in senders + [h5]:
        cap_stop(h)
    srv_stop(h5)

    info(f' Selesai. Hasil disimpan di: {_cur_dir}/\n')
    info(f'   - iperf3_tcp_h1.txt ... iperf3_tcp_h4.txt → Throughput per sender\n')
    info(f'   - iperf3_udp_h1.txt   → Jitter, Packet Loss\n')
    info(f'   - ping_rtt_h1.txt     → RTT, Delay\n')
    info(f'   - hping3_cet_h1.txt   → Connection Establishment Time\n')
    info(f'   - h1.pcap ... h4.pcap → Window Behavior per sender (Wireshark)\n')
    info(f'   - h5.pcap             → Out-of-Order, Duplicate, Overhead (Wireshark)\n')


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
_cur_dir = OUTDIR

def main():
    global _cur_dir
    setLogLevel('info')
    mkdir(OUTDIR)

    info('\n╔' + '═'*58 + '╗\n')
    info('║  Spine-Leaf QoS TCP Testing — ONOS SDN' + ' '*18 + '║\n')
    info(f'║  Output : {OUTDIR:<47}║\n')
    info(f'║  Durasi : {DURATION}s per skenario' + ' '*39 + '║\n')
    info('╚' + '═'*58 + '╝\n')

    # Bangun topologi
    net = build_topology()
    net.start()
    time.sleep(15)
    
    push_proactive_spine_leaf_calcium()
    info('\n[*] Topologi dimulai. Menunggu ONOS mendeteksi switch (20 detik)...\n')
    time.sleep(20)

    # Pingall dua kali untuk host discovery
    info('[*] pingall ke-1 (host discovery)...\n')
    net.pingAll(timeout=3)
    time.sleep(5)
    info('[*] pingall ke-2 (verifikasi konektivitas)...\n')
    loss = net.pingAll(timeout=3)
    info(f'[*] Packet loss pingall: {loss:.0f}%\n')

    if loss > 20:
        info('[!] Konektivitas belum optimal.\n')
        info('[!] Periksa ONOS (app openflow, fwd, hostprovider aktif?)\n')
        info('[!] Tekan Enter untuk lanjut, atau Ctrl+C untuk batal.\n')
        try:
            input()
        except KeyboardInterrupt:
            net.stop()
            return

    try:
        skenario_1_baseline(net)
        skenario_2_incast(net)

    except KeyboardInterrupt:
        info('\n[!] Testing dihentikan.\n')
    finally:
        cleanup_all(net)

    info('\n╔' + '═'*58 + '╗\n')
    info('║  SEMUA SKENARIO SELESAI' + ' '*34 + '║\n')
    info(f'║  Folder hasil : {OUTDIR:<42}║\n')
    info('╠' + '═'*58 + '╣\n')
    info('║  Langkah selanjutnya:' + ' '*36 + '║\n')
    info('║  1. Buka file .pcap di Wireshark' + ' '*25 + '║\n')
    info('║  2. Baca iperf3_tcp.txt untuk Throughput/Goodput/Retr' + ' '*4 + '║\n')
    info('║  3. Baca iperf3_udp.txt untuk Jitter/Packet Loss' + ' '*8 + '║\n')
    info('║  4. Baca ping_rtt.txt untuk RTT/Delay' + ' '*19 + '║\n')
    info('║  5. Baca hping3_cet.txt untuk CET' + ' '*23 + '║\n')
    info('╚' + '═'*58 + '╝\n')

    info('\n[*] Mininet CLI aktif. Ketik "exit" untuk selesai.\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    main()
