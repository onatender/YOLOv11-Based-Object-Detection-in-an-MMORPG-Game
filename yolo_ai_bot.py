import sys
import os
import json
import time
import threading
import numpy as np
import mss
import cv2
import pydirectinput
import ctypes
from ultralytics import YOLO
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, QLineEdit, QFrame, QSlider, QCheckBox
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap
from memory_helper import MemoryHelper

# --- YÖNETİCİ KONTROLÜ ---
def is_admin():
    try: return ctypes.windll.shell32.IsUserAnAdmin()
    except: return False
if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()

# --- DPI FARKINDALIĞI (Hatasız Hedefleme İçin) ---
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1) # 1: Process_System_DPI_Aware
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

class BotWorker(QThread):
    update_frame = pyqtSignal(np.ndarray)
    update_log = pyqtSignal(str)
    update_data = pyqtSignal(float, float, float, float, str, bool, int, int, float) # X, Y, HP, ATK, Name, BarOnScreen, VID, EnemyDead, CurrFov

    def __init__(self, model_path):
        super().__init__()
        self.model = YOLO(model_path)
        self.proc_name = "metin2client.bin"
        self.helper = MemoryHelper(self.proc_name) # Merkezi Bellek Erişimi
        self.running = True 
        self.autopilot = False 
        self.is_rotating = False
        self.rotation_start_time = 0.0
        self.stuck_retry_count = 0
        self.curr_x, self.curr_y, self.curr_hp = 0.0, 0.0, 100
        self.last_x, self.last_y, self.last_hp = 0.0, 0.0, 100
        self.mem_is_attacking = 0
        self.curr_name = "Bağlanıyor..."
        self.new_name_to_write = None
        self.curr_fov = 2500.0
        self.target_fov = 2500.0
        self.auto_fov_enabled = False
        self.is_bar_on_screen = False 
        self.target_vid = 0 # Saldırılan hedefin VID adresi
        self.conf_threshold = 0.60 # Varsayılan güven eşiği: %60
        self.is_enemy_dead = 0 # 0: Yok, 1: Canlı, 2: Ölü
        self.max_fov = 10000.0 # Varsayılan Max FOV değeri
        self.show_preview = False # Performans için varsayılan KAPALI
        self.template = cv2.imread("search_for.png") if os.path.exists("search_for.png") else None
        
        # Zamanlayıcılar
        self.last_f_time = time.time()
        self.f_state = False # False: Bırakıldı, True: Basıldı
        self.last_ui_time = 0.0 # UI güncelleme zamanlayıcısı
        self.last_action_time = time.time()
        self.last_state = "IDLE"

    def _memory_scanner(self):
        """Merkezi self.helper Üzerinden Bellek Tarayıcı"""
        while self.running:
            try:
                # 1. Koordinat Okuma
                ptr_pos = self.helper.resolve_pointer(0x0398EF0C, [0x8, 0x20])
                if ptr_pos:
                    self.curr_x = self.helper.read_float(ptr_pos)
                    self.curr_y = self.helper.read_float(ptr_pos + 0x8)
                
                # 2. HP Okuma
                ptr_hp = self.helper.resolve_pointer(0x0398EF0C, [0x8, 0x61C])
                if ptr_hp: self.curr_hp = self.helper.read_int(ptr_hp)
                
                # 3. İsim İşlemleri
                ptr_name = self.helper.resolve_pointer(0x03914B3C, [0x14, 0x10])
                if ptr_name:
                    if self.new_name_to_write:
                        self.helper.write_string(ptr_name, self.new_name_to_write)
                        self.new_name_to_write = None
                    self.curr_name = self.helper.read_string(ptr_name)
                
                # 4. Saldırı Durumu
                ptr_atk = self.helper.resolve_pointer(0x0398EF0C, [0x8, 0x658])
                if ptr_atk: self.mem_is_attacking = self.helper.read_int(ptr_atk)

                # 5. Can Barı
                ptr_bar = self.helper.resolve_pointer(0x0398EF0C, [0x8, 0x6C0])
                if ptr_bar: self.is_bar_on_screen = (self.helper.read_int(ptr_bar) > 0)
                
                # 6. Düşman Durumu (isEnemyDead)
                ptr_dead = self.helper.resolve_pointer(0x039171A8, [0x6C0])
                if ptr_dead: self.is_enemy_dead = self.helper.read_uint(ptr_dead)
                
                # 7. Anlık FOV (setFov) [[[Base+039195F8]+0]+14]+134
                ptr_fov_set = self.helper.resolve_pointer(0x039195F8, [0, 0x14, 0x134])
                if ptr_fov_set:
                    self.curr_fov = self.helper.read_float(ptr_fov_set)
                    # Sabitleme devredeyse HER ZAMAN yaz (Autopilot bekleme)
                    if self.auto_fov_enabled:
                        self.helper.write_float(ptr_fov_set, self.target_fov)
            except: pass
            time.sleep(0.01)

    def run(self):
        # --- BAŞLANGIÇ ---
        device_str = "CUDA (GPU) 🚀" if next(self.model.parameters()).is_cuda else "CPU (SLOW) 🐢"
        self.update_log.emit(f"⚙️ İzleme Başlatıldı: {device_str}")
        
        # --- MAX FOV YAZIMI ---
        try:
            ptr_fov = self.helper.resolve_pointer(0x00A6AC3C, [0])
            if ptr_fov: self.helper.write_float(ptr_fov, self.max_fov)
        except: pass
        
        threading.Thread(target=self._memory_scanner, daemon=True).start()

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self.running:
                now = time.time()
                
                # --- ACİL DURDURMA (F10 - Global) ---
                if ctypes.windll.user32.GetAsyncKeyState(0x79): # F10 tuşu
                    if self.autopilot:
                        self.autopilot = False
                        self.update_log.emit("🛑 ACİL DURDURMA: Otonom Pilot kapatıldı.")
                
                sct_img = sct.grab(monitor)
                frame = np.frombuffer(sct_img.bgra, dtype=np.uint8).reshape(sct_img.height, sct_img.width, 4)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                
                results = self.model(frame, conf=self.conf_threshold, device='0', half=True, imgsz=640, verbose=False)
                targets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = box.xyxy[0]
                        cx, cy_mid = int((x1+x2)/2), int((y1+y2)/2)
                        targets.append((cx, cy_mid))
                        cv2.circle(frame, (cx, cy_mid), int(max(x2-x1, y2-y1)/1.6), (0, 0, 255), 2)

                # UI GÜNCELLEME
                if now - self.last_ui_time > 0.04:
                    if self.show_preview:
                        self.update_frame.emit(frame)
                    self.update_data.emit(self.curr_x, self.curr_y, float(self.curr_hp), float(self.mem_is_attacking), self.curr_name, self.is_bar_on_screen, self.target_vid, self.is_enemy_dead, self.curr_fov)
                    self.last_ui_time = now
                
                if not self.autopilot:
                    for k in ['f', 'q', 't', 'g']: pydirectinput.keyUp(k)
                    time.sleep(0.01); continue
                
                # --- OTONOM BOT MANTIĞI ---
                is_empty = len(targets) == 0
                is_static = abs(self.curr_x - self.last_x) < 0.001

                # 1. STUCK RECOVERY (Mücadele Öncelikli)
                if (self.last_state in ["MOVING_TO_METIN", "MOVING"]) and is_static and (now - self.last_action_time > 2.5):
                    actions = ['space', 's', 'a', 'd']
                    key = actions[self.stuck_retry_count % len(actions)]
                    self.update_log.emit(f"⚠️ Engel! Hamle: {key.upper()}")
                    pydirectinput.keyDown(key); time.sleep(0.5); pydirectinput.keyUp(key)
                    pydirectinput.press('3'); self.stuck_retry_count += 1
                    self.last_action_time = now; self.last_state = "IDLE"; continue
                    
                if not is_static: self.stuck_retry_count = 0

                # 2. HEDEF VE SALDIRI DURUMU
                # is_enemy_dead: 1 (Canlı), 2 (Ölü), 0 (Yok)
                if self.is_enemy_dead == 1:
                    if self.mem_is_attacking == 1:
                        if self.last_state != "ATTACKING":
                            self.update_log.emit("⚔️ Metin Kesiliyor...")
                            self.last_state = "ATTACKING"
                    else:
                        if self.last_state != "MOVING_TO_METIN":
                            self.update_log.emit("🏃 Metne Gidiliyor...")
                            self.last_state = "MOVING_TO_METIN"
                elif self.is_enemy_dead == 2 or (not self.is_bar_on_screen and self.last_state == "ATTACKING"):
                    self.update_log.emit("✅ Metin bitti, yeni hedef aranıyor")
                    self.last_state = "IDLE"; self.is_enemy_dead = 0
                
                # 3. HAREKET VE ARAMA
                if not is_empty:
                    # Hedef varsa her türlü rotasyonu durdur
                    if self.is_rotating: 
                        for k in ['q', 't', 'g']: pydirectinput.keyUp(k)
                        self.is_rotating = False
                    
                    if self.last_state == "IDLE":
                        tx, ty = targets[0]
                        abs_x, abs_y = int(tx + monitor["left"]), int(ty + monitor["top"])
                        pydirectinput.press('space') # Dur
                        ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                        time.sleep(0.1); pydirectinput.click()
                        self.last_action_time = now; self.last_state = "MOVING_TO_METIN"; self.update_log.emit(f"🎯 Hedef: ({abs_x}, {abs_y})")
                else:
                    # Hedef yoksa ve otonomsa arama yap
                    if self.last_state == "IDLE":
                        if not self.is_rotating:
                            pydirectinput.keyDown('q'); self.is_rotating = True; self.rotation_start_time = now
                        
                        search_cycle = (now - self.rotation_start_time) % 4.0
                        if search_cycle < 2.0: pydirectinput.keyDown('t'); pydirectinput.keyUp('g')
                        else: pydirectinput.keyDown('g'); pydirectinput.keyUp('t')

                        if now - self.rotation_start_time > 12:
                            for k in ['q', 't', 'g']: pydirectinput.keyUp(k)
                            self.is_rotating = False

                self.last_hp = self.curr_hp; self.last_x = self.curr_x

    def stop(self):
        self.running = False

class PreviewWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Bot Preview")
        self.setMinimumSize(640, 480) # Minimum boyut belirlendi
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0,0,0,0) # Kenar boşlukları sıfırlandı
        self.preview_label = QLabel("ÖNİZLEME BEKLENİYOR..."); self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setScaledContents(True) # Tam ekran ölçekleme aktif
        self.layout.addWidget(self.preview_label)
        self.setStyleSheet("background: black; color: white;")

    def update_image(self, f):
        q_img = QImage(cv2.cvtColor(f, cv2.COLOR_BGR2RGB).data, f.shape[1], f.shape[0], f.shape[1]*3, QImage.Format.Format_RGB888)
        self.preview_label.setPixmap(QPixmap.fromImage(q_img).scaled(self.width(), self.height(), Qt.AspectRatioMode.KeepAspectRatio))

class ModernBotUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Metin2 YOLO Bot")
        self.setMinimumSize(340, 800) # Esnek boyutlandırma
        self.setStyleSheet("QMainWindow { background: #0f172a; } QLabel { color: #cbd5e1; font-size: 11px; } QLineEdit { background: #1e293b; color: white; border: 1px solid #334155; border-radius: 5px; padding: 5px; } QPushButton { background: #22c55e; color: white; font-weight: bold; padding: 10px; border-radius: 8px; } #StopBtn { background: #ef4444; } QTextEdit { background: #000; color: #38bdf8; border-radius: 5px; font-family: 'Consolas'; font-size: 10px; }")

        central = QWidget(); self.setCentralWidget(central); layout = QHBoxLayout(central)
        left = QVBoxLayout(); left_panel = QFrame(); left_panel.setLayout(left); left_panel.setFixedWidth(320)
        left.addWidget(QLabel("🤖 MEMORY HELPER ACTIVE", styleSheet="font-size: 18px; color: #38bdf8; font-weight: bold;"))
        
        left.addWidget(QLabel("👤 Karakter İsmi (Değiştirmek için yazıp Enter'a basın):"))
        self.name_input = QLineEdit("---"); self.name_input.returnPressed.connect(self.change_name)
        left.addWidget(self.name_input)
        
        self.hud_x = QLabel("X: 0.00"); left.addWidget(self.hud_x)
        self.hud_y = QLabel("Y: 0.00"); left.addWidget(self.hud_y)
        self.hud_hp = QLabel("HP: 0"); left.addWidget(self.hud_hp)
        self.hud_atk = QLabel("ATK: 0"); left.addWidget(self.hud_atk)
        self.hud_bar = QLabel("Hedef Barı: KAPALI"); left.addWidget(self.hud_bar)
        self.hud_vid = QLabel("Hedef VID: 0"); left.addWidget(self.hud_vid)
        self.hud_status = QLabel("Durum: BEKLEMEDE"); left.addWidget(self.hud_status)
        self.hud_enemy = QLabel("Düşman: SEÇİLMEDİ"); left.addWidget(self.hud_enemy)
        
        # --- FOV KONTROL PANELI (REDESIGN) ---
        fov_group = QVBoxLayout()
        fov_group.setSpacing(10)
        
        # 1. Max FOV (Limit)
        max_fov_layout = QHBoxLayout()
        self.max_fov_input = QLineEdit("10000"); self.max_fov_input.setFixedWidth(60)
        self.hud_fov_max = QLabel("Mevcut: 10000"); self.hud_fov_max.setStyleSheet("color: #38bdf8;")
        btn_max_fov = QPushButton("Sınırı Ayarla"); btn_max_fov.setFixedWidth(100); btn_max_fov.setStyleSheet("padding: 5px; font-size: 11px;")
        btn_max_fov.clicked.connect(self.set_max_fov_manual)
        max_fov_layout.addWidget(QLabel("Max FOV:")); max_fov_layout.addWidget(self.max_fov_input); max_fov_layout.addWidget(btn_max_fov); max_fov_layout.addWidget(self.hud_fov_max)
        fov_group.addLayout(max_fov_layout)

        # 2. Set FOV (Zoom)
        set_fov_layout = QHBoxLayout()
        self.fov_val_input = QLineEdit("2500"); self.fov_val_input.setFixedWidth(60)
        self.hud_fov_curr = QLabel("Anlık: 2500"); self.hud_fov_curr.setStyleSheet("color: #38bdf8;")
        self.fov_auto_cb = QCheckBox("Sabitle")
        self.fov_auto_cb.stateChanged.connect(self.toggle_auto_fov)
        btn_set_fov = QPushButton("Zumu Ayarla"); btn_set_fov.setFixedWidth(100); btn_set_fov.setStyleSheet("padding: 5px; font-size: 11px;")
        btn_set_fov.clicked.connect(self.set_zoom_fov_manual)
        set_fov_layout.addWidget(QLabel("Zoom FOV:")); set_fov_layout.addWidget(self.fov_val_input); set_fov_layout.addWidget(btn_set_fov); set_fov_layout.addWidget(self.hud_fov_curr)
        fov_group.addLayout(set_fov_layout)
        fov_group.addWidget(self.fov_auto_cb)
        
        left.addLayout(fov_group)

        # Confidence Ayarı
        left.addWidget(QLabel("Confidence (Güven) Ayarı:"))
        conf_layout = QHBoxLayout()
        self.conf_slider = QSlider(Qt.Orientation.Horizontal)
        self.conf_slider.setRange(5, 95); self.conf_slider.setValue(60)
        self.conf_label = QLabel("0.60")
        self.conf_slider.valueChanged.connect(self.update_conf)
        conf_layout.addWidget(self.conf_slider); conf_layout.addWidget(self.conf_label)
        left.addLayout(conf_layout)

        left.addStretch()
        self.start_btn = QPushButton("BAŞLAT"); self.start_btn.clicked.connect(self.start_bot)
        left.addWidget(self.start_btn)
        self.stop_btn = QPushButton("DURDUR"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop_bot)
        left.addWidget(self.stop_btn)

        # Önizleme Kontrolü
        self.preview_window = PreviewWindow()
        self.show_preview_cb = QCheckBox("Görüntüyü Göster (Ayrı Pencere)")
        self.show_preview_cb.stateChanged.connect(self.toggle_preview)
        left.addWidget(self.show_preview_cb)

        self.log = QTextEdit(); self.log.setFixedHeight(120); left.addWidget(self.log)
        layout.addWidget(left_panel)

        self.worker = BotWorker(r"runs\detect\train6\weights\best.pt")
        self.worker.update_frame.connect(self.preview_window.update_image)
        self.worker.update_log.connect(self.on_update_log)
        self.worker.update_data.connect(self.on_update_data)
        self.worker.start()

    def start_bot(self): self.worker.autopilot = True; self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)
    def stop_bot(self): self.worker.autopilot = False; self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)
    
    def toggle_preview(self, state):
        show = (state == 2)
        self.worker.show_preview = show
        if show: self.preview_window.show()
        else: self.preview_window.hide()
        self.on_update_log(f"📺 Önizleme: {'AÇIK' if show else 'KAPALI'}")

    def on_update_frame(self, f): pass # Artık PreviewWindow kullanıyor
    def on_update_log(self, m): self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}")
    def on_update_data(self, x, y, hp, atk, name, is_bar, vid, enemy_dead, fov):
        self.hud_x.setText(f"X: {x:.2f}"); self.hud_y.setText(f"Y: {y:.2f}")
        self.hud_hp.setText(f"HP: {int(hp)}"); self.hud_atk.setText(f"ATK: {int(atk)}")
        self.hud_vid.setText(f"Hedef VID: {vid}")
        self.hud_fov_curr.setText(f"Anlık: {fov:.0f}")
        self.hud_fov_max.setText(f"Mevcut: {self.worker.max_fov:.0f}")
        
        # Düşman Durumu
        if enemy_dead == 1:
            self.hud_enemy.setText("Düşman: CANLI 🛡️"); self.hud_enemy.setStyleSheet("color: #22c55e;")
        elif enemy_dead == 2:
            self.hud_enemy.setText("Düşman: ÖLDÜ 💀"); self.hud_enemy.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            self.hud_enemy.setText("Düşman: SEÇİLMEDİ"); self.hud_enemy.setStyleSheet("color: #94a3b8;")

        if vid > 0:
            self.hud_status.setText("Durum: SALDIRIYOR"); self.hud_status.setStyleSheet("color: #22c55e; font-weight: bold;")
        else:
            self.hud_status.setText("Durum: BEKLEMEDE"); self.hud_status.setStyleSheet("color: #94a3b8;")
            
        if is_bar:
            self.hud_bar.setText("Hedef Barı: AÇIK"); self.hud_bar.setStyleSheet("color: #22c55e; font-weight: bold;")
        else:
            self.hud_bar.setText("Hedef Barı: KAPALI"); self.hud_bar.setStyleSheet("color: #94a3b8;")

    def toggle_auto_fov(self, state):
        enabled = (state == 2)
        self.worker.auto_fov_enabled = enabled
        self.on_update_log(f"📸 Auto FOV Sabitleme: {'AÇIK' if enabled else 'KAPALI'}")

    def set_zoom_fov_manual(self):
        try:
            val = float(self.fov_val_input.text())
            self.worker.target_fov = val
            ptr_fov_set = self.worker.helper.resolve_pointer(0x039195F8, [0, 0x14, 0x134])
            if ptr_fov_set: self.worker.helper.write_float(ptr_fov_set, val)
            self.on_update_log(f"🎯 Zoom FOV {val} olarak ayarlandı.")
        except: self.on_update_log("⚠️ Geçersiz Zoom değeri!")

    def set_max_fov_manual(self):
        try:
            val = float(self.max_fov_input.text())
            self.worker.max_fov = val
            ptr_fov_max = self.worker.helper.resolve_pointer(0x00A6AC3C, [0])
            if ptr_fov_max: self.worker.helper.write_float(ptr_fov_max, val)
            self.on_update_log(f"📸 Max FOV Sınırı {val} olarak ayarlandı.")
        except: self.on_update_log("⚠️ Geçersiz Max FOV değeri!")

    def update_conf(self, val):
        conf = val / 100.0
        self.conf_label.setText(f"{conf:.2f}")
        if self.worker: self.worker.conf_threshold = conf

    def change_name(self):
        new_name = self.name_input.text()
        self.worker.new_name_to_write = new_name
        self.on_update_log(f"✍️ İsim değiştiriliyor: {new_name}")

if __name__ == "__main__":
    app = QApplication(sys.argv); win = ModernBotUI(); win.show(); sys.exit(app.exec())
