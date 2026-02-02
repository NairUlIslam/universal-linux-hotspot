import sys
import os
import json
import time
import subprocess
import re
import qrcode
from PIL import Image
from PyQt6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QDialog, 
                             QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, 
                             QPushButton, QMessageBox, QWidget, QListWidget, QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
                             QCheckBox, QListWidgetItem, QInputDialog, QTabWidget, QRadioButton, QButtonGroup)
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QImage
from PyQt6.QtCore import QTimer, Qt, QSharedMemory
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

# Config
CONFIG_FILE = os.path.expanduser("~/.config/hotspot_gui_config.json")
# Use Wrapper script for Sudoers fix
BACKEND_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "run_backend.sh"))
PID_FILE = "/tmp/hotspot_backend.pid"
ICON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "icon.png"))
SOCKET_NAME = "hotspot_gui_socket"
STATUS_FILE = "/tmp/hotspot_status.json"

# Stylesheets
DARK_THEME = """
QWidget {
    background-color: #2b2b2b;
    color: #e0e0e0;
}
QLineEdit, QComboBox, QSpinBox, QListWidget, QTableWidget {
    background-color: #3b3b3b;
    border: 1px solid #555;
    padding: 5px;
    color: #fff;
    selection-background-color: #0078d7;
}
QPushButton {
    background-color: #0078d7;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
}
QPushButton:hover {
    background-color: #0063b1;
}
QHeaderView::section {
    background-color: #3b3b3b;
    padding: 4px;
    border: 1px solid #555;
}
"""

class SettingsManager:
    def __init__(self):
        self.config = {
            "ssid": "MintHotspot",
            "password": "password123",
            "interface": None,
            "band": "bg",
            "auto_off": 0,
            "hidden": False,
            "dns": "",
            "mac_mode": "block",
            "blocked_macs": [],
            "allowed_macs": [],
            "dark_mode": False,
            "route_vpn": True
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.config.update(json.load(f))
            except:
                pass

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f)

    def get(self, key):
        return self.config.get(key)

    def set(self, key, value):
        self.config[key] = value
        self.save()

class QRCodeDialog(QDialog):
    def __init__(self, ssid, password):
        super().__init__()
        self.setWindowTitle("Wi-Fi QR Code")
        self.ssid = ssid
        self.password = password
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        wifi_uri = f"WIFI:S:{self.ssid};T:WPA;P:{self.password};;"
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(wifi_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        tmp_path = "/tmp/hotspot_qr.png"
        img.save(tmp_path)
        
        label = QLabel()
        pixmap = QPixmap(tmp_path)
        label.setPixmap(pixmap)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        
        info = QLabel(f"Scan to connect to '{self.ssid}'")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)
        self.setLayout(layout)

