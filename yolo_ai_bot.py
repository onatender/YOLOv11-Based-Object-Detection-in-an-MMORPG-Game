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
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, QLineEdit, QFrame, QSlider, QCheckBox, QGridLayout, QSizePolicy
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap
from memory_helper import MemoryHelper

# --- YÖNETİCİ KONTROLÜ VE GÜVENLİK ---
def is_admin():
    try: return ctypes.windll.shell32.IsUserAnAdmin()
    except: return False

if not is_admin():
    # Force Admin Elevation
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{__file__}"', None, 1)
    sys.exit()

pydirectinput.FAILSAFE = False # Kilitleri kapat
# DPI Farkındalığı (Qt 6 varsayılan olarak yönetir, manuel ayar bazen çakışma yaratır)
# try:
#     ctypes.windll.shcore.SetProcessDpiAwareness(1)
# except Exception:
#     try: ctypes.windll.user32.SetProcessDPIAware()
#     except: pass

class BotWorker(QThread):
    update_frame = pyqtSignal(np.ndarray)
    update_log = pyqtSignal(str)
    update_data = pyqtSignal(float, float, float, str, int, int, int, float) # X, Y, ATK, Name, SelVID, AtkVID, EnemyDead, CurrFov

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
        self.curr_x, self.curr_y = 0.0, 0.0
        self.last_x, self.last_y = 0.0, 0.0
        self.mem_is_attacking = 0
        self.curr_name = "Bağlanıyor..."
        self.new_name_to_write = None
        self.curr_fov = 10000.0
        self.target_fov = 10000.0
        self.auto_fov_enabled = True # Varsayılan: TİKLİ
        self.sel_vid = 0 # Seçili hedef VID [0x462DC]
        self.atk_vid = 0 # Saldırılan hedef VID [0x4C]
        self.locked_vid = 0 # Hedef kaybolunca geri yazılacak ID (Target Lock)
        self.conf_threshold = 0.60 # Varsayılan güven eşiği: %60
        self.is_enemy_dead = 0 # 0: Yok, 1: Canlı, 2: Ölü
        self.max_fov = 10000.0 # Varsayılan Max FOV değeri
        self.show_preview = False 
        self.template = cv2.imread("search_for.png") if os.path.exists("search_for.png") else None
        
        # Zamanlayıcılar ve Durum Yönetimi
        self.last_f_time = time.time()
        self.f_state = False 
        self.last_ui_time = 0.0 
        self.last_action_time = time.time()
        self.last_state = "IDLE"
        self.last_logged_msg = ""
        self.search_start_time = 0.0
        self.stuck_cooldown = 0.0
        self.atk_confirm_count = 0
        self.static_duration = 0.0
        self.last_frame_time = time.time()

    def smart_log(self, msg):
        """Aynı logun tekrar etmesini önleyerek temiz çıktı sağlar."""
        if msg != self.last_logged_msg:
            # UI loguna gönder
            self.update_log.emit(msg)
            self.last_logged_msg = msg

    def _memory_scanner(self):
        """Merkezi self.helper Üzerinden Bellek Tarayıcı"""
        while self.running:
            try:
                # 1. Koordinat Okuma (Yeni Pointer: [0x03914B44]+0+0x910)
                ptr_pos = self.helper.resolve_pointer(0x03914B44, [0, 0x910])
                if ptr_pos:
                    self.curr_x = self.helper.read_float(ptr_pos)
                    self.curr_y = self.helper.read_float(ptr_pos + 0x4) # Y offset: 0x914 - 0x910 = 0x4
                
                # 2. İsim İşlemleri
                ptr_name = self.helper.resolve_pointer(0x03914B3C, [0x14, 0x10])
                if ptr_name:
                    if self.new_name_to_write:
                        self.helper.write_string(ptr_name, self.new_name_to_write)
                        self.new_name_to_write = None
                    self.curr_name = self.helper.read_string(ptr_name)
                
                # 4. Saldırı Durumu (Yeni Pointer: [0x039103F0]+0x10+0x7EC)
                ptr_atk = self.helper.resolve_pointer(0x039103F0, [0x10, 0x7EC])
                if ptr_atk: 
                    atk_val = self.helper.read_int(ptr_atk)
                    self.mem_is_attacking = 1 if atk_val > 0 else 0

                # 4. Düşman Durumu (isEnemyDead) - VID İşlemlerinden Önce Okunmalı
                ptr_dead = self.helper.resolve_pointer(0x039171A8, [0x6C0])
                if ptr_dead: self.is_enemy_dead = self.helper.read_uint(ptr_dead)
                
                # ❗ ÖLÜM KONTROLÜ: Eğer metin öldüyse kilitleri hemen çöz
                if self.is_enemy_dead == 2:
                    self.locked_vid = 0; self.sel_vid = 0; self.atk_vid = 0

                # 5. Selected VID ([0x039103C8]+0x462DC)
                ptr_sel = self.helper.resolve_pointer(0x039103C8, [0x462DC])
                if ptr_sel: 
                    v = self.helper.read_uint(ptr_sel)
                    if v > 0: 
                        self.sel_vid = v; self.locked_vid = v
                    elif self.autopilot and self.locked_vid > 0:
                        # Sadece metin canlıyken kilit yazmaya devam et
                        if self.is_enemy_dead == 1:
                            self.helper.write_uint(ptr_sel, self.locked_vid)
                            time.sleep(0.01)
                            if self.helper.read_uint(ptr_sel) == 0: self.locked_vid = 0; self.sel_vid = 0
                            else: self.sel_vid = self.locked_vid
                        else: self.locked_vid = 0; self.sel_vid = 0
                    else: self.sel_vid = 0

                # 6. Attack VID ([0x039103B8]+0x4C) - DOĞRUDAN OKUMA
                # 6. Attack VID ([0x039103B8]+0x4C) - HAM VERİ OKUMA
                ptr_atk_target = self.helper.resolve_pointer(0x039103B8, [0x4C])
                if ptr_atk_target: 
                    # 🚀 Hayalet Saldırı: Sadece canlı metne seçiliysek hafızaya YAZ
                    if self.autopilot and self.sel_vid > 0 and self.is_enemy_dead == 1:
                        if self.helper.read_uint(ptr_atk_target) != self.sel_vid:
                            self.helper.write_uint(ptr_atk_target, self.sel_vid)
                    
                    # 🎯 SON DURUMU HER ZAMAN OKU (Filtresiz)
                    self.atk_vid = self.helper.read_uint(ptr_atk_target)
                
                # ❗ MANUEL SIFIRLAMALAR KALDIRILDI - Hafıza ne diyorsa o!
                
                # 7. Anlık FOV (setFov) [[[Base+039195F8]+0]+14]+134
                ptr_fov_set = self.helper.resolve_pointer(0x039195F8, [0, 0x14, 0x134])
                if ptr_fov_set:
                    self.curr_fov = self.helper.read_float(ptr_fov_set)
                    if self.auto_fov_enabled:
                        self.helper.write_float(ptr_fov_set, self.target_fov)

                # 🚀 ANLIK SİNYAL: Bellek verilerini görüntüyü beklemeden saniyede 100 kez Dashboard'a gönder
                self.update_data.emit(self.curr_x, self.curr_y, float(self.mem_is_attacking), self.curr_name, self.sel_vid, self.atk_vid, self.is_enemy_dead, self.curr_fov)
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
                
                # YOLO MODELLERİYLE HEDEF TESPİTİ
                results = self.model(frame, conf=self.conf_threshold, device='0', half=True, imgsz=640, verbose=False)
                targets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = box.xyxy[0]
                        cx, cy_mid = int((x1+x2)/2), int((y1+y2)/2)
                        targets.append((cx, cy_mid))
                        cv2.circle(frame, (cx, cy_mid), int(max(x2-x1, y2-y1)/1.6), (0, 0, 255), 2)
                
                is_empty = len(targets) == 0

                # UI GÜNCELLEME (Sadece Görüntü)
                if now - self.last_ui_time > 0.04:
                    if self.show_preview: self.update_frame.emit(frame)
                    self.last_ui_time = now
                
                if not self.autopilot:
                    for k in ['f', 'q', 't', 'g']: pydirectinput.keyUp(k)
                    time.sleep(0.01); continue
                
                # --- OTONOM BOT MANTIĞI ---
                # 1. HAREKETLİLİK VE SIKIŞMA TAKİBİ
                is_static = abs(self.curr_x - self.last_x) < 0.01 # Hassasiyeti biraz düşürdük
                if is_static:
                    self.static_duration += (now - self.last_frame_time)
                else:
                    self.static_duration = 0.0
                
                # 2. STUCK RECOVERY (HEDEF VARKEN HAREKET EDEMEME)
                # 'Gidiliyor' durumunda 1.5sn, Arama (Q) durumunda 3sn hareketsizlik sıkışma sayılır
                stuck_threshold = 1.5 if self.last_state == "MOVING_TO_METIN" else 3.0
                
                # Sadece bir şey yapmaya çalışırken (Gidiyor/Arıyor) ve 1.5-3sn hareketsizse
                is_really_stuck = (self.static_duration > stuck_threshold) and \
                                 (not self.is_rotating) and \
                                 (self.last_state in ["MOVING_TO_METIN", "EXPANDING_SEARCH_Q"])

                if is_really_stuck and self.autopilot and (now > self.stuck_cooldown):
                    self.smart_log("SIKIŞMA TESPİT EDİLDİ")
                    recovery_keys = ['space', 'w', 'a', 's', 'd', '3']
                    key = recovery_keys[self.stuck_retry_count % len(recovery_keys)]
                    self.smart_log(f"{key.upper()} DENENİYOR")
                    
                    if key == 'space': pydirectinput.press('space')
                    elif key == '3': pydirectinput.press('3')
                    else:
                        pydirectinput.keyDown(key); time.sleep(0.5); pydirectinput.keyUp(key)
                    
                    self.stuck_retry_count += 1
                    self.stuck_cooldown = now + 1.2
                    self.static_duration = 0.0 # Sayaç sıfırla ki peş peşe basmasın
                    continue

                if not is_static:
                    if self.stuck_retry_count > 0:
                        self.smart_log("KOORDİNAT DEĞİŞTİ, SIKIŞMADAN KURTULUNDU")
                        self.stuck_retry_count = 0

                # 3. DURUM MAKİNESİ (LOG VE STATE TAKİBİ)
                
                # A. SALDIRI DOĞRULAMA (DEBOUNCE)
                # SADECE 'atk_vid > 0' iken (UI'daki 'SALDIRIYOR' şartıyla aynı)
                if self.last_state == "MOVING_TO_METIN" and (self.mem_is_attacking == 1) and (self.atk_vid > 0):
                    self.atk_confirm_count += 1
                else:
                    self.atk_confirm_count = 0

                # B. SALDIRI BAŞLAMA KONTROLÜ (VARILDI KESİLİYOR)
                # Şartlar: Yoldayken + Sayaç dolmuşsa (En az 10 döngü) + Kurtarma anında değilsek
                if self.last_state == "MOVING_TO_METIN" and self.atk_confirm_count > 9 and (now > self.stuck_cooldown):
                    self.smart_log("METNE VARILDI KESİLİYOR")
                    self.last_state = "ATTACKING"

                # B. METİN KESİLME KONTROLÜ
                # Şartlar: Ölü düşman tespiti (2) + IDs temizlenmiş + Öncesinde saldırıyorsak
                is_done = (self.is_enemy_dead == 2) and (self.sel_vid == 0) and (self.atk_vid == 0)
                if is_done and self.last_state == "ATTACKING":
                    self.smart_log("METİN KESİLDİ")
                    self.last_state = "IDLE"
                    self.last_action_time = now

                # C. ARAMA VE GİTME MANTIĞI
                can_search = (self.sel_vid == 0) and (self.atk_vid == 0) and \
                             (self.last_state == "IDLE") and (self.is_enemy_dead in [0, 2])

                if can_search:
                    if not is_empty:
                        # YOLO BİR ŞEY GÖRDÜ
                        self.smart_log("METİN BULUNDU")
                        
                        if self.is_rotating: 
                            for k in ['q', 't', 'g']: pydirectinput.keyUp(k)
                            self.is_rotating = False
                        
                        tx, ty = targets[0]
                        abs_x, abs_y = int(tx + monitor["left"]), int(ty + monitor["top"])
                        ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                        
                        pydirectinput.press('space')
                        time.sleep(0.08)
                        ctypes.windll.user32.mouse_event(8, 0, 0, 0, 0) # Right Down
                        time.sleep(0.08)
                        ctypes.windll.user32.mouse_event(16, 0, 0, 0, 0) # Right Up
                        
                        self.smart_log("METNE GİDİLİYOR")
                        self.last_state = "MOVING_TO_METIN"
                        self.last_action_time = now
                        self.static_duration = 0.0 # Hareket başladığı an sayacı sıfırla!
                        self.stuck_retry_count = 0
                    else:
                        # Metin yok, aranıyor
                        self.smart_log("METİN ARANIYOR")
                        if not self.is_rotating:
                            self.is_rotating = True
                            self.rotation_start_time = now
                            self.smart_log("METİN BULUNAMADI Q İLE ARAMA GENİŞLETİLİYOR")
                            self.static_duration = 0.0 # Rotasyon başladığı an sayacı sıfırla!
                            pydirectinput.keyDown('q')
                        elif now - self.rotation_start_time > 12.0:
                            # 12 saniye döndü hala yok. Biraz ilerle ki yer değişsin.
                            pydirectinput.keyUp('q')
                            self.is_rotating = False
                            self.smart_log("ARAMA GENİŞLETİLDİ AMA BULUNAMADI, YER DEĞİŞTİRİLİYOR (W)")
                            pydirectinput.keyDown('w')
                            time.sleep(1.0)
                            pydirectinput.keyUp('w')
                            self.last_action_time = now
                            self.rotation_start_time = now # Reset timer
                else:
                    # Görev var (Gidiyor veya Kesiyor)
                    if not is_empty and self.last_state == "MOVING_TO_METIN":
                        tx, ty = targets[0]
                        abs_x, abs_y = int(tx + monitor["left"]), int(ty + monitor["top"])
                        ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                        
                        # Re-click logic (Eğer yolda takıldıysa ve IDLE'a düşmemişse)
                        if (self.sel_vid == 0) and (now - self.last_action_time > 2.0):
                            ctypes.windll.user32.mouse_event(8, 0, 0, 0, 0)
                            time.sleep(0.08)
                            ctypes.windll.user32.mouse_event(16, 0, 0, 0, 0)
                            self.last_action_time = now
                            self.smart_log("METNE GİDİLİYOR (Yeniden Seçildi)")
                            self.static_duration = 0.0 # Yeniden seçimde sayacı sıfırla!
                    
                    if self.is_rotating:
                        for k in ['q', 't', 'g']: pydirectinput.keyUp(k)
                        self.is_rotating = False

                self.last_frame_time = now
                self.last_x = self.curr_x

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
        self.setWindowTitle("Metin2 YOLO Bot - PRO DASHBOARD")
        self.setMinimumSize(1000, 750) # Geniş ekran desteği
        self.setStyleSheet("QMainWindow { background: #0f172a; } QLabel { color: #cbd5e1; font-size: 11px; } QLineEdit { background: #1e293b; color: white; border: 1px solid #334155; border-radius: 5px; padding: 5px; } QPushButton { background: #22c55e; color: white; font-weight: bold; padding: 10px; border-radius: 8px; } #StopBtn { background: #ef4444; } QTextEdit { background: #000; color: #38bdf8; border-radius: 5px; font-family: 'Consolas'; font-size: 11px; }")

        central = QWidget(); self.setCentralWidget(central); main_layout = QVBoxLayout(central)
        
        # --- ÜST BÖLÜM: KARAKTER VE DURUM BİLGİSİ ---
        top_panel = QFrame(); top_panel.setStyleSheet("background: #1e293b; border-radius: 10px;"); top_layout = QHBoxLayout(top_panel)
        
        # Karakter Bilgileri
        char_info = QVBoxLayout()
        helper_title = QLabel("🤖 MEMORY HELPER ACTIVE")
        helper_title.setStyleSheet("font-size: 16px; color: #38bdf8; font-weight: bold;")
        char_info.addWidget(helper_title)
        self.hud_name_label = QLabel("Karakter: ---"); char_info.addWidget(self.hud_name_label)
        self.name_input = QLineEdit("---"); self.name_input.setFixedWidth(200); self.name_input.returnPressed.connect(self.change_name)
        char_info.addWidget(self.name_input); top_layout.addLayout(char_info)
        
        # Koordinat ve Vuruş
        stats_layout = QGridLayout()
        self.hud_x = QLabel("X: 0.00"); stats_layout.addWidget(self.hud_x, 0, 0)
        self.hud_y = QLabel("Y: 0.00"); stats_layout.addWidget(self.hud_y, 0, 1)
        self.hud_atk = QLabel("ATK: 0"); stats_layout.addWidget(self.hud_atk, 1, 0, 1, 2)
        top_layout.addLayout(stats_layout)
        
        # Target Bilgileri
        target_layout = QVBoxLayout()
        self.hud_sel_vid = QLabel("Seçili VID: 0"); target_layout.addWidget(self.hud_sel_vid)
        self.hud_atk_vid = QLabel("Saldırı VID: 0"); target_layout.addWidget(self.hud_atk_vid)
        self.hud_status = QLabel("Durum: BEKLEMEDE"); target_layout.addWidget(self.hud_status)
        self.hud_enemy = QLabel("Düşman: SEÇİLMEDİ"); target_layout.addWidget(self.hud_enemy)
        top_layout.addLayout(target_layout); main_layout.addWidget(top_panel)

        # --- ORTA BÖLÜM: AYARLAR VE BUTONLAR ---
        mid_layout = QHBoxLayout()
        
        # FOV Paneli (Sol)
        fov_panel = QFrame(); fov_panel.setStyleSheet("background: #1e293b; border-radius: 10px;"); fov_v = QVBoxLayout(fov_panel)
        fov_v.addWidget(QLabel("📸 GÖRÜŞ AÇISI (FOV) AYARLARI", styleSheet="font-weight: bold; color: #38bdf8;"))
        
        max_row = QHBoxLayout(); self.max_fov_input = QLineEdit("10000"); self.hud_fov_max = QLabel("Mevcut: 10000")
        btn_max = QPushButton("Sınır"); btn_max.clicked.connect(self.set_max_fov_manual)
        max_row.addWidget(QLabel("Max:")); max_row.addWidget(self.max_fov_input); max_row.addWidget(btn_max); max_row.addWidget(self.hud_fov_max)
        fov_v.addLayout(max_row)

        zoom_row = QHBoxLayout(); self.fov_val_input = QLineEdit("10000"); self.hud_fov_curr = QLabel("Anlık: 10000")
        btn_zoom = QPushButton("Zoom"); btn_zoom.clicked.connect(self.set_zoom_fov_manual)
        zoom_row.addWidget(QLabel("Set:")); zoom_row.addWidget(self.fov_val_input); zoom_row.addWidget(btn_zoom); zoom_row.addWidget(self.hud_fov_curr)
        fov_v.addLayout(zoom_row)
        
        self.fov_auto_cb = QCheckBox("Sürekli Sabitle"); self.fov_auto_cb.setChecked(True)
        self.fov_auto_cb.stateChanged.connect(self.toggle_auto_fov)
        fov_v.addWidget(self.fov_auto_cb); mid_layout.addWidget(fov_panel)

        # Kontrol Ve Gürüntü (Sağ)
        ctrl_panel = QVBoxLayout()
        self.conf_slider = QSlider(Qt.Orientation.Horizontal); self.conf_slider.setRange(5, 95); self.conf_slider.setValue(60)
        self.conf_label = QLabel("Conf: 0.60"); self.conf_slider.valueChanged.connect(self.update_conf)
        ctrl_panel.addWidget(self.conf_label); ctrl_panel.addWidget(self.conf_slider)
        
        self.start_btn = QPushButton("🚀 BAŞLAT (AUTO)"); self.start_btn.clicked.connect(self.start_bot)
        self.stop_btn = QPushButton("🛑 DURDUR"); self.stop_btn.setEnabled(False); self.stop_btn.setObjectName("StopBtn"); self.stop_btn.clicked.connect(self.stop_bot)
        ctrl_panel.addWidget(self.start_btn); ctrl_panel.addWidget(self.stop_btn)
        
        self.preview_window = PreviewWindow()
        self.preview_window.hide() # Kesinlikle kapalı başla
        
        self.show_preview_cb = QCheckBox("Görüntüyü Göster (Yeni Pencere)")
        self.show_preview_cb.setChecked(False) 
        self.show_preview_cb.stateChanged.connect(self.toggle_preview)
        ctrl_panel.addWidget(self.show_preview_cb); mid_layout.addLayout(ctrl_panel)
        main_layout.addLayout(mid_layout)

        # --- ALT BÖLÜM: LOGLAR (GENİŞLEYEN) ---
        self.log = QTextEdit(); self.log.setPlaceholderText("Sistem logları burada görünecek..."); self.log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout.addWidget(QLabel("📜 SİSTEM LOGLARI")); main_layout.addWidget(self.log)

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
    def on_update_log(self, m):
        # Eğer log metni '[' ile başlıyorsa zaten formatlıdır (Dashboard mesajları)
        # Değilse tarih ekleyerek en tepeye (veya sona) yaz.
        # Kullanıcının isteği üzerine logu çok biriktirmeyelim
        if self.log.document().blockCount() > 50:
            self.log.clear()
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}")
    def on_update_data(self, x, y, atk, name, sel_vid, atk_vid, enemy_dead, fov):
        self.hud_x.setText(f"X: {x:.2f}"); self.hud_y.setText(f"Y: {y:.2f}")
        self.hud_atk.setText(f"ATK: {int(atk)}")
        self.hud_sel_vid.setText(f"Seçili VID: {sel_vid}")
        self.hud_atk_vid.setText(f"Saldırı VID: {atk_vid}")
        self.hud_name_label.setText(f"Karakter: {name}")
        self.hud_fov_curr.setText(f"Anlık: {fov:.0f}")
        self.hud_fov_max.setText(f"Mevcut: {self.worker.max_fov:.0f}")
        
        # Düşman Durumu
        if enemy_dead == 1:
            self.hud_enemy.setText("Düşman: CANLI 🛡️"); self.hud_enemy.setStyleSheet("color: #22c55e;")
        elif enemy_dead == 2:
            self.hud_enemy.setText("Düşman: ÖLDÜ 💀"); self.hud_enemy.setStyleSheet("color: #ef4444; font-weight: bold;")
        else:
            self.hud_enemy.setText("Düşman: SEÇİLMEDİ"); self.hud_enemy.setStyleSheet("color: #94a3b8;")

        if (atk_vid > 0) and (atk > 0):
            self.hud_status.setText("Durum: SALDIRIYOR"); self.hud_status.setStyleSheet("color: #22c55e; font-weight: bold;")
        else:
            self.hud_status.setText("Durum: BEKLEMEDE"); self.hud_status.setStyleSheet("color: #94a3b8;")

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
