import subprocess
import sys
import time
import signal
import re
import argparse
import os

# Configuration defaults
DEFAULT_HOTSPOT_NAME = "MintHotspot"
DEFAULT_PASSWORD = "password123" 
CONNECTION_NAME = "temp_hotspot_con"
PID_FILE = "/tmp/hotspot_backend.pid"

# State tracking
HOTSPOT_IFACE = None
CURRENT_UPSTREAM_IFACE = None
MAC_MODE = "block" # or "allow"
MAC_LIST = []
EXCLUDE_VPN = False
VIRTUAL_AP_IFACE = None  # Virtual interface for STA+AP concurrency
USING_CONCURRENCY = False  # Whether we're using concurrent mode

def run_command(command, check=True):
    try:
        result = subprocess.run(
            command,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            print(f"\n[!] Error executing command: {' '.join(command)}")
            print(f"[!] System Response: {e.stderr}")
            sys.exit(1)
        return None

def ensure_wifi_active(iface):
    print("Checking Wi-Fi radio status...")
    run_command(['nmcli', 'radio', 'wifi', 'on'], check=False)
    for _ in range(5):
        output = run_command(['nmcli', '-t', '-f', 'DEVICE,STATE', 'device'])
        if any(f"{iface}:disconnected" in line for line in output.split('\n')) or \
           any(f"{iface}:connected" in line for line in output.split('\n')):
            return
        time.sleep(1)
    print(f"Warning: Interface {iface} is still not fully ready, but proceeding...")

# ============================================================
# STA+AP CONCURRENT MODE FUNCTIONS (hostapd + dnsmasq)
# ============================================================

HOSTAPD_CONF = "/tmp/hotspot_hostapd.conf"
HOSTAPD_PID = "/tmp/hotspot_hostapd.pid"
DNSMASQ_CONF = "/tmp/hotspot_dnsmasq.conf"
DNSMASQ_PID = "/tmp/hotspot_dnsmasq.pid"
DNSMASQ_LEASES = "/tmp/hotspot_dnsmasq.leases"

def check_hostapd_available():
    """Check if hostapd is installed."""
    result = subprocess.run(['which', 'hostapd'], capture_output=True, text=True)
    return result.returncode == 0

def check_dnsmasq_available():
    """Check if dnsmasq is installed."""
    result = subprocess.run(['which', 'dnsmasq'], capture_output=True, text=True)
    return result.returncode == 0

def create_virtual_ap_interface(physical_iface):
    """
    Create a virtual AP interface for STA+AP concurrent mode.
    This allows the physical interface to stay connected to WiFi
    while the virtual interface runs the hotspot.
    
    Returns: virtual_iface_name or None on failure
    """
    virtual_iface = f"{physical_iface}_ap"
    
    # Check if it already exists
    result = subprocess.run(['ip', 'link', 'show', virtual_iface], 
                           capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Virtual interface {virtual_iface} already exists, removing first...")
        delete_virtual_ap_interface(physical_iface)
    
    # Create the virtual AP interface using iw
    print(f"Creating virtual AP interface: {virtual_iface}")
    result = subprocess.run(
        ['iw', 'dev', physical_iface, 'interface', 'add', virtual_iface, 'type', '__ap'],
        capture_output=True, text=True
    )
    
    if result.returncode != 0:
        print(f"Failed to create virtual interface: {result.stderr}")
        return None
    
    # Give it a moment to initialize
    time.sleep(0.3)
    
    # Bring the interface up
    subprocess.run(['ip', 'link', 'set', virtual_iface, 'up'], check=False)
    
    # Verify it was created
    result = subprocess.run(['ip', 'link', 'show', virtual_iface], 
                           capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Virtual AP interface {virtual_iface} created successfully")
        return virtual_iface
    else:
        print(f"Virtual interface creation verification failed")
        return None

def delete_virtual_ap_interface(physical_iface):
    """Delete the virtual AP interface if it exists."""
    virtual_iface = f"{physical_iface}_ap"
    
    result = subprocess.run(['ip', 'link', 'show', virtual_iface], 
                           capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Removing virtual interface: {virtual_iface}")
        subprocess.run(['iw', 'dev', virtual_iface, 'del'], check=False)
        return True
    return False

def is_physical_interface(iface):
    """Check if an interface is backed by physical hardware (PCI/USB) vs virtual (VPN/Tun)."""
    try:
        # 1. Check if /sys/class/net/<iface>/device exists
        dev_path = f"/sys/class/net/{iface}/device"
        if not os.path.exists(dev_path):
            return False
            
        # 2. Check subsystem - virtual devices often point to /sys/devices/virtual
        # Real devices point to pci or usb
        real_path = os.path.realpath(dev_path)
        if "/virtual/" in real_path:
            return False
            
        return True
    except:
        # Fallback heuristic: tun/tap/wg naming
        return not iface.startswith(('tun', 'tap', 'wg', 'ppp'))

def get_best_channel(iface, band='bg'):
    """
    Select the best channel for the given band.
    Currently returns defaults, but could be enhanced to scan for least congested.
    """
    # TODO: Implement actual congestion scanning
    if band == 'a':
        return 36
    return 6

def get_wifi_channel(iface):
    """Get the current channel of a WiFi interface."""
    try:
        result = subprocess.run(['iw', 'dev', iface, 'info'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if 'channel' in line.lower():
                    # Parse: channel 36 (5180 MHz), width: 80 MHz
                    match = re.search(r'channel\s+(\d+)', line)
                    if match:
                        return int(match.group(1))
    except:
        pass
    return 6  # Default to channel 6

def check_5ghz_ap_allowed(channel, iface):
    """
    Check if 5GHz AP mode is allowed on the given channel for the specific interface.
    Returns: (allowed: bool, reason: str)
    """
    if channel <= 14:
        return True, "2.4GHz channel"
    
    try:
        # Resolve interface to phy
        phy_name = 'phy0' # Default
        info_result = subprocess.run(['iw', 'dev', iface, 'info'], capture_output=True, text=True, timeout=5)
        if info_result.returncode == 0:
            for line in info_result.stdout.splitlines():
                 parts = line.strip().split()
                 if len(parts) >= 2 and parts[0] == 'wiphy':
                     phy_idx = parts[1]
                     phy_name = f"phy{phy_idx}"
                     break

        # Get regulatory info for the correct PHY
        result = subprocess.run(['iw', 'phy', phy_name, 'channels'], 
                               capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            for i, line in enumerate(lines):
                # Find the channel line
                if f'[{channel}]' in line:
                    # Check if NO-IR flag is present
                    if 'No IR' in line or 'NO-IR' in line:
                        return False, f"Channel {channel} on {phy_name} has NO-IR (No Initiate Radiation) restriction"
                    return True, f"Channel {channel} allowed on {phy_name}"
    except:
        pass
    
    return True, "Unable to verify, assuming allowed"

def attempt_regulatory_bypass():
    """
    Attempt to set a permissive regulatory domain for 5GHz AP.
    Returns: bool indicating if bypass was attempted
    """
    try:
        # Try setting US regulatory domain (more permissive for 5GHz)
        subprocess.run(['iw', 'reg', 'set', 'US'], check=False, capture_output=True)
        time.sleep(0.3)
        return True
    except:
        return False

def generate_hostapd_config(iface, ssid, password, channel=6, band='bg', hidden=False, country_code='US'):
    """Generate hostapd configuration file with regulatory settings."""
    # Determine hardware mode based on band and channel
    if band == 'a' or channel > 14:
        hw_mode = 'a'
        # For 5GHz, add regulatory settings to try bypassing NO-IR
        config = f"""# Hotspot hostapd configuration
interface={iface}
driver=nl80211
ssid={ssid}
country_code={country_code}
ieee80211d=1
ieee80211h=1
hw_mode={hw_mode}
channel={channel}
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid={'1' if hidden else '0'}
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
"""
    else:
        hw_mode = 'g'
        config = f"""# Hotspot hostapd configuration
interface={iface}
driver=nl80211
ssid={ssid}
hw_mode={hw_mode}
channel={channel}
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid={'1' if hidden else '0'}
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""
    
    with open(HOSTAPD_CONF, 'w') as f:
        f.write(config)

    
    return HOSTAPD_CONF

def generate_dnsmasq_config(iface, dns_server=None):
    """Generate dnsmasq configuration for DHCP."""
    # Use a subnet that's unlikely to conflict
    subnet = "192.168.45"
    
    config = f"""# Hotspot dnsmasq configuration
interface={iface}
bind-interfaces
dhcp-range={subnet}.10,{subnet}.250,255.255.255.0,24h
dhcp-option=option:router,{subnet}.1
dhcp-leasefile={DNSMASQ_LEASES}
"""
    
    if dns_server:
        config += f"server={dns_server}\n"
    else:
        # BUG FIX: Do NOT force Google DNS (8.8.8.8).
        # Many VPNs block non-VPN DNS traffic to prevent leaks.
        # By omitting 'server=', dnsmasq defaults to determining upstream DNS from host settings (resolv.conf),
        # which will correctly point to the VPN's DNS when active.
        pass
    
    with open(DNSMASQ_CONF, 'w') as f:
        f.write(config)
    
    return DNSMASQ_CONF, f"{subnet}.1"

def start_hostapd(config_file):
    """Start hostapd with the given config."""
    # Kill any existing hostapd
    subprocess.run(['pkill', '-f', 'hostapd.*hotspot'], check=False)
    time.sleep(0.3)
    
    # Start hostapd
    print("Starting hostapd...")
    proc = subprocess.Popen(
        ['hostapd', '-B', '-P', HOSTAPD_PID, config_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait a bit and check if it started successfully
    time.sleep(1)
    
    if os.path.exists(HOSTAPD_PID):
        with open(HOSTAPD_PID, 'r') as f:
            pid = f.read().strip()
        # Check if process is running
        try:
            os.kill(int(pid), 0)
            print(f"hostapd started successfully (PID: {pid})")
            return True
        except:
            pass
    
    # Check for errors
    result = subprocess.run(['hostapd', '-d', config_file], 
                           capture_output=True, text=True, timeout=3)
    print(f"hostapd failed to start: {result.stderr}")
    return False

def start_dnsmasq(config_file):
    """Start dnsmasq with the given config."""
    # Kill any existing dnsmasq for our config
    subprocess.run(['pkill', '-f', 'dnsmasq.*hotspot'], check=False)
    time.sleep(0.3)
    
    # Ensure lease file exists
    open(DNSMASQ_LEASES, 'a').close()
    
    # Start dnsmasq
    print("Starting dnsmasq...")
    result = subprocess.run(
        ['dnsmasq', '-C', config_file, '-x', DNSMASQ_PID],
        capture_output=True, text=True
    )
    
    if result.returncode == 0:
        print("dnsmasq started successfully")
        return True
    else:
        print(f"dnsmasq failed to start: {result.stderr}")
        return False

def setup_concurrent_ap_network(iface, gateway_ip, upstream_iface):
    """Configure network for the concurrent AP interface."""
    # Assign IP to the AP interface
    subprocess.run(['ip', 'addr', 'flush', 'dev', iface], check=False)
    subprocess.run(['ip', 'addr', 'add', f'{gateway_ip}/24', 'dev', iface], check=False)
    subprocess.run(['ip', 'link', 'set', iface, 'up'], check=False)
    
    # Enable IP forwarding
    subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=1'], check=False)
    
    # Setup NAT for the AP subnet
    subnet = gateway_ip.rsplit('.', 1)[0] + '.0/24'
    subprocess.run(['iptables', '-t', 'nat', '-A', 'POSTROUTING', 
                   '-s', subnet, '-o', upstream_iface, '-j', 'MASQUERADE'], check=False)
    subprocess.run(['iptables', '-A', 'FORWARD', '-i', iface, '-o', upstream_iface, '-j', 'ACCEPT'], check=False)
    subprocess.run(['iptables', '-A', 'FORWARD', '-i', upstream_iface, '-o', iface, 
                   '-m', 'state', '--state', 'RELATED,ESTABLISHED', '-j', 'ACCEPT'], check=False)
    
    # Fix MTU issues for VPN (clamp MSS to PMTU) - Critical for concurrent-mode VPN routing
    subprocess.run(['iptables', '-A', 'FORWARD', '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', 
                   '-j', 'TCPMSS', '--clamp-mss-to-pmtu'], check=False)

def stop_concurrent_mode():
    """Stop hostapd and dnsmasq, cleanup everything."""
    print("Stopping concurrent mode services...")
    
    # Stop hostapd
    if os.path.exists(HOSTAPD_PID):
        try:
            with open(HOSTAPD_PID, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
        except:
            pass
        os.remove(HOSTAPD_PID)
    subprocess.run(['pkill', '-f', 'hostapd.*hotspot'], check=False)
    
    # Stop dnsmasq
    if os.path.exists(DNSMASQ_PID):
        try:
            with open(DNSMASQ_PID, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
        except:
            pass
        os.remove(DNSMASQ_PID)
    subprocess.run(['pkill', '-f', 'dnsmasq.*hotspot'], check=False)
    
    # Clean up config files
    for f in [HOSTAPD_CONF, DNSMASQ_CONF, DNSMASQ_LEASES]:
        if os.path.exists(f):
            os.remove(f)

def get_wifi_interfaces():
    """Legacy function for backward compatibility."""
    interfaces = get_detailed_interfaces()
    return [iface['name'] for iface in interfaces if iface['type'] == 'wifi']

def get_detailed_interfaces():
    """
    Get detailed information about all network interfaces.
    Returns list of dicts with comprehensive info for each interface.
    """
    interfaces = []
    
    try:
        run_command(['nmcli', 'device', 'refresh'], check=False)
        
        # Get device list with connection info
        output = run_command(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device'], check=False)
        if not output:
            return interfaces
        
        # Also get IP addresses for each interface
        ip_info = {}
        try:
            ip_output = subprocess.run(['ip', '-4', 'addr', 'show'], capture_output=True, text=True, timeout=5)
            current_iface = None
            for line in ip_output.stdout.splitlines():
                if not line.startswith(' '):
                    parts = line.split(':')
                    if len(parts) >= 2:
                        current_iface = parts[1].strip()
                elif 'inet ' in line and current_iface:
                    ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
                    if ip_match:
                        ip_info[current_iface] = ip_match.group(1)
        except:
            pass
        
        # Check for VPN tunnels and other special interfaces
        all_ifaces = []
        try:
            for iface_dir in os.listdir('/sys/class/net'):
                all_ifaces.append(iface_dir)
        except:
            pass
        
        for line in output.splitlines():
            parts = line.split(':')
            if len(parts) < 3:
                continue
            
            dev_name = parts[0]
            dev_type = parts[1]
            dev_state = parts[2]
            dev_connection = parts[3] if len(parts) > 3 else ""
            
            # Skip certain virtual interfaces but keep useful ones
            skip_prefixes = ('lo', 'docker', 'br-', 'veth', 'virbr', 'p2p-', 'vboxnet')
            if dev_name.startswith(skip_prefixes):
                continue
            if dev_type == 'wifi-p2p':
                continue
            
            iface_info = {
                'name': dev_name,
                'type': dev_type,
                'state': dev_state,
                'connected': dev_state == 'connected',
                'connection_name': dev_connection if dev_connection and dev_connection != '--' else None,
                'driver': None,
                'bus': None,
                'is_usb': False,
                'is_internal': False,
                'is_vpn': False,
                'is_mobile': False,
                'is_tethered': False,
                'is_bridge': False,
                'ap_support': False,
                'supports_5ghz': False,
                'in_monitor_mode': False,
                'has_ip': dev_name in ip_info,
                'ip_address': ip_info.get(dev_name),
                'is_internet_source': False,
                'label': dev_name,
                'issues': []  # List of potential problems
            }
            
            # Detect special interface types
            # VPN tunnels
            if dev_name.startswith(('tun', 'tap', 'wg', 'ppp', 'vpn')):
                iface_info['is_vpn'] = True
                iface_info['type'] = 'vpn'
            
            # Mobile broadband
            if dev_name.startswith(('wwan', 'wwp', 'cdc', 'mbim')):
                iface_info['is_mobile'] = True
                iface_info['type'] = 'mobile'
            
            # USB tethering (Android/iPhone)
            if dev_name.startswith(('usb', 'enp0s20u', 'enp0s2')) and 'usb' in dev_name.lower():
                iface_info['is_tethered'] = True
            
            # Bridge interfaces (some may be useful)
            if dev_name.startswith('br') and not dev_name.startswith('br-'):
                iface_info['is_bridge'] = True
                iface_info['type'] = 'bridge'
            
            # Get driver and bus info
            try:
                # Check if USB device
                device_path_link = f'/sys/class/net/{dev_name}/device'
                if os.path.exists(device_path_link):
                    usb_check = subprocess.run(
                        ['readlink', '-f', device_path_link],
                        capture_output=True, text=True, timeout=2
                    )
                    if usb_check.returncode == 0:
                        device_path = usb_check.stdout.strip()
                        iface_info['is_usb'] = '/usb' in device_path
                        iface_info['is_internal'] = '/pci' in device_path and '/usb' not in device_path
                    
                    # Get driver name
                    driver_link = f'{device_path_link}/driver'
                    if os.path.exists(driver_link):
                        driver_check = subprocess.run(
                            ['readlink', '-f', driver_link],
                            capture_output=True, text=True, timeout=2
                        )
                        if driver_check.returncode == 0:
                            iface_info['driver'] = os.path.basename(driver_check.stdout.strip())
            except:
                pass
            
            # Check AP mode support and monitor mode for WiFi interfaces
            if dev_type == 'wifi':
                ap_ok, ap_err = check_ap_mode_support_for_iface(dev_name)
                iface_info['ap_support'] = ap_ok
                if not ap_ok:
                    iface_info['issues'].append("No AP mode support")
                
                # Check 5GHz support
                iface_info['supports_5ghz'] = check_5ghz_support_for_iface(dev_name)
                
                # Check STA/AP concurrency support
                supports_concurrency, concurrency_channels = check_sta_ap_concurrency(dev_name)
                iface_info['supports_concurrency'] = supports_concurrency
                iface_info['concurrency_channels'] = concurrency_channels
                
                # Check if in monitor mode
                try:
                    iw_info = subprocess.run(['iw', 'dev', dev_name, 'info'], 
                                            capture_output=True, text=True, timeout=3)
                    if 'type monitor' in iw_info.stdout.lower():
                        iface_info['in_monitor_mode'] = True
                        iface_info['issues'].append("In monitor mode")
                except:
                    pass
            
            # Check for connectivity issues
            if iface_info['connected'] and not iface_info['has_ip']:
                iface_info['issues'].append("Connected but no IP")
            
            # Generate human-friendly label
            iface_info['label'] = generate_interface_label(iface_info)
            
            interfaces.append(iface_info)
        
        # Determine which interface is providing internet
        try:
            route_output = subprocess.run(['ip', 'route', 'show', 'default'], 
                                         capture_output=True, text=True, timeout=5)
            if route_output.returncode == 0:
                match = re.search(r'dev\s+(\S+)', route_output.stdout)
                if match:
                    internet_iface = match.group(1)
                    for iface in interfaces:
                        if iface['name'] == internet_iface:
                            iface['is_internet_source'] = True
                            break
        except:
            pass
    
    except Exception as e:
        print(f"Error discovering interfaces: {e}")
    
    return interfaces

def get_all_internet_sources():
    """Get all interfaces that could provide internet (including VPN tunnels)."""
    interfaces = get_detailed_interfaces()
    sources = []
    
    # Priority order: Ethernet > Mobile > Tethered > VPN > WiFi
    for iface in interfaces:
        if not iface['connected'] and not iface['has_ip']:
            continue
        
        source_info = {
            'name': iface['name'],
            'label': iface['label'],
            'type': iface['type'],
            'priority': 0,
            'is_vpn': iface['is_vpn'],
            'is_optimal': False
        }
        
        # Assign priority
        if iface['type'] == 'ethernet' and iface['has_ip']:
            source_info['priority'] = 100
            source_info['is_optimal'] = True
        elif iface['is_mobile'] and iface['has_ip']:
            source_info['priority'] = 80
        elif iface['is_tethered'] and iface['has_ip']:
            source_info['priority'] = 70
        elif iface['is_vpn'] and iface['has_ip']:
            source_info['priority'] = 60
        elif iface['type'] == 'wifi' and iface['connected']:
            source_info['priority'] = 50
        
        if source_info['priority'] > 0:
            sources.append(source_info)
    
    # Sort by priority
    sources.sort(key=lambda x: x['priority'], reverse=True)
    return sources


def check_sta_ap_concurrency(iface):
    """
    Check if a WiFi interface supports STA/AP concurrency (simultaneous client + hotspot).
    
    This parses the 'valid interface combinations' from iw phy output.
    A card supports concurrency if it shows:
        #{ managed } <= 1, #{ AP } <= 1
    or similar with both managed (STA) and AP in the same combination.
    
    Returns: (supports_concurrency: bool, max_channels: int or None)
    """
    try:
        # Get the phy name for this interface
        phy_result = subprocess.run(
            ['iw', 'dev', iface, 'info'],
            capture_output=True, text=True, timeout=5
        )
        if phy_result.returncode != 0:
            return False, None
        
        phy_name = None
        for line in phy_result.stdout.splitlines():
            if 'wiphy' in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'wiphy':
                        phy_name = f"phy{parts[i+1]}"
                        break
        
        if not phy_name:
            return False, None
        
        # Get phy info with interface combinations
        phy_info = subprocess.run(['iw', phy_name, 'info'], capture_output=True, text=True, timeout=5)
        if phy_info.returncode != 0:
            return False, None
        
        # Parse valid interface combinations
        in_combinations = False
        current_combo = ""
        max_channels = 1
        
        for line in phy_info.stdout.splitlines():
            if 'valid interface combinations:' in line.lower():
                in_combinations = True
                continue
            
            if in_combinations:
                # End of combinations section
                if line.strip() and not line.startswith('\t') and not line.startswith(' '):
                    break
                
                # Accumulate combination lines
                current_combo += " " + line.strip()
                
                # Check for channels info
                if '#channels' in line:
                    match = re.search(r'#channels\s*<=\s*(\d+)', line)
                    if match:
                        max_channels = max(max_channels, int(match.group(1)))
        
        # Check if combination allows both managed (STA) and AP
        # Look for patterns like: #{ managed } <= N ... #{ AP ... } <= M
        has_managed = bool(re.search(r'#\{\s*managed\s*\}', current_combo, re.IGNORECASE))
        has_ap = bool(re.search(r'#\{[^}]*AP[^}]*\}', current_combo, re.IGNORECASE))
        
        # Also check total count allows 2+
        total_match = re.search(r'total\s*<=\s*(\d+)', current_combo)
        total_ok = total_match and int(total_match.group(1)) >= 2
        
        supports_concurrency = has_managed and has_ap and total_ok
        
        return supports_concurrency, max_channels if supports_concurrency else None
        
    except Exception as e:
        return False, None


def check_ap_mode_support_for_iface(iface):
    """Check if a specific interface supports AP mode."""
    try:
        # Get the phy name for this interface
        phy_result = subprocess.run(
            ['iw', 'dev', iface, 'info'],
            capture_output=True, text=True, timeout=5
        )
        if phy_result.returncode != 0:
            return True, None  # Can't check, assume OK
        
        phy_name = None
        for line in phy_result.stdout.splitlines():
            if 'wiphy' in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'wiphy':
                        phy_name = f"phy{parts[i+1]}"
                        break
        
        if not phy_name:
            return True, None
        
        # Check capabilities for this phy
        phy_info = subprocess.run(['iw', phy_name, 'info'], capture_output=True, text=True, timeout=5)
        if phy_info.returncode != 0:
            return True, None
        
        in_modes = False
        for line in phy_info.stdout.splitlines():
            if 'Supported interface modes:' in line:
                in_modes = True
                continue
            if in_modes:
                if line.strip().startswith('*'):
                    if 'AP' in line:
                        return True, None
                elif line.strip() and not line.startswith('\t\t'):
                    in_modes = False
        
        return False, f"{iface} does not support AP mode"
    except:
        return True, None

def check_5ghz_support_for_iface(iface):
    """Check if a specific interface supports 5GHz."""
    try:
        phy_result = subprocess.run(
            ['iw', 'dev', iface, 'info'],
            capture_output=True, text=True, timeout=5
        )
        if phy_result.returncode != 0:
            return False
        
        phy_name = None
        for line in phy_result.stdout.splitlines():
            if 'wiphy' in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'wiphy':
                        phy_name = f"phy{parts[i+1]}"
                        break
        
        if not phy_name:
            return False
        
        phy_info = subprocess.run(['iw', phy_name, 'info'], capture_output=True, text=True, timeout=5)
        # Look for 5GHz frequencies
        return '5180' in phy_info.stdout or '5240' in phy_info.stdout or '5745' in phy_info.stdout
    except:
        return False

def generate_interface_label(iface_info):
    """Generate a human-friendly label for an interface."""
    name = iface_info['name']
    itype = iface_info.get('type', 'unknown')
    
    parts = []
    
    # Base type with clear descriptions
    if itype == 'wifi':
        if iface_info.get('is_usb'):
            parts.append("🔌 USB Wi-Fi Adapter")
        elif iface_info.get('is_internal'):
            parts.append("📶 Built-in Wi-Fi")
        else:
            parts.append("📶 Wi-Fi")
    elif itype == 'ethernet':
        if iface_info.get('is_usb'):
            parts.append("🔌 USB Ethernet")
        else:
            parts.append("🔗 Ethernet")
    elif itype == 'vpn' or iface_info.get('is_vpn'):
        parts.append("🔒 VPN Tunnel")
    elif itype == 'mobile' or iface_info.get('is_mobile'):
        parts.append("📱 Mobile Broadband")
    elif iface_info.get('is_tethered'):
        parts.append("📱 Phone Tethering")
    elif itype == 'bridge':
        parts.append("🌉 Bridge")
    else:
        parts.append(itype.title())
    
    # Add capabilities for WiFi
    if itype == 'wifi':
        caps = []
        if iface_info.get('ap_support'):
            caps.append("AP")
        if iface_info.get('supports_5ghz'):
            caps.append("5GHz")
        if iface_info.get('supports_concurrency'):
            caps.append("STA+AP")
        if iface_info.get('in_monitor_mode'):
            caps.append("⚠️Monitor")
        if caps:
            parts.append(f"[{', '.join(caps)}]")
    
    # Mark as internet source
    if iface_info.get('is_internet_source'):
        parts.append("🌐")
    
    # Add connection status
    if iface_info.get('connected') and iface_info.get('connection_name'):
        parts.append(f"→ {iface_info['connection_name']}")
    elif iface_info.get('has_ip'):
        parts.append(f"→ {iface_info.get('ip_address', 'connected')}")
    
    # Add device name
    parts.append(f"({name})")
    
    # Add issues if any
    issues = iface_info.get('issues', [])
    if issues:
        parts.append(f"⚠️ {', '.join(issues)}")
    
    return " ".join(parts)

def get_smart_interface_selection(manual_internet_iface=None):
    """
    Intelligently select which interface should be used for internet and which for hotspot.
    Returns: (internet_iface, hotspot_iface, reason)
    
    Priority for Internet Source:
    0. Manual override (if provided)
    1. Ethernet (wired) - most stable
    2. Mobile broadband (wwan)
    3. Phone tethering (usb0)
    4. VPN tunnel (if route_vpn enabled)
    5. WiFi connection
    
    Priority for Hotspot:
    1. USB WiFi adapter with AP support (leaves internal free)
    2. Internal WiFi with AP support (if not needed for internet)
    3. Any WiFi with AP support
    """
    interfaces = get_detailed_interfaces()
    
    # Categorize interfaces
    ethernet_ifaces = [i for i in interfaces if i['type'] == 'ethernet' and i['state'] != 'unavailable']
    wifi_ifaces = [i for i in interfaces if i['type'] == 'wifi']

    vpn_ifaces = [i for i in interfaces if i.get('is_vpn') and i.get('has_ip')]
    mobile_ifaces = [i for i in interfaces if i.get('is_mobile') and i.get('has_ip')]
    tether_ifaces = [i for i in interfaces if i.get('is_tethered') and i.get('has_ip')]
    
    # Filter WiFi by capabilities
    usb_wifi = [i for i in wifi_ifaces if i.get('is_usb') and i.get('ap_support') and not i.get('in_monitor_mode')]

    internal_wifi = [i for i in wifi_ifaces if i.get('is_internal') and not i.get('in_monitor_mode')]
    ap_capable_wifi = [i for i in wifi_ifaces if i.get('ap_support') and not i.get('in_monitor_mode')]
    
    # Find connected interfaces
    connected_ethernet = [i for i in ethernet_ifaces if i.get('connected') or i.get('has_ip')]
    connected_wifi = [i for i in wifi_ifaces if i.get('connected')]
    
    internet_iface = None
    hotspot_iface = None
    reason = ""
    warnings = []
    
    # === DETERMINE INTERNET SOURCE ===
    
    if manual_internet_iface:
        internet_iface = manual_internet_iface
        reason = f"👤 Manually selected internet source: {manual_internet_iface}"
    
    # Priority 1: Ethernet with IP
    elif connected_ethernet:
        best_eth = connected_ethernet[0]
        internet_iface = best_eth['name']
        if best_eth.get('is_usb'):
            reason = "🔗 Using USB Ethernet for internet"
        else:
            reason = "🔗 Using Ethernet for internet (optimal)"
    
    # Priority 2: Mobile broadband
    elif mobile_ifaces:
        internet_iface = mobile_ifaces[0]['name']
        reason = "📱 Using Mobile Broadband for internet"
    
    # Priority 3: Phone tethering
    elif tether_ifaces:
        internet_iface = tether_ifaces[0]['name']
        reason = "📱 Using Phone Tethering for internet"
    
    # Priority 4: Connected WiFi (will warn if same as hotspot)
    elif connected_wifi:
        internet_iface = connected_wifi[0]['name']
        reason = "📶 Using WiFi for internet"
    
    # No internet source found
    else:
        reason = "⚠️ No internet source found"
        warnings.append("Hotspot clients will not have internet access")
    
    # === DETERMINE HOTSPOT INTERFACE ===
    
    # Check for monitor mode conflicts
    monitor_mode_ifaces = [i for i in wifi_ifaces if i.get('in_monitor_mode')]
    if monitor_mode_ifaces:
        warnings.append(f"{monitor_mode_ifaces[0]['name']} is in monitor mode (cannot use for AP)")
    
    # Filter for concurrency-capable WiFi
    concurrent_wifi = [i for i in wifi_ifaces if i.get('supports_concurrency') and i.get('ap_support') and not i.get('in_monitor_mode')]
    
    # Priority 1: USB WiFi adapter with AP support (ALWAYS PREFERRED)
    # Using a separate physical radio is always better than time-slicing one radio
    if usb_wifi:
        hotspot_iface = usb_wifi[0]['name']
        reason += " | 🔌 USB adapter for hotspot (Dedicated Hardware)"
        
    # Priority 2: Concurrency-capable WiFi that is providing internet
    # This is the Next Best case - same adapter does both STA (client) and AP (hotspot)
    elif concurrent_wifi:
        concurrent_and_connected = [i for i in concurrent_wifi if i.get('connected')]
        
        # If internet source is a concurrent-capable WiFi, use it!
        if concurrent_and_connected and internet_iface in [i['name'] for i in concurrent_and_connected]:
            candidate = [i for i in concurrent_and_connected if i['name'] == internet_iface][0]
            hotspot_iface = candidate['name']
            channels = candidate.get('concurrency_channels', 1)
            if channels >= 2:
                reason += f" | 🎯 Same WiFi for hotspot (STA+AP concurrent, {channels} channels)"
            else:
                reason += " | 🎯 Same WiFi for hotspot (STA+AP concurrent, same channel)"
        
        # Otherwise just pick the best concurrent-capable one
        else:
            candidate = concurrent_wifi[0]
            hotspot_iface = candidate['name']
            if candidate.get('connected'):
                reason += f" | 🎯 Built-in WiFi (STA+AP concurrent)"
            else:
                reason += " | 📶 Built-in WiFi for hotspot"
    
    # Priority 3: Internal WiFi NOT being used for internet (legacy - no concurrency)
    elif internal_wifi:
        internal_with_ap = [i for i in internal_wifi if i.get('ap_support')]
        if internal_with_ap:
            candidate = internal_with_ap[0]
            hotspot_iface = candidate['name']
            
            # Check if this is also the internet source
            if candidate['name'] == internet_iface:
                if len(wifi_ifaces) == 1 and not connected_ethernet and not mobile_ifaces and not tether_ifaces:
                    # Critical: only one WiFi, no concurrency, and it's providing internet
                    reason = "⚠️ SINGLE ADAPTER: Will disconnect from current network"
                    warnings.append("Your only WiFi will switch from client to AP mode")
                else:
                    reason += " | 📶 Built-in WiFi for hotspot"
            else:
                reason += " | 📶 Built-in WiFi for hotspot"
    
    # Priority 4: Any AP-capable WiFi
    elif ap_capable_wifi:
        hotspot_iface = ap_capable_wifi[0]['name']
        reason += " | 📶 WiFi for hotspot"
    
    # No usable WiFi for hotspot
    else:
        if wifi_ifaces:
            no_ap = [i for i in wifi_ifaces if not i.get('ap_support')]
            if no_ap:
                reason = f"❌ No AP support: {no_ap[0]['name']} cannot create hotspot"
            elif monitor_mode_ifaces:
                reason = f"❌ {monitor_mode_ifaces[0]['name']} in monitor mode"
            else:
                reason = "❌ WiFi adapters have issues"
        else:
            reason = "❌ No WiFi adapter found"
    
    # Add warnings to reason
    if warnings:
        reason += " | ⚠️ " + "; ".join(warnings)
    
    return internet_iface, hotspot_iface, reason





def check_ap_mode_support(iface):
    """Check if interface supports AP (Access Point) mode using iw."""
    try:
        output = subprocess.run(['iw', 'list'], capture_output=True, text=True, timeout=5)
        if output.returncode != 0:
            return True, "Could not verify AP support (iw command failed)"
        
        # Parse iw list output to find interface capabilities
        in_supported_modes = False
        for line in output.stdout.splitlines():
            if 'Supported interface modes:' in line:
                in_supported_modes = True
                continue
            if in_supported_modes:
                if line.strip().startswith('*'):
                    if 'AP' in line:
                        return True, None
                else:
                    in_supported_modes = False
        
        return False, f"Interface {iface} does not support AP (Access Point) mode"
    except Exception as e:
        return True, f"Could not verify AP support: {e}"

def check_5ghz_support():
    """Check if any WiFi card supports 5GHz band."""
    try:
        output = subprocess.run(['iw', 'list'], capture_output=True, text=True, timeout=5)
        if output.returncode != 0:
            return True, "Could not verify 5GHz support"
        
        # Look for 5 GHz frequencies (5180 MHz and above)
        if '5180' in output.stdout or '5240' in output.stdout or '5745' in output.stdout:
            return True, None
        return False, "Your Wi-Fi adapter does not support 5GHz band. Use 2.4GHz instead."
    except Exception as e:
        return True, f"Could not verify 5GHz support: {e}"

def check_rfkill_status(iface):
    """Check if WiFi is blocked by hardware or software RF kill switch."""
    try:
        # Check rfkill status
        result = subprocess.run(['rfkill', 'list', 'wifi'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return True, None  # Can't check, assume OK
        
        output = result.stdout.lower()
        if 'hard blocked: yes' in output:
            return False, "Wi-Fi is HARDWARE BLOCKED. Check physical Wi-Fi switch on your laptop."
        if 'soft blocked: yes' in output:
            return False, "Wi-Fi is SOFTWARE BLOCKED. Run: sudo rfkill unblock wifi"
        return True, None
    except Exception as e:
        return True, None  # Can't check, proceed

def check_interface_state(iface):
    """Check interface operational state."""
    try:
        # Check if interface is UP
        result = subprocess.run(['ip', 'link', 'show', iface], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False, f"Interface {iface} not found in system"
        
        if 'state DOWN' in result.stdout:
            print(f"Interface {iface} is DOWN. Attempting to bring it UP...")
            # Try to unblock radio first
            subprocess.run(['rfkill', 'unblock', 'wifi'], check=False)
            subprocess.run(['ip', 'link', 'set', iface, 'up'], check=False)
            
            # Retry loop (give it up to 5 seconds)
            for _ in range(5):
                time.sleep(1)
                result = subprocess.run(['ip', 'link', 'show', iface], capture_output=True, text=True)
                if 'state DOWN' not in result.stdout:
                    print(f"Interface {iface} is now UP.")
                    return True, None
            
            # If still down, return True (Success) but with a Warning.
            # We proceeded because nmcli often can bring it up automatically during connection.
            msg = f"Interface {iface} appears DOWN. Proceeding anyway, but this may fail."
            print(f"Warning: {msg}")
            return True, msg
        if 'NO-CARRIER' in result.stdout and 'state UP' not in result.stdout:
            return True, f"Interface {iface} has no carrier (normal for WiFi before connection)"
        return True, None
    except Exception as e:
        return True, None

STATUS_FILE = "/tmp/hotspot_status.json"

def write_status(status, message, is_error=False):
    """Write status to file for GUI to read and display notifications."""
    import json
    try:
        data = {
            "timestamp": time.time(),
            "status": status,
            "message": message,
            "is_error": is_error
        }
        with open(STATUS_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def check_interface_busy(iface):
    """Check if the interface is currently connected to a WiFi network."""
    try:
        output = run_command(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device'], check=False)
        if output:
            for line in output.splitlines():
                parts = line.split(':')
                if len(parts) >= 3 and parts[0] == iface:
                    state = parts[1]
                    connection = parts[2] if len(parts) > 2 else ""
                    if state == "connected" and connection and connection != CONNECTION_NAME:
                        return True, connection
        return False, None
    except:
        return False, None

def preflight_checks(interface=None, ssid=None, password=None, exclude_vpn=False, force_single_interface=False, band='bg'):
    """
    Comprehensive pre-flight validation before starting hotspot.
    Returns (success: bool, error_message: str, warnings: list)
    """
    errors = []
    warnings = []
    
    # Get detailed interface information
    all_interfaces = get_detailed_interfaces()
    wifi_interfaces = [i for i in all_interfaces if i['type'] == 'wifi']
    ethernet_interfaces = [i for i in all_interfaces if i['type'] == 'ethernet']
    mobile_interfaces = [i for i in all_interfaces if i.get('is_mobile')]
    tether_interfaces = [i for i in all_interfaces if i.get('is_tethered')]
    vpn_interfaces = [i for i in all_interfaces if i.get('is_vpn')]
    
    # 1. Check if NetworkManager is running
    try:
        result = subprocess.run(['systemctl', 'is-active', 'NetworkManager'], 
                               capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            errors.append("NetworkManager is not running. Start it with: sudo systemctl start NetworkManager")
    except Exception as e:
        warnings.append(f"Could not verify NetworkManager status: {e}")
    
    # 2. Check RF kill status FIRST (hardware/software blocks)
    rfkill_ok, rfkill_error = check_rfkill_status(None)
    if not rfkill_ok:
        errors.append(rfkill_error)
        return False, "\n".join(errors), warnings
    
    # 3. Check for Wi-Fi interfaces
    if not wifi_interfaces:
        errors.append("No Wi-Fi interfaces found. Ensure your Wi-Fi adapter is connected and recognized.")
        return False, "\n".join(errors), warnings
    
    # 4. Check for monitor mode on all WiFi interfaces
    monitor_mode_ifaces = [i for i in wifi_interfaces if i.get('in_monitor_mode')]
    if monitor_mode_ifaces:
        iface_names = [i['name'] for i in monitor_mode_ifaces]
        warnings.append(f"Interface(s) in monitor mode (cannot use for AP): {', '.join(iface_names)}")
    
    # 5. Determine which interface to use
    target_iface = interface
    target_iface_info = None
    
    if not target_iface:
        # Smart selection using new logic
        _, recommended_hotspot, _ = get_smart_interface_selection()
        target_iface = recommended_hotspot
    
    # Find detailed info for target interface
    for i in wifi_interfaces:
        if i['name'] == target_iface:
            target_iface_info = i
            break
    
    if not target_iface:
        errors.append(f"No suitable Wi-Fi interface found for hotspot.")
        return False, "\n".join(errors), warnings
    
    if not target_iface_info:
        wifi_names = [i['name'] for i in wifi_interfaces]
        errors.append(f"Interface '{target_iface}' not found. Available: {', '.join(wifi_names)}")
        return False, "\n".join(errors), warnings
    
    # 6. Check if target interface is in monitor mode
    if target_iface_info.get('in_monitor_mode'):
        errors.append(
            f"Interface {target_iface} is in MONITOR MODE and cannot be used for Access Point.\n"
            f"To fix: sudo iw dev {target_iface} set type managed"
        )
        return False, "\n".join(errors), warnings
    
    # 7. Check interface operational state
    iface_ok, iface_error = check_interface_state(target_iface)
    if not iface_ok:
        errors.append(iface_error)
    elif iface_error:
        warnings.append(iface_error)
    
    # 8. Check AP mode support for the specific interface
    if not target_iface_info.get('ap_support'):
        other_ap = [i for i in wifi_interfaces if i.get('ap_support') and not i.get('in_monitor_mode')]
        if other_ap:
            errors.append(
                f"Interface {target_iface} does not support AP mode.\n"
                f"Alternative with AP support: {other_ap[0]['label']}"
            )
        else:
            errors.append(
                f"Interface {target_iface} does not support AP (Access Point) mode.\n"
                f"You may need a different Wi-Fi adapter that supports AP mode."
            )
    
    # 9. Check if interface is busy and analyze alternatives
    is_busy = target_iface_info.get('connected')
    connection_name = target_iface_info.get('connection_name')
    
    # Determine all available internet sources
    connected_ethernet = [i for i in ethernet_interfaces if i.get('connected') or i.get('has_ip')]
    connected_mobile = [i for i in mobile_interfaces if i.get('has_ip')]
    connected_tether = [i for i in tether_interfaces if i.get('has_ip')]
    connected_vpn = [i for i in vpn_interfaces if i.get('has_ip')]
    connected_wifi = [i for i in wifi_interfaces if i.get('connected') and i['name'] != target_iface]
    
    has_alternative_internet = bool(connected_ethernet or connected_mobile or connected_tether or connected_wifi)
    
    # Check if this adapter supports STA/AP concurrency
    supports_concurrency = target_iface_info.get('supports_concurrency', False)
    
    # Determine internet source
    upstream = get_upstream_interface(exclude_vpn)
    
    if is_busy:
        is_internet_source = target_iface_info.get('is_internet_source') or target_iface == upstream
        
        if is_internet_source:
            # This WiFi interface is providing internet
            ap_capable_other_wifi = [i for i in wifi_interfaces 
                                     if i['name'] != target_iface 
                                     and i.get('ap_support') 
                                     and not i.get('in_monitor_mode')]
            
            # NEW: If adapter supports STA/AP concurrency, no disconnection will occur
            if supports_concurrency:
                channels = target_iface_info.get('concurrency_channels', 1)
                if channels >= 2:
                    warnings.append(
                        f"🎯 STA+AP Concurrent Mode: WiFi stays connected to '{connection_name}' "
                        f"while hosting hotspot ({channels} channels available)."
                    )
                else:
                    warnings.append(
                        f"🎯 STA+AP Concurrent Mode: WiFi stays connected to '{connection_name}' "
                        f"while hosting hotspot (same channel required)."
                    )
            elif not has_alternative_internet and len(wifi_interfaces) == 1:
                # CRITICAL: Single WiFi, no concurrency, no alternatives = will lose all internet
                if force_single_interface:
                    warnings.append(
                        f"⚠️ FORCED: Your only Wi-Fi interface ({target_iface}) will disconnect from '{connection_name}'. "
                        f"You will lose internet connectivity. Proceed with caution."
                    )
                else:
                    solutions = ["Solutions:"]
                    solutions.append("  1. Connect via Ethernet cable first")
                    solutions.append("  2. Add a USB Wi-Fi adapter for the hotspot")
                    solutions.append("  3. Tether your phone via USB for internet")
                    solutions.append("  4. Use --force-single-interface flag if you understand the risk")
                    errors.append(
                        f"BLOCKED: Your only Wi-Fi interface ({target_iface}) is providing internet via '{connection_name}'.\n"
                        f"Starting a hotspot will disconnect you completely.\n" + "\n".join(solutions)
                    )
            elif connected_ethernet:
                # Ethernet is available - just warn about WiFi disconnection
                eth_name = connected_ethernet[0]['name']
                warnings.append(
                    f"Your Wi-Fi ({target_iface}) will disconnect from '{connection_name}'. "
                    f"Internet will continue via Ethernet ({eth_name})."
                )
            elif connected_mobile:
                warnings.append(
                    f"Your Wi-Fi ({target_iface}) will disconnect from '{connection_name}'. "
                    f"Internet will continue via Mobile Broadband."
                )
            elif connected_tether:
                warnings.append(
                    f"Your Wi-Fi ({target_iface}) will disconnect from '{connection_name}'. "
                    f"Internet will continue via Phone Tethering."
                )
            elif ap_capable_other_wifi:
                # Suggest using the other WiFi adapter instead
                warnings.append(
                    f"Interface {target_iface} is providing internet. "
                    f"Consider using {ap_capable_other_wifi[0]['label']} for hotspot instead."
                )
            else:
                warnings.append(
                    f"Interface {target_iface} is connected to '{connection_name}'. "
                    f"It will be disconnected to start the hotspot."
                )
        else:
            # Connected but not providing internet - still warn unless concurrent
            if supports_concurrency:
                warnings.append(
                    f"🎯 STA+AP Concurrent Mode: Connection to '{connection_name}' will be maintained."
                )
            else:
                warnings.append(
                    f"Interface {target_iface} is connected to '{connection_name}'. "
                    f"It will be disconnected to start the hotspot."
            )
    
    # 10. Check for internet connectivity
    if not upstream and not has_alternative_internet:
        warnings.append(
            "No active internet connection detected. Hotspot clients will not have internet access unless you connect later."
        )
    
    # 11. Check 5GHz band support if selected
    if band == 'a':  # 5GHz
        if not target_iface_info.get('supports_5ghz'):
            errors.append(
                f"Interface {target_iface} does not support 5GHz band.\n"
                f"Use 2.4GHz band instead, or use a 5GHz-capable adapter."
            )
    
    # 12. Validate SSID
    if ssid:
        if len(ssid) < 1 or len(ssid) > 32:
            errors.append("SSID must be between 1 and 32 characters.")
        if any(ord(c) > 127 for c in ssid):
            warnings.append("SSID contains non-ASCII characters. Some devices may not display it correctly.")
    
    # 13. Validate password
    if password:
        if len(password) < 8:
            errors.append("Password must be at least 8 characters for WPA2 security.")
        elif len(password) > 63:
            errors.append("Password must not exceed 63 characters.")
    
    # 14. Check for conflicting hotspot processes
    try:
        result = subprocess.run(['pgrep', '-f', 'hotspot_backend.py'], capture_output=True, text=True)
        if result.stdout.strip():
            pids = [p for p in result.stdout.strip().split('\n') if p and int(p) != os.getpid()]
            if pids:
                warnings.append(f"Another hotspot backend may be running (PIDs: {', '.join(pids)}). It will be terminated.")
    except:
        pass
    
    # 15. Check for interface issues
    if target_iface_info.get('issues'):
        for issue in target_iface_info['issues']:
            if 'No AP' in issue:
                pass  # Already handled above
            else:
                warnings.append(f"Interface issue: {issue}")
    
    if errors:
        return False, "\n".join(errors), warnings
    
    return True, None, warnings





def get_upstream_interface(exclude_vpn=False):
    """
    Finds the interface providing internet using kernel routing decision.
    """
    try:
        # standard mode: trust the kernel (includes VPN if active)
        if not exclude_vpn:
            # Try multiple targets to determine true default route
            for target in ['1.1.1.1', '8.8.8.8']:
                output = run_command(['ip', 'route', 'get', target], check=False)
                # Output: 1.1.1.1 via 192.168.1.1 dev enp3s0 src ...
                if output:
                    match = re.search(r'dev\s+(\S+)', output)
                    if match:
                        return match.group(1)
            # Fallback for standard mode - still don't filter VPN
            output = run_command(['ip', '-4', 'route', 'show', 'default'], check=False)
            if output:
                for line in output.splitlines():
                    match = re.search(r'dev\s+(\S+)', line)
                    if match:
                        return match.group(1)
            return None
        
        # exclude_vpn mode: Manually hunt for physical default route
        output = run_command(['ip', '-4', 'route', 'show', 'default'], check=False)
        candidates = []
        if output:
            for line in output.splitlines():
                match = re.search(r'dev\s+(\S+)', line)
                if match: candidates.append(match.group(1))
        
        vpn_prefixes = ('tun', 'tap', 'wg', 'ppp')
        filtered = [dev for dev in candidates if not dev.startswith(vpn_prefixes)]
        
        if filtered: return filtered[0]
        if candidates: return candidates[0]
            
    except: pass
    return None

def get_smart_interface(exclude_vpn=False, manual_internet_iface=None):
    """Selects the best available Wi-Fi interface for hotspot."""
    # Use the sophisticated selection logic which prioritizes USB adapters
    _, hotspot_iface, reason = get_smart_interface_selection(manual_internet_iface)
    
    if hotspot_iface:
        print(f"Smart Select: {reason}")
        return hotspot_iface
    
    # Fallback to legacy method if sophisticated selection returns nothing
    wifi_devs = get_wifi_interfaces()
    if not wifi_devs: return None
    
    upstream = get_upstream_interface(exclude_vpn)
    candidates = []
    # prioritising disconnected devices to avoid conflict
    for dev in wifi_devs:
        if dev != upstream:
            candidates.append(dev)
            
    if candidates:
        return candidates[0]
    
    return wifi_devs[0]

def count_connected_clients(iface):
    try:
        output = run_command(['iw', 'dev', iface, 'station', 'dump'], check=False)
        if output: return output.count("Station")
        return 0
    except: return 0

def update_firewall(hotspot_iface, upstream_iface):
    print(f"-> Routing update: Internet from {upstream_iface} -> Hotspot on {hotspot_iface}")

    run_command(['sysctl', '-w', 'net.ipv4.ip_forward=1'], check=False)
    # Disable Reverse Path Filtering - Critical for VPN + Hotspot routing
    run_command(['sysctl', '-w', 'net.ipv4.conf.all.rp_filter=0'], check=False)
    run_command(['sysctl', '-w', 'net.ipv4.conf.default.rp_filter=0'], check=False)
    try:
        run_command(['sysctl', '-w', f'net.ipv4.conf.{upstream_iface}.rp_filter=0'], check=False)
        run_command(['sysctl', '-w', f'net.ipv4.conf.{hotspot_iface}.rp_filter=0'], check=False)
    except: pass
    
    run_command(['iptables', '-t', 'nat', '-F', 'POSTROUTING'], check=False)
    run_command(['iptables', '-F', 'FORWARD'], check=False)
    
    run_command([
        'iptables', '-t', 'nat', '-I', 'POSTROUTING', 
        '-o', upstream_iface, '-j', 'MASQUERADE'
    ], check=False)

    # Force Policy to ACCEPT (Fixes Docker/Firewalld interference)
    run_command(['iptables', '-P', 'FORWARD', 'ACCEPT'], check=False)

    # TCP MSS Clamping - MUST be in mangle table for correctness
    # This fixes issues with packet fragmentation over VPNs and some ISPs
    run_command(['iptables', '-t', 'mangle', '-F', 'FORWARD'], check=False)
    run_command([
        'iptables', '-t', 'mangle', '-I', 'FORWARD', '1', 
        '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', 
        '-j', 'TCPMSS', '--clamp-mss-to-pmtu'
    ], check=False)

    # Insert forwarding rules in filter table
    run_command(['iptables', '-I', 'FORWARD', '1', '-m', 'state', '--state', 'RELATED,ESTABLISHED', '-j', 'ACCEPT'], check=False)
    run_command(['iptables', '-I', 'FORWARD', '1', '-i', upstream_iface, '-o', hotspot_iface, '-j', 'ACCEPT'], check=False)
    
    if MAC_MODE == "allow":
        run_command(['iptables', '-A', 'FORWARD', '-i', hotspot_iface, '-j', 'DROP'], check=False)
        for mac in MAC_LIST:
            run_command([
                'iptables', '-I', 'FORWARD', '1', 
                '-i', hotspot_iface, '-o', upstream_iface, 
                '-m', 'mac', '--mac-source', mac, '-j', 'ACCEPT'
            ], check=False)
        
    elif MAC_MODE == "block":
        # Allow main flow (inserted at 1, so before drops if any)
        run_command(['iptables', '-I', 'FORWARD', '1', '-i', hotspot_iface, '-o', upstream_iface, '-j', 'ACCEPT'], check=False)
        for mac in MAC_LIST:
            run_command([
                'iptables', '-I', 'FORWARD', '1', 
                '-i', hotspot_iface, '-o', upstream_iface, 
                '-m', 'mac', '--mac-source', mac, '-j', 'DROP'
            ], check=False)

def cleanup(signal_received=None, frame=None):
    global VIRTUAL_AP_IFACE, USING_CONCURRENCY, HOTSPOT_IFACE
    print("\n\nStopping hotspot...")
    
    # 1. Stop hostapd/dnsmasq if running (concurrent mode)
    stop_concurrent_mode()
    
    # 2. Clean Firewall
    run_command(['iptables', '-t', 'nat', '-F', 'POSTROUTING'], check=False)
    run_command(['iptables', '-t', 'mangle', '-F', 'FORWARD'], check=False) # Clean mangle too
    run_command(['iptables', '-D', 'FORWARD', '-m', 'state', '--state', 'RELATED,ESTABLISHED', '-j', 'ACCEPT'], check=False)
    
    # 3. Delete the hotspot connection (NetworkManager mode)
    run_command(['nmcli', 'con', 'delete', CONNECTION_NAME], check=False)
    
    # 4. Clean up virtual interface if we were using concurrent mode
    if USING_CONCURRENCY and VIRTUAL_AP_IFACE:
        print(f"Cleaning up virtual interface: {VIRTUAL_AP_IFACE}")
        subprocess.run(['iw', 'dev', VIRTUAL_AP_IFACE, 'del'], check=False)
        VIRTUAL_AP_IFACE = None
        USING_CONCURRENCY = False
    elif HOTSPOT_IFACE:
        # Also try to clean up any orphaned virtual interfaces
        delete_virtual_ap_interface(HOTSPOT_IFACE)
    
    # 5. Kill PID file
    if os.path.exists(PID_FILE): os.remove(PID_FILE)
    sys.exit(0)

def main():
    global HOTSPOT_IFACE, CURRENT_UPSTREAM_IFACE, MAC_MODE, MAC_LIST, EXCLUDE_VPN
    
    if subprocess.call(["id", "-u"], stdout=subprocess.PIPE) != 0:
        print("Run with sudo.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description='Hotspot Backend Service')
    parser.add_argument('--interface', help='Wi-Fi interface')
    parser.add_argument('--ssid', default=DEFAULT_HOTSPOT_NAME)
    parser.add_argument('--password', default=DEFAULT_PASSWORD)
    parser.add_argument('--band', default='bg', choices=['bg', 'a'])
    parser.add_argument('--hidden', action='store_true')
    parser.add_argument('--dns', help='Custom DNS')
    parser.add_argument('--mac-mode', default="block", choices=["block", "allow"])
    parser.add_argument('--block', action='append')
    parser.add_argument('--allow', action='append')
    parser.add_argument('--auto-off', type=int, default=0)
    parser.add_argument('--exclude-vpn', action='store_true', help='Avoid routing through VPN interfaces')
    parser.add_argument('--force-single-interface', action='store_true', 
                        help='Force hotspot on single WiFi interface even if it will disconnect internet')
    parser.add_argument('--internet-interface', help='Explicitly set upstream internet interface')
    parser.add_argument('--stop', action='store_true')
    
    args = parser.parse_args()

    if args.stop:
        print("Stopping hotspot...")
        
        # 1. Try to stop via PID file first
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
                # Wait for process to actually terminate
                for _ in range(10):
                    try:
                        os.kill(pid, 0)  # Check if still alive
                        time.sleep(0.5)
                    except OSError:
                        break  # Process terminated
            except Exception as e:
                print(f"PID-based stop failed: {e}")
        
        # 2. Fallback: Kill any remaining backend processes (excluding ourselves)
        my_pid = os.getpid()
        try:
            result = subprocess.run(['pgrep', '-f', 'hotspot_backend.py'], 
                                    capture_output=True, text=True)
            if result.stdout:
                for pid_str in result.stdout.strip().split('\n'):
                    try:
                        pid = int(pid_str)
                        if pid != my_pid:
                            os.kill(pid, signal.SIGTERM)
                    except (ValueError, OSError):
                        pass
        except Exception:
            pass
        
        # 3. Stop hostapd/dnsmasq if running (concurrent mode)
        stop_concurrent_mode()
        
        # 4. Always clean up the nmcli connection
        run_command(['nmcli', 'con', 'delete', CONNECTION_NAME], check=False)
        
        # 5. Clean up firewall rules
        run_command(['iptables', '-t', 'nat', '-F', 'POSTROUTING'], check=False)
        run_command(['iptables', '-D', 'FORWARD', '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', '-j', 'TCPMSS', '--clamp-mss-to-pmtu'], check=False)
        run_command(['iptables', '-D', 'FORWARD', '-m', 'state', '--state', 'RELATED,ESTABLISHED', '-j', 'ACCEPT'], check=False)
        
        # 6. Clean up any virtual AP interfaces (for STA+AP concurrent mode)
        wifi_interfaces = get_wifi_interfaces()
        for iface in wifi_interfaces:
            virtual_iface = f"{iface}_ap"
            result = subprocess.run(['ip', 'link', 'show', virtual_iface], 
                                   capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Cleaning up virtual interface: {virtual_iface}")
                subprocess.run(['iw', 'dev', virtual_iface, 'del'], check=False)
        
        # 7. Remove PID file
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        
        print("Hotspot stopped.")
        sys.exit(0)
    
    MAC_MODE = args.mac_mode
    if MAC_MODE == "allow" and args.allow: MAC_LIST = args.allow
    elif MAC_MODE == "block" and args.block: MAC_LIST = args.block
    
    EXCLUDE_VPN = args.exclude_vpn

    # =============================================
    # PRE-FLIGHT VALIDATION
    # =============================================
    print("Running pre-flight checks...")
    success, error_msg, warnings_list = preflight_checks(
        interface=args.interface,
        ssid=args.ssid,
        password=args.password,
        exclude_vpn=EXCLUDE_VPN,
        force_single_interface=args.force_single_interface,
        band=args.band
    )
    
    # Print warnings (non-fatal)
    for warning in warnings_list:
        print(f"[!] {warning}")
    
    # If errors, abort with clear message and notify GUI
    if not success:
        print(f"\n[ERROR] Cannot start hotspot:\n{error_msg}")
        write_status("error", error_msg, is_error=True)
        sys.exit(1)
    
    print("Pre-flight checks passed.\n")

    with open(PID_FILE, 'w') as f: f.write(str(os.getpid()))
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    # SMART SELECTION LOGIC with STA+AP Concurrency Support
    physical_iface = None
    actual_hotspot_iface = None  # The interface we'll actually use for the hotspot
    
    if args.interface: 
        HOTSPOT_IFACE = args.interface
        physical_iface = args.interface
    else:
        # Use new smart selector with manual internet interface if provided
        HOTSPOT_IFACE = get_smart_interface(EXCLUDE_VPN, manual_internet_iface=args.internet_interface)
        physical_iface = HOTSPOT_IFACE
        if not HOTSPOT_IFACE:
            print("Error: No Wi-Fi interfaces found.")
            cleanup()
    
    print(f"Selected Hotspot Interface: {HOTSPOT_IFACE}")
    
    # Check if this interface supports STA+AP concurrency AND is currently connected
    all_ifaces = get_detailed_interfaces()
    target_iface_info = None
    for iface in all_ifaces:
        if iface['name'] == physical_iface:
            target_iface_info = iface
            break
    
    supports_concurrency = target_iface_info and target_iface_info.get('supports_concurrency', False)
    is_connected = target_iface_info and target_iface_info.get('connected', False)
    hostapd_available = check_hostapd_available() and check_dnsmasq_available()
    
    # Check if we're using a SEPARATE adapter for hotspot (dual-adapter mode)
    # In dual-adapter mode, NO regulatory restrictions apply - each adapter is independent
    internet_iface = get_upstream_interface(EXCLUDE_VPN)
    
    # Check if upstream is physically separate
    # Virtual interfaces (VPNs) are NOT separate adapters.
    is_separate_physical = False
    if internet_iface and internet_iface != physical_iface:
        is_separate_physical = is_physical_interface(internet_iface)
        print(f"Upstream {internet_iface} is physical? {is_separate_physical}")

    using_separate_adapter = is_separate_physical
    
    print(f"Decision: Dual Adapter={using_separate_adapter}, Concurrency={supports_concurrency}, Connected={is_connected}")
    
    if supports_concurrency and is_connected and hostapd_available:
        # === STA+AP CONCURRENT MODE (same adapter) ===
        # This is where regulatory restrictions might apply
        print(f"🎯 Using STA+AP Concurrent Mode - WiFi connection will be preserved!")
        connection_name = target_iface_info.get('connection_name', 'current network')
        print(f"   Maintaining connection to: {connection_name}")
        
        # Get channel logic for concurrent mode
        concurrency_channels = target_iface_info.get('concurrency_channels', 1)
        
        if concurrency_channels >= 2:
             # Dual-channel support (rare but possible, or VIRTUAL_AP logic)
             # User said it worked before, so we TRUST their preference first
             print(f"   Device supports multi-channel concurrency (channels: {concurrency_channels})")
             if args.band == 'a':
                 print("   User requested 5GHz band")
                 # If user explicitly wants 5GHz, check if we can use a different 5GHz channel? 
                 # Usually dual-band means 2.4 + 5. It's safer to stick to different bands if possible.
                 # But for now, let's just try to generate a channel for the requested band.
                 # Actually, usually getting a clean channel is better.
                 channel = get_best_channel(iface=physical_iface, band='a')
             else:
                 print("   User requested 2.4GHz band (default)")
                 channel = get_best_channel(iface=physical_iface, band='bg')
             
             print(f"   Selected channel: {channel} (Cross-band concurrency attempt)")
             
        else:
             # Single-channel limit: MUST match the physical channel
             channel = get_wifi_channel(physical_iface)
             print(f"   Single-channel device: Forcing hotspot to match STA channel: {channel}")
         
        print(f"   Hotspot channel: {channel}")
        
        # Check if 5GHz AP is allowed on this channel (only for same-adapter concurrent)
        ap_allowed, reason = check_5ghz_ap_allowed(channel, physical_iface)
        if not ap_allowed:
            print(f"⚠️ 5GHz AP restriction detected: {reason}")
            print("   Attempting regulatory bypass...")
            attempt_regulatory_bypass()
            # Note: We'll try anyway and let hostapd tell us if it fails
            print(f"   ⚠️ NOTICE: 5GHz channel {channel} may have regulatory restrictions.")
            write_status("warning", f"5GHz restriction detected. Attempting anyway...")
        
        # Create virtual AP interface
        virtual_iface = create_virtual_ap_interface(physical_iface)
        if virtual_iface:
            VIRTUAL_AP_IFACE = virtual_iface
            USING_CONCURRENCY = True
            actual_hotspot_iface = virtual_iface
            print(f"   Hotspot will run on virtual interface: {virtual_iface}")
            
            # Tell NetworkManager to not manage this interface
            subprocess.run(['nmcli', 'device', 'set', virtual_iface, 'managed', 'no'], 
                          capture_output=True, check=False)
            
            # Determine upstream interface for NAT
            # Use dynamic routing to support VPNs, instead of hardcoding to physical_iface
            upstream_candidate = get_upstream_interface(exclude_vpn=args.exclude_vpn)
            upstream = upstream_candidate if upstream_candidate else physical_iface
            
            # Generate configs with regulatory settings
            generate_hostapd_config(virtual_iface, args.ssid, args.password, 
                                   channel=channel, band=args.band, hidden=args.hidden,
                                   country_code='IN')  # India regulatory domain
            dnsmasq_conf, gateway_ip = generate_dnsmasq_config(virtual_iface, args.dns)
            
            # Setup network
            setup_concurrent_ap_network(virtual_iface, gateway_ip, upstream)
            
            # Start services
            if not start_hostapd(HOSTAPD_CONF):
                # hostapd failed - DO NOT fall back to disconnecting WiFi
                print("\n❌ STA+AP Concurrent Mode Failed!")
                print("   hostapd could not start the hotspot on the virtual interface.")
                if channel > 14:
                    print(f"   Likely cause: 5GHz channel {channel} has NO-IR restriction")
                    print("   Your WiFi driver enforces regulatory compliance.\n")
                    print("   💡 SOLUTIONS:")
                    print("   1. Connect to a 2.4GHz WiFi network (e.g., your router's 2.4GHz band)")
                    print("   2. Use a second USB WiFi adapter for the hotspot")
                else:
                    print("   Check system logs: journalctl -xe | grep hostapd")
                
                # Clean up and exit with error - DO NOT FALL BACK TO DISCONNECTING WIFI
                stop_concurrent_mode()
                delete_virtual_ap_interface(physical_iface)
                write_status("error", "STA+AP mode failed. Try 2.4GHz or use second adapter.", is_error=True)
                if os.path.exists(PID_FILE): os.remove(PID_FILE)
                sys.exit(1)
            else:
                start_dnsmasq(dnsmasq_conf)
                
                print(f"\n🎯 Hotspot ACTIVE on {actual_hotspot_iface} (STA+AP Concurrent Mode)")
                print(f"   WiFi connection preserved on {physical_iface}")
                print(f"   Hotspot SSID: {args.ssid}")
                print(f"   Gateway IP: {gateway_ip}")
                write_status("active", f"Hotspot '{args.ssid}' active (STA+AP mode) - WiFi preserved")
        else:
            # Failed to create virtual interface - DO NOT fall back to disconnecting WiFi
            print(f"\n❌ Failed to create virtual interface!")
            print("   Cannot use STA+AP concurrent mode without virtual interface.")
            print("   Your WiFi will NOT be disconnected.")
            write_status("error", "Failed to create virtual AP interface", is_error=True)
            if os.path.exists(PID_FILE): os.remove(PID_FILE)
            sys.exit(1)

    elif using_separate_adapter:
        # === DUAL ADAPTER MODE ===
        # Using different WiFi/Ethernet for internet - hotspot adapter is independent
        print(f"🔌 Dual Adapter Mode: Hotspot on {physical_iface}, Internet via {internet_iface}")
        actual_hotspot_iface = physical_iface
        USING_CONCURRENCY = False
        # Fall through to standard NetworkManager setup below
            
    elif is_connected and not hostapd_available:
        # Connected to WiFi but hostapd not installed - can't do concurrent mode
        print("\n❌ Cannot use STA+AP concurrent mode!")
        print("   hostapd and/or dnsmasq are not installed.")
        print("   Run the installer again: sudo bash install.sh")
        print("   Your WiFi will NOT be disconnected.")
        write_status("error", "hostapd/dnsmasq not installed - run installer", is_error=True)
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        sys.exit(1)
        
    elif is_connected and not supports_concurrency:
        # Connected but adapter doesn't support concurrency
        print("\n❌ Your WiFi adapter doesn't support STA+AP concurrent mode!")
        print("   Consider using a second USB WiFi adapter for the hotspot.")
        print("   Your WiFi will NOT be disconnected.")
        write_status("error", "Adapter doesn't support concurrent mode", is_error=True)
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
        sys.exit(1)
        
    else:
        # NOT connected to WiFi - standard mode is fine (nothing to disconnect)
        actual_hotspot_iface = physical_iface
        USING_CONCURRENCY = False
    
    # If not using concurrent mode (or it failed), use NetworkManager
    if not USING_CONCURRENCY:
        try:
            ensure_wifi_active(physical_iface)
            run_command(['nmcli', 'device', 'disconnect', physical_iface], check=False)
            
            # Clean up any existing hotspot connection
            run_command(['nmcli', 'con', 'delete', CONNECTION_NAME], check=False)

            print(f"Creating Hotspot '{args.ssid}' on {actual_hotspot_iface}...")
            run_command([
                'nmcli', 'con', 'add', 'type', 'wifi', 'ifname', actual_hotspot_iface, 
                'con-name', CONNECTION_NAME, 'autoconnect', 'no', 
                'ssid', args.ssid, 'mode', 'ap', '802-11-wireless.band', args.band
            ])

            if args.hidden: run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, '802-11-wireless.hidden', 'yes'])

            run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, 'wifi-sec.key-mgmt', 'wpa-psk'])
            run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, 'wifi-sec.psk', args.password])
            run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, 'ipv4.method', 'shared'])
            
            if args.dns:
                run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, 'ipv4.dns', args.dns])
                run_command(['nmcli', 'con', 'modify', CONNECTION_NAME, 'ipv4.ignore-auto-dns', 'yes'])

            print("Activating hotspot...")
            run_command(['nmcli', 'con', 'up', CONNECTION_NAME, 'ifname', actual_hotspot_iface])
            
            # Fix MTU/Speed issues (MSS Clamping) - Global fix for all modes (Standard + Concurrent)
            run_command(['iptables', '-A', 'FORWARD', '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', 
                         '-j', 'TCPMSS', '--clamp-mss-to-pmtu'], check=False)
            
            print(f"\nHotspot ACTIVE on {actual_hotspot_iface}")
            write_status("active", f"Hotspot '{args.ssid}' is now active on {actual_hotspot_iface}")
        except Exception as e:
            print(f"Error starting NetworkManager hotspot: {e}")
            cleanup()
    
    # === MONITORING LOOP (for both concurrent and standard modes) ===
    print("Monitoring internet source...")
    
    try:
        idle_seconds = 0
        check_counter = 0
        while True:
            # Continually ensure we are using the correct upstream (changes if VPN connects/disconnects)
            new_upstream = get_upstream_interface(EXCLUDE_VPN)
            
            if new_upstream and new_upstream != CURRENT_UPSTREAM_IFACE:
                if new_upstream != actual_hotspot_iface:
                    if USING_CONCURRENCY:
                        # For concurrent mode, we already set up NAT - just track changes
                        pass
                    else:
                        update_firewall(actual_hotspot_iface, new_upstream)
                    CURRENT_UPSTREAM_IFACE = new_upstream
            
            # Auto-off check: only check clients every 5 seconds to reduce overhead
            if args.auto_off > 0:
                check_counter += 1
                if check_counter >= 5:
                    check_counter = 0
                    if USING_CONCURRENCY:
                        # For hostapd mode, count clients from lease file
                        try:
                            if os.path.exists(DNSMASQ_LEASES):
                                with open(DNSMASQ_LEASES, 'r') as f:
                                    clients = len([l for l in f.readlines() if l.strip()])
                            else:
                                clients = 0
                        except:
                            clients = 0
                    else:
                        clients = count_connected_clients(actual_hotspot_iface)
                    
                    if clients == 0: 
                        idle_seconds += 5
                    else: 
                        idle_seconds = 0
                    if idle_seconds >= (args.auto_off * 60):
                        print("Auto-off trigger.")
                        cleanup()

            time.sleep(1)

    except Exception as e:
        print(f"Error in monitoring loop: {e}")
        cleanup()

if __name__ == "__main__":
    main()