class MacFilterDialog(QDialog):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Manage Devices (MAC Filter)")
        self.setMinimumSize(400, 400)
        self.allowed = self.settings.get("allowed_macs") or []
        self.blocked = self.settings.get("blocked_macs") or []
        self.mode = self.settings.get("mac_mode") or "block"
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        
        mode_group = QButtonGroup(self)
        mode_layout = QHBoxLayout()
        self.rb_block = QRadioButton("Blocklist Mode (Allow all except blocked)")
        self.rb_allow = QRadioButton("Allowlist Mode (Block all except allowed)")
        mode_group.addButton(self.rb_block)
        mode_group.addButton(self.rb_allow)
        
        if self.mode == "block": self.rb_block.setChecked(True)
        else: self.rb_allow.setChecked(True)
            
        mode_layout.addWidget(self.rb_block)
        mode_layout.addWidget(self.rb_allow)
        layout.addLayout(mode_layout)
        
        tabs = QTabWidget()
        self.blocked_list = self.create_list_tab(self.blocked)
        self.allowed_list = self.create_list_tab(self.allowed)
        
        tabs.addTab(self.blocked_list, "Blocked Devices")
        tabs.addTab(self.allowed_list, "Allowed Devices")
        layout.addWidget(tabs)
        
        btns = QHBoxLayout()
        add_btn = QPushButton("Add MAC")
        add_btn.clicked.connect(lambda: self.add_mac(tabs.currentIndex()))
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(lambda: self.remove_mac(tabs.currentIndex()))
        
        btns.addWidget(add_btn)
        btns.addWidget(remove_btn)
        layout.addLayout(btns)
        
        close_btn = QPushButton("Save & Close")
        close_btn.clicked.connect(self.save_and_close)
        layout.addWidget(close_btn)
        self.setLayout(layout)
        
    def create_list_tab(self, data):
        list_widget = QListWidget()
        for mac in data: list_widget.addItem(mac)
        return list_widget

    def add_mac(self, index):
        target_list = self.blocked_list if index == 0 else self.allowed_list
        target_data = self.blocked if index == 0 else self.allowed
        mac, ok = QInputDialog.getText(self, "Add MAC", "Enter MAC Address (e.g. AA:BB:CC:DD:EE:FF):")
        if ok and mac:
            mac = mac.strip().upper()
            if re.match(r"^([0-9A-F]{2}[:-]){5}([0-9A-F]{2})$", mac):
                if mac not in target_data:
                    target_data.append(mac); target_list.addItem(mac)
            else: QMessageBox.warning(self, "Invalid MAC", "Please enter a valid MAC address.")

    def remove_mac(self, index):
        target_list = self.blocked_list if index == 0 else self.allowed_list
        target_data = self.blocked if index == 0 else self.allowed
        for item in target_list.selectedItems():
            target_data.remove(item.text()); target_list.takeItem(target_list.row(item))

    def save_and_close(self):
        self.settings.set("allowed_macs", self.allowed)
        self.settings.set("blocked_macs", self.blocked)
        self.settings.set("mac_mode", "block" if self.rb_block.isChecked() else "allow")
        self.accept()

