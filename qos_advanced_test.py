#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   Advanced Multi-Scenario QoS TCP Testing                   ║
║   Topologi  : 2 Spine × 4 Leaf × 8 Host (Spine-Leaf)       ║
║   Controller: ONOS via Docker                               ║
║   Skenario  : 5 skenario otomatis dengan 8 host aktif       ║
╚══════════════════════════════════════════════════════════════╝

Cara pakai:
    sudo python3 qos_advanced_test.py

Pastikan sebelumnya:
    docker run -d --name onos -p 8181:8181 -p 8101:8101 -p 6653:6653 onosproject/onos:2.7.0
    (aktivasi app: openflow, fwd, hostprovider via ONOS CLI)
"""

import os
import re
import time
import subprocess
import csv
from datetime import datetime
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info, warn
from mininet.link import TCLink

# ══════════════════════════════════════════════════════════════
#  KONFIGURASI — ubah sesuai kebutuhan
# ══════════════════════════════════════════════════════════════
CONTROLLER_IP   = '127.0.0.1'
CONTROLLER_PORT = 6653

OUTDIR = f'/tmp/qos_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

# Durasi per skenario (detik)
DURATION_BASELINE   = 30
DURATION_CONCURRENT = 30
DURATION_INCAST     = 30
DURATION_NOISE      = 30
DURATION_DITG       = 20   # per rate level

# Parameter link
SPINE_LEAF_BW     = 1000   # Mbps — uplink kapasitas tinggi
LEAF_HOST_BW      = 100    # Mbps — bottleneck (access link)
SPINE_LEAF_DELAY  = '1ms'
LEAF_HOST_DELAY   = '0.5ms'
QUEUE_SIZE        = 200    # paket

# D-ITG rate levels (pkt/s) untuk Skenario 5 — single-pair ramp-up.
# Dengan ukuran paket DITG_PACKET_SIZE bytes, rate ~24.400 pps secara
# teori sudah menyentuh kapasitas link akses 100Mbps; rate di atas itu
# sengaja dimasukkan untuk benar-benar memaksa kondisi overload.
DITG_RATES = [5000, 10000, 20000, 30000, 45000, 65000, 90000]
DITG_PACKET_SIZE = 512   # bytes — payload per paket D-ITG

# UDP bandwidth untuk test jitter/loss
UDP_BW = '80M'

# Mapping host → IP
HOST_IPS = {
    'h1':'10.0.0.1','h2':'10.0.0.2',
    'h3':'10.0.0.3','h4':'10.0.0.4',
    'h5':'10.0.0.5','h6':'10.0.0.6',
    'h7':'10.0.0.7','h8':'10.0.0.8',
}
# ══════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────
#  TOPOLOGI
# ─────────────────────────────────────────────────────────────
def build_topology():
    """
    Membuat topologi Spine-Leaf:
      ┌──────────┐   ┌──────────┐
      │  Spine 1 │   │  Spine 2 │   ← DPID 0x01, 0x02
      └────┬─────┘   └────┬─────┘
     ┌─────┼──┬──────┬────┼──┐
    [L1] [L2] [L3] [L4]       ← DPID 0x11–0x14
    h1,h2 h3,h4 h5,h6 h7,h8  ← 100Mbps access links
    """
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=False
    )

    net.addController('c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)

    # Spine — DPID eksplisit (hindari konflik dengan Leaf)
    sp1 = net.addSwitch('s1', protocols='OpenFlow13', dpid='0000000000000001')
    sp2 = net.addSwitch('s2', protocols='OpenFlow13', dpid='0000000000000002')

    # Leaf — DPID 0x11–0x14 (tidak bertabrakan dengan Spine)
    lf1 = net.addSwitch('l1', protocols='OpenFlow13', dpid='0000000000000011')
    lf2 = net.addSwitch('l2', protocols='OpenFlow13', dpid='0000000000000012')
    lf3 = net.addSwitch('l3', protocols='OpenFlow13', dpid='0000000000000013')
    lf4 = net.addSwitch('l4', protocols='OpenFlow13', dpid='0000000000000014')

    # Host
    for name, ip in HOST_IPS.items():
        idx = int(name[1])
        net.addHost(name, ip=f'{ip}/24', mac=f'00:00:00:00:00:0{idx}')

    # Link Spine ↔ Leaf: 1Gbps, 1ms — full mesh ECMP
    # use_htb default True; warning "quantum is big" murni kosmetik dari tc/htb,
    # tidak memengaruhi shaping bandwidth — aman diabaikan.
    sl_opts = dict(bw=SPINE_LEAF_BW, delay=SPINE_LEAF_DELAY, max_queue_size=QUEUE_SIZE)
    for spine in [sp1, sp2]:
        for leaf in [lf1, lf2, lf3, lf4]:
            net.addLink(spine, leaf, **sl_opts)

    # Link Leaf ↔ Host: 100Mbps, 0.5ms — bottleneck
    lh_opts = dict(bw=LEAF_HOST_BW, delay=LEAF_HOST_DELAY, max_queue_size=QUEUE_SIZE)
    for leaf, hosts in [(lf1,['h1','h2']), (lf2,['h3','h4']),
                        (lf3,['h5','h6']), (lf4,['h7','h8'])]:
        for hname in hosts:
            net.addLink(leaf, net.get(hname), **lh_opts)

    return net


# ─────────────────────────────────────────────────────────────
#  HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────
def mkdir(path):
    os.makedirs(path, exist_ok=True)

def cap_start(host):
    """Mulai tcpdump di host (background)"""
    iface = f'{host.name}-eth0'
    out   = f'{_cur_dir}/{host.name}.pcap'
    host.cmd(f'pkill -f "tcpdump.*{iface}"; tcpdump -i {iface} -w {out} &')
    time.sleep(0.3)

def cap_stop(*hosts):
    """Hentikan tcpdump di sejumlah host"""
    for h in hosts:
        h.cmd('pkill -f tcpdump')
    time.sleep(0.8)

def srv_start(*hosts):
    """
    Start iperf3 server (daemon) di sejumlah host.

    PENTING: pkill di Mininet bersifat GLOBAL (semua host berbagi PID
    namespace yang sama dengan root, bukan terisolasi per-host). Maka
    pkill HANYA dipanggil SATU KALI sebelum loop start, bukan di
    setiap iterasi — kalau dipanggil per-host, server yang baru saja
    distart di host sebelumnya akan ikut terbunuh (ini adalah bug
    yang menyebabkan data S2/S4 kosong di versi sebelumnya).
    """
    if hosts:
        hosts[0].cmd('pkill -9 -f iperf3 2>/dev/null')
        time.sleep(0.3)
    for h in hosts:
        h.cmd('iperf3 -s -D --logfile /dev/null')
    time.sleep(0.5)

def srv_stop(*hosts):
    """Stop semua server & sender di host"""
    for h in hosts:
        h.cmd('pkill -f iperf3; pkill -f ITGSend; pkill -f ITGRecv')

def win_start(host, dst_ip, outfile):
    """Monitor cwnd/rtt setiap 0.5 detik ke file (background)"""
    host.cmd(
        f'bash -c "while true; do '
        f'ss -tin dst {dst_ip} 2>/dev/null | grep -E \'cwnd|rtt\'; '
        f'sleep 0.5; done >> {outfile}" &'
    )

def win_stop(*hosts):
    for h in hosts:
        h.cmd('pkill -f "ss -tin"')

def hping_cet(host, dst_ip, outfile, count=30):
    """Ukur Connection Establishment Time via hping3 SYN"""
    result = host.cmd(f'hping3 -S -p 5201 -c {count} --fast {dst_ip} 2>&1')
    with open(outfile, 'w') as f:
        f.write(result)


# ─────────────────────────────────────────────────────────────
#  ANALISIS PCAP (dijalankan setelah capture selesai)
# ─────────────────────────────────────────────────────────────
def tshark_count(pcap, yfilter):
    """Hitung baris output tshark sesuai filter"""
    try:
        r = subprocess.run(
            ['tshark', '-r', pcap, '-Y', yfilter, '-q'],
            capture_output=True, text=True
        )
        return len([l for l in r.stdout.splitlines() if l.strip()])
    except Exception:
        return -1

def analyze_pcap(pcap):
    """Ekstrak semua metrik dari file pcap"""
    if not os.path.exists(pcap) or os.path.getsize(pcap) < 200:
        return {}
    m = {}

    # Out-of-Order, Duplicate ACK, Retransmission
    m['out_of_order']    = tshark_count(pcap, 'tcp.analysis.out_of_order')
    m['duplicate_ack']   = tshark_count(pcap, 'tcp.analysis.duplicate_ack')
    m['retransmission']  = tshark_count(pcap, 'tcp.analysis.retransmission or tcp.analysis.fast_retransmission')

    # Protocol Overhead
    try:
        r = subprocess.run(
            ['tshark', '-r', pcap, '-Y', 'tcp and tcp.len > 0',
             '-T', 'fields', '-e', 'frame.len', '-e', 'tcp.len'],
            capture_output=True, text=True
        )
        total_f = total_p = 0
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    total_f += int(parts[0]); total_p += int(parts[1])
                except ValueError:
                    pass
        if total_f > 0:
            m['overhead_pct'] = round((1 - total_p / total_f) * 100, 2)
            m['efficiency_pct'] = round((total_p / total_f) * 100, 2)
    except Exception:
        pass

    # Connection Establishment Time
    try:
        def get_times(yf):
            r = subprocess.run(
                ['tshark', '-r', pcap, '-Y', yf, '-T', 'fields', '-e', 'frame.time_epoch'],
                capture_output=True, text=True
            )
            return [float(x) for x in r.stdout.splitlines() if x.strip()]

        syns    = get_times('tcp.flags.syn==1 and tcp.flags.ack==0')
        synacks = get_times('tcp.flags.syn==1 and tcp.flags.ack==1')
        if syns and synacks:
            pairs = min(len(syns), len(synacks))
            cets  = [(synacks[i] - syns[i]) * 1000 for i in range(pairs)]
            m['cet_avg_ms'] = round(sum(cets) / len(cets), 3)
            m['cet_min_ms'] = round(min(cets), 3)
            m['cet_max_ms'] = round(max(cets), 3)
    except Exception:
        pass

    return m


def parse_iperf_tcp(fpath):
    """
    Parse iperf3 TCP output → Throughput, Goodput, Retransmission.
    Jika parsing gagal (koneksi error, file kosong, dll), field 'error'
    diisi dengan cuplikan log mentah agar mudah didiagnosis — bukan
    diam-diam dikembalikan kosong seperti sebelumnya.
    """
    if not os.path.exists(fpath):
        return {'error': 'file output tidak ditemukan'}
    with open(fpath) as f:
        content = f.read()
    if not content.strip():
        return {'error': 'file output kosong (iperf3 tidak menghasilkan apapun)'}
    m = {}
    for line in content.splitlines():
        parts = line.split()
        if 'sender' in line:
            for i, p in enumerate(parts):
                if 'Mbits' in p and i > 0:
                    try: m['throughput_mbps'] = float(parts[i-1])
                    except ValueError: pass
                if p == 'Retr' and i + 1 < len(parts):
                    try: m['retr_segs'] = int(parts[i+1])
                    except (ValueError, IndexError): pass
        elif 'receiver' in line:
            for i, p in enumerate(parts):
                if 'Mbits' in p and i > 0:
                    try: m['goodput_mbps'] = float(parts[i-1])
                    except ValueError: pass
    if 'throughput_mbps' in m and 'goodput_mbps' in m and m['throughput_mbps'] > 0:
        m['goodput_pct'] = round(m['goodput_mbps'] / m['throughput_mbps'] * 100, 1)
    if not m:
        first_line = content.strip().splitlines()[0][:150]
        m['error'] = f'tidak ada baris sender/receiver — kemungkinan koneksi gagal. Log: "{first_line}"'
    return m


def parse_iperf_udp(fpath):
    """Parse iperf3 UDP output → Jitter, Packet Loss"""
    if not os.path.exists(fpath):
        return {}
    m = {}
    with open(fpath) as f:
        for line in f:
            if 'ms' in line and ('%' in line or 'receiver' in line):
                parts = line.split()
                for i, p in enumerate(parts):
                    if p.endswith('ms'):
                        try: m['jitter_ms'] = float(p.replace('ms',''))
                        except ValueError: pass
                    if '(' in p and '%' in p:
                        try: m['pkt_loss_pct'] = float(p.strip('()%'))
                        except ValueError: pass
    return m


def parse_ping(fpath):
    """Parse ping output → RTT avg, loss"""
    if not os.path.exists(fpath):
        return {}
    m = {}
    with open(fpath) as f:
        for line in f:
            if 'rtt min' in line or 'round-trip' in line:
                try:
                    vals = line.split('=')[1].strip().split('/')
                    m['rtt_min_ms']  = float(vals[0])
                    m['rtt_avg_ms']  = float(vals[1])
                    m['rtt_max_ms']  = float(vals[2])
                    m['rtt_mdev_ms'] = float(vals[3].split()[0])
                except (IndexError, ValueError):
                    pass
            if 'packet loss' in line:
                for seg in line.split(','):
                    if 'packet loss' in seg:
                        try:
                            pct = ''.join(c for c in seg if c.isdigit() or c == '.')
                            m['ping_loss_pct'] = float(pct)
                        except ValueError:
                            pass
    return m


def parse_hping(fpath):
    """Parse hping3 output → CET dari RTT SYN"""
    if not os.path.exists(fpath):
        return {}
    m = {}
    with open(fpath) as f:
        for line in f:
            if 'round-trip' in line:
                try:
                    vals = line.split('=')[1].strip().split('/')
                    m['cet_hping_avg_ms'] = float(vals[1])
                    m['cet_hping_min_ms'] = float(vals[0])
                    m['cet_hping_max_ms'] = float(vals[2])
                except (IndexError, ValueError):
                    pass
    return m


def parse_ditg_raw_packets(content):
    """
    Fallback parser untuk format ITGDec versi PER-PAKET (bukan ringkasan
    teks "Average bitrate" dll). Beberapa build D-ITG (termasuk yang
    terpasang di Ubuntu 22.04 milikmu) menghasilkan listing per baris:
        <seq> <dep_H> <dep_M> <dep_S.micro> <arr_H> <arr_M> <arr_S.micro> <size>
    Delay, jitter, throughput, dan total paket dihitung langsung dari
    data mentah ini — bukan menebak flag CLI yang belum tentu ada di
    versi D-ITG yang terpasang.
    """
    delays = []
    sizes = []
    timestamps = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            dep_t = float(parts[1]) * 3600 + float(parts[2]) * 60 + float(parts[3])
            arr_t = float(parts[4]) * 3600 + float(parts[5]) * 60 + float(parts[6])
            size  = float(parts[7])
        except ValueError:
            continue
        delay = arr_t - dep_t
        if 0 <= delay < 5:  # buang baris aneh/lintas tengah malam
            delays.append(delay)
            sizes.append(size)
            timestamps.append(arr_t)

    if not delays:
        return {}

    m = {'ditg_total_packets': len(delays)}
    m['ditg_delay_avg_ms'] = round(sum(delays) / len(delays) * 1000, 4)
    m['ditg_delay_min_ms'] = round(min(delays) * 1000, 4)
    m['ditg_delay_max_ms'] = round(max(delays) * 1000, 4)
    if len(delays) > 1:
        mean_d = sum(delays) / len(delays)
        variance = sum((d - mean_d) ** 2 for d in delays) / len(delays)
        m['ditg_jitter_ms'] = round((variance ** 0.5) * 1000, 4)
    if len(timestamps) > 1:
        duration = max(timestamps) - min(timestamps)
        if duration > 0:
            m['ditg_bitrate_mbps'] = round(sum(sizes) * 8 / duration / 1_000_000, 3)
    return m


def parse_ditg_decode(fpath):
    """
    Parse hasil 'ITGDec' → goodput, delay, jitter, packet loss.
    Fungsi ini sebelumnya TIDAK ADA — ITGDec sudah dijalankan dan
    hasilnya tersimpan di file, tapi tidak pernah dibaca ke laporan
    akhir (itu sebabnya S5 di laporan sebelumnya hanya berisi RTT
    ping, tanpa metrik D-ITG sama sekali).

    Format output ITGDec berbeda antar versi/build. Parser ini mencoba
    dua kemungkinan secara berurutan:
      1) format ringkasan teks ("Average bitrate", "Average delay", dst)
      2) fallback ke format per-paket mentah (lihat parse_ditg_raw_packets)
    Jika keduanya tidak cocok, cuplikan mentah disertakan di
    'raw_snippet' supaya tetap bisa diperiksa manual.
    """
    if not os.path.exists(fpath):
        return {'error': 'file ITGDec tidak ditemukan'}
    with open(fpath) as f:
        content = f.read()
    if not content.strip():
        return {'error': 'file ITGDec kosong'}

    m = {}
    patterns = {
        'ditg_bitrate_mbps':    r'(?:Average\s+bitrate|Bitrate)\D*([\d.]+)',
        'ditg_delay_s':         r'(?:Average\s+delay)\D*([\d.]+)',
        'ditg_jitter_s':        r'(?:Average\s+jitter)\D*([\d.]+)',
        'ditg_packet_loss_pct': r'(?:Average\s+packet\s+loss|Packet\s+loss)\D*([\d.]+)\s*%?',
        'ditg_total_packets':   r'Total\s+packets\D*(\d+)',
        'ditg_packets_dropped': r'(?:Packets\s+dropped|Dropped\s+packets)\D*(\d+)',
    }
    for key, pat in patterns.items():
        match = re.search(pat, content, re.IGNORECASE)
        if match:
            try:
                m[key] = float(match.group(1))
            except ValueError:
                pass

    if not m:
        # Format ringkasan tidak cocok — coba format per-paket mentah
        m = parse_ditg_raw_packets(content)

    if not m:
        m['error'] = 'format ITGDec tidak dikenali parser, cek raw_snippet'
        m['raw_snippet'] = content[:300].replace('\n', ' | ')

    return m


# ─────────────────────────────────────────────────────────────
#  SKENARIO 1 — Baseline: Single Flow h1 → h5
# ─────────────────────────────────────────────────────────────
def skenario_1_baseline(net):
    """
    Flow tunggal h1 → h5. Baseline semua metrik.
    Mencakup: Throughput, Goodput, Retransmission, Jitter,
    Packet Loss, Delay, RTT, CET, Window Behavior, Overhead.
    """
    global _cur_dir
    _cur_dir = f'{OUTDIR}/s1_baseline'
    mkdir(_cur_dir)
    info('\n' + '═'*60 + '\n')
    info(' SKENARIO 1: Baseline — Single flow h1 → h5\n')
    info('═'*60 + '\n')

    h1 = net.get('h1'); h5 = net.get('h5')

    # === Capture ===
    cap_start(h1); cap_start(h5)

    # === iperf3 TCP: Throughput, Goodput, Retransmission ===
    info(' > [TCP] Throughput / Goodput / Retransmission...\n')
    srv_start(h5)
    win_start(h1, '10.0.0.5', f'{_cur_dir}/window_h1.txt')
    tcp = h1.cmd(f'iperf3 -c 10.0.0.5 -t {DURATION_BASELINE} -i 1 -f m 2>&1')
    win_stop(h1)
    with open(f'{_cur_dir}/iperf3_tcp.txt','w') as f: f.write(tcp)
    srv_stop(h5)

    # === iperf3 UDP: Jitter, Packet Loss ===
    info(' > [UDP] Jitter / Packet Loss...\n')
    srv_start(h5)
    udp = h1.cmd(f'iperf3 -c 10.0.0.5 -u -b {UDP_BW} -t 20 2>&1')
    with open(f'{_cur_dir}/iperf3_udp.txt','w') as f: f.write(udp)
    srv_stop(h5)

    # === Ping: RTT, Delay, Latency ===
    info(' > [PING] RTT / Delay / Latency...\n')
    ping = h1.cmd('ping -c 500 -i 0.05 10.0.0.5 2>&1')
    with open(f'{_cur_dir}/ping_rtt.txt','w') as f: f.write(ping)

    # === hping3: Connection Establishment Time ===
    info(' > [HPING3] Connection Establishment Time...\n')
    hping_cet(h1, '10.0.0.5', f'{_cur_dir}/hping_cet.txt')

    cap_stop(h1, h5)
    srv_stop(h1, h5)

    # === Analisis ===
    r = {}
    r.update(parse_iperf_tcp(f'{_cur_dir}/iperf3_tcp.txt'))
    r.update(parse_iperf_udp(f'{_cur_dir}/iperf3_udp.txt'))
    r.update(parse_ping(f'{_cur_dir}/ping_rtt.txt'))
    r.update(parse_hping(f'{_cur_dir}/hping_cet.txt'))
    r.update(analyze_pcap(f'{_cur_dir}/h1.pcap'))
    info(f' Selesai. Data: {_cur_dir}/\n')
    return r


# ─────────────────────────────────────────────────────────────
#  SKENARIO 2 — Concurrent 4-pair (semua Leaf aktif sekaligus)
# ─────────────────────────────────────────────────────────────
def skenario_2_concurrent(net):
    """
    4 TCP flow bersamaan melewati kedua Spine:
      h1→h5  (Leaf1 → Spine1/2 → Leaf3)
      h2→h6  (Leaf1 → Spine1/2 → Leaf3)
      h3→h7  (Leaf2 → Spine1/2 → Leaf4)
      h4→h8  (Leaf2 → Spine1/2 → Leaf4)
    Menekan Spine switches. Mengukur degradasi throughput,
    fairness, dan RTT under load pada semua pair.
    """
    global _cur_dir
    _cur_dir = f'{OUTDIR}/s2_concurrent_4pair'
    mkdir(_cur_dir)
    info('\n' + '═'*60 + '\n')
    info(' SKENARIO 2: Concurrent 4-pair — semua Spine dipakai\n')
    info('═'*60 + '\n')

    pairs = [('h1','h5','10.0.0.5'), ('h2','h6','10.0.0.6'),
             ('h3','h7','10.0.0.7'), ('h4','h8','10.0.0.8')]

    clients = [net.get(p[0]) for p in pairs]
    servers = [net.get(p[1]) for p in pairs]

    # Capture di semua 8 host
    for h in clients + servers:
        cap_start(h)

    # Start semua server
    srv_start(*servers)

    # Window monitoring di tiap client
    for cl, _, sip in pairs:
        win_start(net.get(cl), sip, f'{_cur_dir}/window_{cl}.txt')

    # Jalankan semua client bersamaan (non-blocking)
    info(' > Starting 4 concurrent flows...\n')
    for cl, sv, sip in pairs:
        h = net.get(cl)
        h.cmd(f'iperf3 -c {sip} -t {DURATION_CONCURRENT} -i 1 -f m '
              f'> {_cur_dir}/{cl}_to_{sv}.txt 2>&1 &')

    # Ping dari h1 ke h5 selama concurrent load
    net.get('h1').cmd(
        f'ping -c {DURATION_CONCURRENT * 5} -i 0.2 10.0.0.5 '
        f'> {_cur_dir}/h1_rtt_under_load.txt 2>&1 &'
    )
    # Ping dari h3 ke h7 (pair berbeda)
    net.get('h3').cmd(
        f'ping -c {DURATION_CONCURRENT * 5} -i 0.2 10.0.0.7 '
        f'> {_cur_dir}/h3_rtt_under_load.txt 2>&1 &'
    )

    info(f' > Menunggu {DURATION_CONCURRENT + 5}s...\n')
    time.sleep(DURATION_CONCURRENT + 5)

    win_stop(*clients)
    cap_stop(*clients + servers)
    srv_stop(*servers)

    # Analisis per pair + gabungan
    results = {}
    total_tp = 0
    for cl, sv, _ in pairs:
        r = parse_iperf_tcp(f'{_cur_dir}/{cl}_to_{sv}.txt')
        results[f'{cl}→{sv}'] = r
        if 'throughput_mbps' in r:
            total_tp += r['throughput_mbps']

    results['total_throughput_all_pairs'] = round(total_tp, 2)
    results['rtt_h1_under_load'] = parse_ping(f'{_cur_dir}/h1_rtt_under_load.txt')
    results['rtt_h3_under_load'] = parse_ping(f'{_cur_dir}/h3_rtt_under_load.txt')
    # Pcap h1 untuk out-of-order / overhead
    results['pcap_h1'] = analyze_pcap(f'{_cur_dir}/h1.pcap')
    info(f' Selesai. Data: {_cur_dir}/\n')
    return results


# ─────────────────────────────────────────────────────────────
#  SKENARIO 3 — Incast: 4 sender → 1 receiver (TCP Incast)
# ─────────────────────────────────────────────────────────────
def skenario_3_incast(net):
    """
    h1, h2, h3, h4 semuanya mengirim ke h5 secara bersamaan.
    Mensimulasikan TCP Incast problem khas data center.
    Mengukur: Window Behavior (cwnd collapse), Retransmission
    spike, Packet Loss, Out-of-Order pada sisi receiver.

    CATATAN: iperf3 server standar (1 instance, default port) hanya
    melayani SATU test sekaligus — koneksi lain akan ditolak
    ("server busy"), sehingga incast yang terukur jadi limitasi
    aplikasi iperf3, bukan kongesti jaringan murni. Maka di sini h5
    menjalankan 4 instance server di port berbeda (5201-5204), satu
    per sender, agar ke-4 koneksi benar-benar simultan dan bottleneck
    yang terukur adalah link akses h5 (100Mbps) yang sesungguhnya.
    """
    global _cur_dir
    _cur_dir = f'{OUTDIR}/s3_incast'
    mkdir(_cur_dir)
    info('\n' + '═'*60 + '\n')
    info(' SKENARIO 3: Incast — h1,h2,h3,h4 → h5 sekaligus (multi-port)\n')
    info('═'*60 + '\n')

    senders = [net.get(f'h{i}') for i in range(1, 5)]
    h5 = net.get('h5')
    ports = {1: 5201, 2: 5202, 3: 5203, 4: 5204}

    # Capture di semua sender + receiver
    for h in senders + [h5]:
        cap_start(h)

    # Bersihkan iperf3 lama (sekali saja — pkill bersifat global di Mininet)
    h5.cmd('pkill -9 -f iperf3 2>/dev/null')
    time.sleep(0.3)
    # 4 instance server di h5, port terpisah agar 4 koneksi simultan diterima
    for i in range(1, 5):
        h5.cmd(f'iperf3 -s -p {ports[i]} -D --logfile /dev/null')
    time.sleep(0.5)

    # Monitor cwnd di semua sender (incast menyebabkan cwnd collapse)
    for h in senders:
        win_start(h, '10.0.0.5', f'{_cur_dir}/window_{h.name}.txt')

    # Kirim semua bersamaan, masing-masing ke port server sendiri
    info(' > 4 sender → h5 sekaligus (port terpisah, true-incast)...\n')
    for i, h in enumerate(senders, start=1):
        h.cmd(f'iperf3 -c 10.0.0.5 -p {ports[i]} -t {DURATION_INCAST} -i 1 -f m '
              f'> {_cur_dir}/{h.name}_incast.txt 2>&1 &')

    time.sleep(DURATION_INCAST + 5)

    win_stop(*senders)
    cap_stop(*senders, h5)
    h5.cmd('pkill -9 -f iperf3 2>/dev/null')

    results = {}
    for h in senders:
        r = parse_iperf_tcp(f'{_cur_dir}/{h.name}_incast.txt')
        p = analyze_pcap(f'{_cur_dir}/{h.name}.pcap')
        results[f'{h.name}_incast'] = {**r, **p}

    # Analisis receiver side (h5) — out-of-order, duplicate
    results['h5_receiver'] = analyze_pcap(f'{_cur_dir}/h5.pcap')
    info(f' Selesai. Data: {_cur_dir}/\n')
    return results


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
_cur_dir = OUTDIR  # current scenario output dir (global untuk helper)

def main():
    global _cur_dir
    setLogLevel('info')
    mkdir(OUTDIR)

    info('\n' + '╔' + '═'*58 + '╗\n')
    info('║  ONOS Advanced Multi-Scenario QoS TCP Testing' + ' '*11 + '║\n')
    info(f'║  Output: {OUTDIR:<48}║\n')
    info('╚' + '═'*58 + '╝\n\n')

    # ─ Build topology ─
    net = build_topology()
    net.start()

    info('[*] Topologi dimulai. Menunggu ONOS mendeteksi switch via LLDP...\n')
    time.sleep(20)

    # ─ Initial discovery (2x pingall) ─
    info('[*] pingall ke-1 (host discovery)...\n')
    net.pingAll(timeout=3)
    time.sleep(5)
    info('[*] pingall ke-2 (verifikasi)...\n')
    result = net.pingAll(timeout=3)
    info(f'[*] Packet loss pingall: {result:.0f}%\n')

    if result > 20:
        warn('[!] Konektivitas belum optimal. Lanjut manual? (Enter = ya, Ctrl+C = batal)\n')
        try:
            input()
        except KeyboardInterrupt:
            net.stop()
            return

    all_results = {}

    try:
        all_results['S1 Baseline h1→h5']              = skenario_1_baseline(net)
        all_results['S2 Concurrent 4-pair']            = skenario_2_concurrent(net)
        all_results['S3 Incast 4sender→h5']            = skenario_3_incast(net)

    except KeyboardInterrupt:
        info('\n[!] Testing dihentikan.\n')
    finally:
        # Bersihkan semua proses
        for name in HOST_IPS:
            h = net.get(name)
            h.cmd('pkill -f iperf3; pkill -f tcpdump; pkill -f ITGSend; pkill -f ITGRecv; pkill -f ping; pkill -f hping3')

    info('\n' + '╔' + '═'*58 + '╗\n')
    info('║  SEMUA SKENARIO SELESAI' + ' '*34 + '║\n')
    info(f'║  Report : {txt:<47}║\n')
    info(f'║  CSV    : {csv_f:<47}║\n')
    info(f'║  Data   : {OUTDIR:<47}║\n')
    info('╚' + '═'*58 + '╝\n')

    # Print ringkasan ke terminal
    with open(txt) as f:
        info(f.read())

    # Drop ke CLI untuk inspeksi manual
    info('\n[*] Mininet CLI aktif. Ketik "exit" untuk selesai.\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    main()