class SettingsDialog(QDialog):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("Hotspot Settings")
        self.setMinimumWidth(450)
        self.interfaces = []
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        
        # === Network Name Section ===
        layout.addWidget(QLabel("<b>Network Name:</b>"))
        self.ssid_input = QLineEdit(self.settings.get("ssid"))
        layout.addWidget(self.ssid_input)
        
        self.hidden_check = QCheckBox("Hidden Network (Stealth)")
        self.hidden_check.setChecked(self.settings.get("hidden") or False)
        layout.addWidget(self.hidden_check)

        layout.addWidget(QLabel("<b>Password:</b>"))
        self.pass_input = QLineEdit(self.settings.get("password"))
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pass_input)

        # === Interface Selection Section ===
        layout.addWidget(QLabel(""))  # Spacer
        layout.addWidget(QLabel("<b>━━━ Interface Configuration ━━━</b>"))
        
        # Smart recommendation label
        self.recommendation_label = QLabel("")
        self.recommendation_label.setStyleSheet("color: #4CAF50; font-style: italic;")
        layout.addWidget(self.recommendation_label)
        
        # Hotspot Interface
        layout.addWidget(QLabel("Hotspot Interface (creates the Wi-Fi network):"))
        self.hotspot_combo = QComboBox()
        self.hotspot_combo.setToolTip("This interface will broadcast the hotspot signal")
        layout.addWidget(self.hotspot_combo)
        
        # Internet Source
        layout.addWidget(QLabel("Internet Source (where to get internet from):"))
        self.internet_combo = QComboBox()
        self.internet_combo.setToolTip("Internet traffic from hotspot clients will be routed through this interface")
        layout.addWidget(self.internet_combo)
        
        # Populate with detailed interface info
        self.populate_interfaces()

        # === Frequency Band ===
        layout.addWidget(QLabel("<b>Frequency Band:</b>"))
        self.band_combo = QComboBox()
        self.band_combo.addItem("2.4 GHz (Long Range, Better Compatibility)", "bg")
        self.band_combo.addItem("5 GHz (High Speed, Less Interference)", "a")
        current_band = self.settings.get("band") or "bg"
        self.band_combo.setCurrentIndex(0 if current_band == "bg" else 1)
        layout.addWidget(self.band_combo)
        
        # === Advanced Options ===
        layout.addWidget(QLabel(""))
        layout.addWidget(QLabel("<b>━━━ Advanced Options ━━━</b>"))
        
        layout.addWidget(QLabel("Custom DNS (Optional):"))
        self.dns_input = QLineEdit(self.settings.get("dns") or "")
        self.dns_input.setPlaceholderText("Leave empty for default (e.g., 1.1.1.1)")
        layout.addWidget(self.dns_input)

        layout.addWidget(QLabel("Auto-off Timer (minutes, 0=Never):"))
        self.timer_spin = QSpinBox()
        self.timer_spin.setRange(0, 120)
        self.timer_spin.setValue(self.settings.get("auto_off") or 0)
        layout.addWidget(self.timer_spin)
        
        # VPN Toggle
        self.vpn_check = QCheckBox("Route Client Traffic via VPN (if active)")
        is_vpn = self.settings.get("route_vpn")
        if is_vpn is None: is_vpn = True
        self.vpn_check.setChecked(is_vpn)
        self.vpn_check.setToolTip("If unchecked, hotspot clients will bypass VPN and use direct internet.")
        layout.addWidget(self.vpn_check)
        
        # MAC Filter button
        mac_btn = QPushButton("Manage Devices (Block/Allow)")
        mac_btn.clicked.connect(self.show_mac_filter)
        layout.addWidget(mac_btn)

        # Save button
        save_btn = QPushButton("Save Settings")
        save_btn.clicked.connect(self.save_settings)
        layout.addWidget(save_btn)
        
        self.setLayout(layout)

    def get_detailed_interfaces(self):
        """Get detailed interface info by calling backend discovery."""
        interfaces = []
        try:
            # Try to import from backend if in same directory
            import importlib.util
            backend_path = os.path.join(os.path.dirname(__file__), "hotspot_backend.py")
            if os.path.exists(backend_path):
                spec = importlib.util.spec_from_file_location("hotspot_backend", backend_path)
                backend = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(backend)
                interfaces = backend.get_detailed_interfaces()
        except Exception as e:
            print(f"Could not load interface details: {e}")
        
        # Fallback to basic nmcli if backend import fails
        if not interfaces:
            interfaces = self.get_basic_interfaces()
        
        return interfaces
    
    def get_basic_interfaces(self):
        """Fallback basic interface discovery."""
        interfaces = []
        try:
            output = subprocess.check_output(
                ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], 
                text=True
            )
            for line in output.splitlines():
                parts = line.split(':')
                if len(parts) >= 3:
                    dev_name = parts[0]
                    dev_type = parts[1]
                    dev_state = parts[2]
                    dev_conn = parts[3] if len(parts) > 3 else ""
                    
                    if dev_name.startswith(('lo', 'docker', 'br-', 'veth', 'p2p-')):
                        continue
                    if dev_type == 'wifi-p2p':
                        continue
                    
                    label = f"{dev_type.title()} ({dev_name})"
                    if dev_state == 'connected' and dev_conn:
                        label += f" → {dev_conn}"
                    
                    interfaces.append({
                        'name': dev_name,
                        'type': dev_type,
                        'state': dev_state,
                        'connected': dev_state == 'connected',
                        'connection_name': dev_conn if dev_conn and dev_conn != '--' else None,
                        'ap_support': dev_type == 'wifi',  # Assume true for basic
                        'supports_5ghz': False,
                        'is_usb': False,
                        'is_internal': True,
                        'label': label
                    })
        except Exception as e:
            print(f"Basic interface discovery failed: {e}")
        return interfaces

    def populate_interfaces(self):
        self.interfaces = self.get_detailed_interfaces()
        
        # Get smart recommendation
        recommended_internet = None
        recommended_hotspot = None
        recommendation_text = ""
        
        try:
            import importlib.util
            backend_path = os.path.join(os.path.dirname(__file__), "hotspot_backend.py")
            if os.path.exists(backend_path):
                spec = importlib.util.spec_from_file_location("hotspot_backend", backend_path)
                backend = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(backend)
                recommended_internet, recommended_hotspot, recommendation_text = backend.get_smart_interface_selection()
        except:
            pass
        
        if recommendation_text:
            self.recommendation_label.setText(f"💡 {recommendation_text}")
        
        # Populate Hotspot Interface dropdown (WiFi only, with AP support)
        self.hotspot_combo.clear()
        self.hotspot_combo.addItem("Auto-detect (Smart)", None)
        
        wifi_interfaces = [i for i in self.interfaces if i['type'] == 'wifi']
        current_hotspot = self.settings.get("interface")
        hotspot_index = 0
        
        for iface in wifi_interfaces:
            label = iface['label']
            if not iface.get('ap_support'):
                label += " ⚠️ No AP"
            self.hotspot_combo.addItem(label, iface['name'])
            if iface['name'] == current_hotspot:
                hotspot_index = self.hotspot_combo.count() - 1
            elif iface['name'] == recommended_hotspot and hotspot_index == 0:
                hotspot_index = self.hotspot_combo.count() - 1
        
        self.hotspot_combo.setCurrentIndex(hotspot_index)
        
        # Populate Internet Source dropdown (all interfaces)
        self.internet_combo.clear()
        self.internet_combo.addItem("Auto-detect (Smart)", None)
        
        current_internet = self.settings.get("internet_interface")
        internet_index = 0
        
        for iface in self.interfaces:
            if iface['type'] in ('wifi', 'ethernet'):
                label = iface['label']
                self.internet_combo.addItem(label, iface['name'])
                if iface['name'] == current_internet:
                    internet_index = self.internet_combo.count() - 1
                elif iface['name'] == recommended_internet and internet_index == 0:
                    internet_index = self.internet_combo.count() - 1
        
        self.internet_combo.setCurrentIndex(internet_index)

    def show_mac_filter(self): MacFilterDialog(self.settings).exec()

    def save_settings(self):
        self.settings.set("ssid", self.ssid_input.text())
        self.settings.set("password", self.pass_input.text())
        self.settings.set("interface", self.hotspot_combo.currentData())
        self.settings.set("internet_interface", self.internet_combo.currentData())
        self.settings.set("band", self.band_combo.currentData())
        self.settings.set("auto_off", self.timer_spin.value())
        self.settings.set("hidden", self.hidden_check.isChecked())
        self.settings.set("dns", self.dns_input.text().strip())
        self.settings.set("route_vpn", self.vpn_check.isChecked())
        self.accept()

class ConnectedDevicesDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Connected Devices")
        self.setMinimumSize(400, 300)
        self.init_ui()
        self.refresh_devices()

    def init_ui(self):
        layout = QVBoxLayout()
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["IP Address", "MAC Address", "State"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_devices)
        layout.addWidget(refresh_btn)
        self.setLayout(layout)

    def refresh_devices(self):
        self.table.setRowCount(0)
        try:
            output = subprocess.check_output(["ip", "neigh"], text=True)
            row = 0
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 5:
                    ip = parts[0]; mac = "Unknown"; state = parts[-1]
                    if "lladdr" in parts:
                        mac_idx = parts.index("lladdr") + 1
                        if mac_idx < len(parts): mac = parts[mac_idx]
                    self.table.insertRow(row)
                    self.table.setItem(row, 0, QTableWidgetItem(ip))
                    self.table.setItem(row, 1, QTableWidgetItem(mac))
                    self.table.setItem(row, 2, QTableWidgetItem(state))
                    row += 1
        except Exception as e: QMessageBox.warning(self, "Error", f"Could not fetch devices: {e}")

class HotspotTray(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.settings = SettingsManager()
        self.setToolTip("Hotspot Manager")
        self.last_rx = 0; self.last_tx = 0; self.last_time = time.time()
        
        # --- ICON LOADING ---
        self.icon_active = QIcon.fromTheme("network-wireless-hotspot")
        self.icon_inactive = QIcon.fromTheme("network-wireless-disconnected")

        if os.path.exists(ICON_PATH):
            pix = QPixmap(ICON_PATH)
            if not pix.isNull():
                self.icon_active = QIcon(pix)
                img = pix.toImage()
                img = img.convertToFormat(QImage.Format.Format_ARGB32)
                dimmed = QPixmap(pix.size())
                dimmed.fill(Qt.GlobalColor.transparent)
                p = QPainter(dimmed)
                p.setOpacity(0.5) 
                p.drawPixmap(0, 0, pix)
                p.end()
                self.icon_inactive = QIcon(dimmed)

        self.setIcon(self.icon_inactive)
        self.apply_theme()
        
        self.menu = QMenu()
        self.speed_action = QAction("Data: --"); self.speed_action.setEnabled(False); self.menu.addAction(self.speed_action)
        self.status_action = QAction("Status: Stopped"); self.status_action.setEnabled(False); self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.toggle_action = QAction("Start Hotspot"); self.toggle_action.triggered.connect(self.toggle_hotspot); self.menu.addAction(self.toggle_action)
        self.devices_action = QAction("Connected Devices"); self.devices_action.triggered.connect(self.show_devices); self.menu.addAction(self.devices_action)
        self.qr_action = QAction("Show QR Code"); self.qr_action.triggered.connect(self.show_qr); self.menu.addAction(self.qr_action)
        self.menu.addSeparator()
        
        # === Quick Settings Submenu ===
        self.quick_menu = QMenu("Quick Settings")
        
        # VPN Toggle
        self.vpn_action = QAction("Route via VPN")
        self.vpn_action.setCheckable(True)
        vpn_enabled = self.settings.get("route_vpn")
        if vpn_enabled is None: vpn_enabled = True
        self.vpn_action.setChecked(vpn_enabled)
        self.vpn_action.triggered.connect(self.toggle_vpn_routing)
        self.quick_menu.addAction(self.vpn_action)
        
        # Interface Selection Submenu
        self.hotspot_iface_menu = QMenu("Hotspot Interface")
        self.internet_iface_menu = QMenu("Internet Source")
        self.quick_menu.addMenu(self.hotspot_iface_menu)
        self.quick_menu.addMenu(self.internet_iface_menu)
        
        # Populate interface menus
        self.refresh_interface_menus()
        
        self.menu.addMenu(self.quick_menu)
        self.menu.addSeparator()
        
        self.theme_action = QAction("Toggle Dark Mode"); self.theme_action.triggered.connect(self.toggle_theme); self.menu.addAction(self.theme_action)
        self.settings_action = QAction("Settings"); self.settings_action.triggered.connect(self.show_settings); self.menu.addAction(self.settings_action)
        self.exit_action = QAction("Exit"); self.exit_action.triggered.connect(self.exit_app); self.menu.addAction(self.exit_action)
        self.setContextMenu(self.menu)
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_loop)
        self.timer.start(2000) 
        self.update_loop()

        self.socket_server = QLocalServer()
        self.socket_server.removeServer(SOCKET_NAME)
        self.socket_server.listen(SOCKET_NAME)
        self.socket_server.newConnection.connect(self.handle_wake_request)

    def handle_wake_request(self):
        conn = self.socket_server.nextPendingConnection()
        self.show_settings()
        conn.close()

    def apply_theme(self):
        if self.settings.get("dark_mode"): self.app.setStyleSheet(DARK_THEME)
        else: self.app.setStyleSheet("")

    def toggle_theme(self):
        current = self.settings.get("dark_mode")
        self.settings.set("dark_mode", not current)
        self.apply_theme()

    def toggle_vpn_routing(self):
        """Toggle VPN routing from tray menu."""
        current = self.settings.get("route_vpn")
        if current is None: current = True
        self.settings.set("route_vpn", not current)
        self.vpn_action.setChecked(not current)
        status = "enabled" if not current else "disabled"
        self.showMessage("VPN Routing", f"VPN routing {status}. Takes effect on next hotspot start.",
                        QSystemTrayIcon.MessageIcon.Information, 2000)

    def refresh_interface_menus(self):
        """Refresh the interface selection menus in Quick Settings."""
        self.hotspot_iface_menu.clear()
        self.internet_iface_menu.clear()
        
        # Get detailed interfaces using backend discovery
        interfaces = []
        try:
            import importlib.util
            backend_path = os.path.join(os.path.dirname(__file__), "hotspot_backend.py")
            if os.path.exists(backend_path):
                spec = importlib.util.spec_from_file_location("hotspot_backend", backend_path)
                backend = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(backend)
                interfaces = backend.get_detailed_interfaces()
        except Exception as e:
            print(f"Could not load detailed interfaces: {e}")
        
        # Fallback to basic discovery if backend fails
        if not interfaces:
            try:
                output = subprocess.check_output(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
                    text=True, timeout=5
                )
                for line in output.splitlines():
                    parts = line.split(':')
                    if len(parts) >= 3:
                        dev_name, dev_type, dev_state = parts[0], parts[1], parts[2]
                        dev_conn = parts[3] if len(parts) > 3 and parts[3] != '--' else ""
                        
                        if dev_name.startswith(('lo', 'docker', 'br-', 'veth', 'p2p-')):
                            continue
                        if dev_type == 'wifi-p2p':
                            continue
                        
                        label = f"{dev_type.title()} ({dev_name})"
                        if dev_state == 'connected' and dev_conn:
                            label += f" → {dev_conn}"
                        
                        interfaces.append({
                            'name': dev_name,
                            'type': dev_type,
                            'label': label,
                            'connected': dev_state == 'connected',
                            'ap_support': dev_type == 'wifi'
                        })
            except:
                pass
        
        current_hotspot = self.settings.get("interface")
        current_internet = self.settings.get("internet_interface")
        
        # Add Auto option to both
        auto_hotspot = QAction("🤖 Auto-detect (Smart)", self)
        auto_hotspot.setCheckable(True)
        auto_hotspot.setChecked(current_hotspot is None)
        auto_hotspot.triggered.connect(lambda: self.set_interface("hotspot", None))
        self.hotspot_iface_menu.addAction(auto_hotspot)
        self.hotspot_iface_menu.addSeparator()
        
        auto_internet = QAction("🤖 Auto-detect (Smart)", self)
        auto_internet.setCheckable(True)
        auto_internet.setChecked(current_internet is None)
        auto_internet.triggered.connect(lambda: self.set_interface("internet", None))
        self.internet_iface_menu.addAction(auto_internet)
        self.internet_iface_menu.addSeparator()
        
        # Populate Hotspot menu (WiFi only)
        for iface in interfaces:
            if iface.get('type') == 'wifi':
                # Use detailed label from backend
                label = iface.get('label', iface['name'])
                
                # Add AP warning if no support
                if not iface.get('ap_support', True):
                    label += " ⚠️ No AP"
                
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(iface['name'] == current_hotspot)
                action.triggered.connect(lambda checked, n=iface['name']: self.set_interface("hotspot", n))
                self.hotspot_iface_menu.addAction(action)
        
        # Populate Internet menu (all relevant interfaces)
        for iface in interfaces:
            iface_type = iface.get('type', '')
            if iface_type in ('wifi', 'ethernet') or iface.get('is_vpn') or iface.get('is_mobile') or iface.get('is_tethered'):
                # Use detailed label from backend
                label = iface.get('label', iface['name'])
                
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(iface['name'] == current_internet)
                action.triggered.connect(lambda checked, n=iface['name']: self.set_interface("internet", n))
                self.internet_iface_menu.addAction(action)

    def set_interface(self, interface_type, interface_name):
        """Set interface from tray menu."""
        if interface_type == "hotspot":
            self.settings.set("interface", interface_name)
            label = interface_name or "Auto-detect"
            self.showMessage("Hotspot Interface", f"Set to: {label}. Takes effect on next start.",
                            QSystemTrayIcon.MessageIcon.Information, 2000)
        elif interface_type == "internet":
            self.settings.set("internet_interface", interface_name)
            label = interface_name or "Auto-detect"
            self.showMessage("Internet Source", f"Set to: {label}. Takes effect on next start.",
                            QSystemTrayIcon.MessageIcon.Information, 2000)
        # Refresh menus to update checkmarks
        self.refresh_interface_menus()


    def get_net_stats(self, iface):
        try:
            rx_path = f"/sys/class/net/{iface}/statistics/rx_bytes"
            tx_path = f"/sys/class/net/{iface}/statistics/tx_bytes"
            if os.path.exists(rx_path):
                with open(rx_path, 'r') as f: rx = int(f.read())
                with open(tx_path, 'r') as f: tx = int(f.read())
                return rx, tx
        except: pass
        return 0,0

    def calculate_speed(self, rx, tx):
        now = time.time()
        dur = now - self.last_time
        if dur <= 0: return "0 B/s", "0 B/s"
        rx_s = (rx-self.last_rx)/dur; tx_s = (tx-self.last_tx)/dur
        self.last_rx=rx; self.last_tx=tx; self.last_time=now
        def fmt(v):
            for u in ['B/s','KB/s','MB/s']:
                if v<1024: return f"{v:.1f} {u}"
                v /= 1024
            return f"{v:.1f} GB/s"
        return fmt(rx_s), fmt(tx_s)

    def get_active_hotspot_interface(self):
        try:
            output = subprocess.check_output(["nmcli", "-t", "-f", "DEVICE,CONNECTION", "device"], text=True)
            for line in output.splitlines():
                if "temp_hotspot_con" in line:
                    return line.split(":")[0]
        except: pass
        return None

    def check_backend_status(self):
        """Check backend status file and show notifications for errors/events."""
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, 'r') as f:
                    data = json.load(f)
                
                # Only process recent status updates (within last 10 seconds)
                if time.time() - data.get("timestamp", 0) < 10:
                    status = data.get("status", "")
                    message = data.get("message", "")
                    is_error = data.get("is_error", False)
                    
                    # Check if we already showed this notification
                    if not hasattr(self, '_last_status_ts') or self._last_status_ts != data.get("timestamp"):
                        self._last_status_ts = data.get("timestamp")
                        
                        if is_error:
                            self.showMessage("Hotspot Error", message, 
                                           QSystemTrayIcon.MessageIcon.Critical, 5000)
                        elif status == "active":
                            self.showMessage("Hotspot Active", message,
                                           QSystemTrayIcon.MessageIcon.Information, 3000)
        except:
            pass

    def update_loop(self):
        # Check for backend status notifications
        self.check_backend_status()
        
        running = False
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f: pid = int(f.read().strip())
                os.kill(pid, 0)
                running = True
            except PermissionError:
                running = True
            except: pass
        
        if running:
            self.setIcon(self.icon_active)
            self.status_action.setText("Status: Running")
            self.toggle_action.setText("Stop Hotspot")
            self.devices_action.setEnabled(True)
            self.qr_action.setEnabled(True)
            
            iface = self.get_active_hotspot_interface()
            if not iface: iface = self.settings.get("interface")
            
            if iface:
                rx, tx = self.get_net_stats(iface)
                if self.last_rx==0: self.last_rx, self.last_tx = rx, tx
                else: dl, ul = self.calculate_speed(rx, tx); self.speed_action.setText(f"↓ {dl}  ↑ {ul}")
        else:
            self.setIcon(self.icon_inactive)
            self.status_action.setText("Status: Stopped")
            self.toggle_action.setText("Start Hotspot")
            self.devices_action.setEnabled(False)
            self.qr_action.setEnabled(False)
            self.speed_action.setText("Data: --")
            self.last_rx = 0

    def toggle_hotspot(self):
        # UI Responsiveness: Disable immediately and show state
        self.toggle_action.setEnabled(False)
        self.toggle_action.setText("Working...")
        
        running = False
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, 'r') as f: pid=int(f.read().strip()); os.kill(pid, 0); running=True
            except PermissionError:
                running = True  # Process exists but owned by root
            except: pass
        
        if running:
            self.status_action.setText("Status: Stopping...")
            self.run_sudo_command(["sudo", BACKEND_SCRIPT, "--stop"])
        else:
            self.status_action.setText("Status: Starting...")
            s=self.settings; cmd = ["sudo", BACKEND_SCRIPT, 
                   "--ssid", s.get("ssid"), "--password", s.get("password"),
                   "--band", s.get("band") or "bg", "--auto-off", str(s.get("auto_off") or 0),
                   "--mac-mode", s.get("mac_mode") or "block"]
            if s.get("interface"): cmd.extend(["--interface", s.get("interface")])
            if s.get("hidden"): cmd.append("--hidden")
            
            # Check for VPN Routing
            use_vpn = s.get("route_vpn")
            if use_vpn is None: use_vpn = True # Default On
            if not use_vpn:
                cmd.append("--exclude-vpn")

            target_list = s.get("blocked_macs") if s.get("mac_mode") == "block" else s.get("allowed_macs")
            
            current_mode = s.get("mac_mode") or "block"
            if current_mode == "allow" and not target_list:
                print("Safety: Allow Mode with empty list -> Switching to Block Mode (Allow All)")
                current_mode = "block"
                target_list = s.get("blocked_macs")

            flag = "--block" if current_mode == "block" else "--allow"
            cmd.extend(["--mac-mode", current_mode])
            
            if target_list:
                for mac in target_list: cmd.extend([flag, mac])
            
            subprocess.run(["sudo", "pkill", "-f", "hotspot_backend.py"])
            
            subprocess.Popen(cmd)
            self.showMessage("Hotspot", "Starting hotspot...", QSystemTrayIcon.MessageIcon.Information, 2000)

        # Force rapid checks to update UI quickly
        QTimer.singleShot(500, self.update_loop)
        QTimer.singleShot(1500, self.update_loop)
        QTimer.singleShot(3000, self.update_loop)
        
        # Re-enable button after short delay to allow state to settle
        QTimer.singleShot(2000, lambda: self.toggle_action.setEnabled(True))

    def run_sudo_command(self, cmd_list): subprocess.run(cmd_list)
    def show_settings(self): SettingsDialog(self.settings).exec()
    def show_devices(self): ConnectedDevicesDialog().exec()
    def show_qr(self): QRCodeDialog(self.settings.get("ssid"), self.settings.get("password")).exec()
    def exit_app(self): QApplication.quit()

def main():
    app = QApplication(sys.argv)
    socket = QLocalSocket()
    socket.connectToServer(SOCKET_NAME)
    if socket.waitForConnected(500):
        socket.disconnectFromServer()
        sys.exit(0)
    app.setQuitOnLastWindowClosed(False)
    tray = HotspotTray(app)
    tray.show()
    # Auto-open settings on launch - REMOVED for minimized startup
    # tray.show_settings()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
