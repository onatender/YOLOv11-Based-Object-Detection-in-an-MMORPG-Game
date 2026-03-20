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
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, QLineEdit, QFrame
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

class BotWorker(QThread):
    update_frame = pyqtSignal(np.ndarray)
    update_log = pyqtSignal(str)
    update_data = pyqtSignal(float, float, float, float, str, bool) # X, Y, HP, ATK, Name, BarOnScreen

    def __init__(self, model_path):
        super().__init__()
        self.model = YOLO(model_path)
        self.proc_name = "metin2client.bin"
        self.running = True
        self.autopilot = False
        
        # Paylaşılan Veriler
        self.curr_x = 0.0
        self.curr_y = 0.0
        self.curr_hp = 0
        self.mem_is_attacking = 0
        self.curr_name = "Bağlanıyor..."
        self.new_name_to_write = None
        self.target_x = 0
        self.target_hp = 0
        
        # Bot Mantığı
        self.last_state = "IDLE"
        self.last_hp = 100.0
        self.last_x = 0.0
        self.last_action_time = time.time()
        self.is_rotating = False
        self.rotation_start_time = 0.0
        self.stuck_retry_count = 0
        self.is_bar_on_screen = False # Hafızadan gelen can barı durumu

    def _memory_scanner(self):
        """Kütüphane (MemoryHelper) Destekli Bellek Tarayıcı"""
        last_reported_addr = None
        ptr_fail_logged = False
        
        try:
            helper = MemoryHelper(self.proc_name)
            mz = helper.get_mz_signature()
            self.update_log.emit(f"🚀 Modül: {hex(helper.module_base).upper()} (MZ: {hex(mz).upper()})")
        except Exception as e:
            self.update_log.emit(f"❌ Bağlantı Kesildi: {str(e)}")
            return

        while self.running:
            try:
                # 1. Koordinat Okuma (Dinamik Pointerlar)
                ptr_x = helper.resolve_pointer(0x03919600, [0x250])
                if ptr_x: self.curr_x = helper.read_float(ptr_x)
                
                ptr_y = helper.resolve_pointer(0x039148F8, [0x9C]) # Yeni Y: [Base+039148F8]+9C
                if ptr_y: self.curr_y = helper.read_float(ptr_y)
                
                # 3. Karakter İsmi Okuma/Yazma (Pointer: [Base+03914B3C]+14+10)
                ptr_name = helper.resolve_pointer(0x03914B3C, [0x14, 0x10])
                if ptr_name:
                    if self.new_name_to_write:
                        helper.write_string(ptr_name, self.new_name_to_write)
                        self.new_name_to_write = None
                    self.curr_name = helper.read_string(ptr_name)
                
                # 5. ATK Pointer Çözme
                ptr_addr = helper.resolve_pointer(0x0356148C, [0x0, 0x88])
                if ptr_addr: self.mem_is_attacking = helper.read_int(ptr_addr)

                # 6. canBarOnScreen Takibi ([Base+0398EF0C]+8+6C0)
                ptr_bar = helper.resolve_pointer(0x0398EF0C, [0x8, 0x6C0])
                if ptr_bar:
                    self.is_bar_on_screen = (helper.read_uint(ptr_bar) & 0xFF) > 0
                else: 
                    self.is_bar_on_screen = False
                
                if ptr_addr:
                    if ptr_addr != last_reported_addr:
                        self.update_log.emit(f"🎯 ATK AKTİF: {hex(ptr_addr).upper()}")
                        last_reported_addr = ptr_addr
                        ptr_fail_logged = False
                elif not ptr_fail_logged:
                    self.update_log.emit("⚠️ ATK İzi koptu (Giriş bekleniyor)")
                    ptr_fail_logged = True
            except: pass
            time.sleep(0.01)

    def run(self):
        # --- BAŞLANGIÇ ---
        self.update_log.emit("⚙️ Bellek İzleme Başlatılıyor...")
        threading.Thread(target=self._memory_scanner, daemon=True).start()

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self.running:
                now = time.time()
                self.update_data.emit(self.curr_x, self.curr_y, float(self.curr_hp), float(self.mem_is_attacking), self.curr_name, self.is_bar_on_screen)

                sct_img = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(sct_img)[:, :, :3])
                
                # Görsel arama (Gereksiz CPU yükü) devri kapandı!
                is_bar_visible = self.is_bar_on_screen
                
                self.update_frame.emit(frame)

                if not self.autopilot:
                    time.sleep(0.05); continue

                # YOLO & BOT MANTIĞI
                results = self.model.predict(source=frame, conf=0.10, device='0', half=True, imgsz=1024, verbose=False)
                targets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = box.xyxy[0]
                        targets.append(((x1+x2)/2, y2 - 20))

                is_empty = len(targets) == 0
                is_static = abs(self.curr_x - self.last_x) < 0.001

                # STUCK RECOVERY (Sadece can barı yokken ve koordinat sabitse tetiklenir)
                if (self.last_state in ["MOVING", "ATTACKING"]) and (now - self.last_action_time > 4.0) and is_static and not self.is_bar_on_screen:
                    self.update_log.emit("⚠️ Engel aşılamadı, kurtarma operasyonu...")
                    for k in ['s','a','d','w']:
                        pydirectinput.keyDown(k); time.sleep(0.3); pydirectinput.keyUp(k)
                    pydirectinput.press('3')
                    pydirectinput.keyDown('f'); time.sleep(1.0); pydirectinput.keyUp('f')
                    self.last_action_time = now; self.last_state = "IDLE"; continue

                if is_bar_visible:
                    if self.last_state != "ATTACKING":
                        self.update_log.emit("⚔️ Metin Kesiliyor...")
                        self.last_state = "ATTACKING"
                else:
                    if self.last_state == "ATTACKING":
                        self.update_log.emit("✅ Metin bitti, yeni hedef aranıyor")
                        self.last_state = "IDLE"
                    
                    if is_empty:
                        if not self.is_rotating:
                            pydirectinput.keyDown('q'); self.is_rotating = True; self.rotation_start_time = now
                        elif now - self.rotation_start_time > 10:
                            pydirectinput.keyUp('q'); self.is_rotating = False
                    else:
                        if self.is_rotating: pydirectinput.keyUp('q'); self.is_rotating = False
                        tx, ty = targets[0]
                        ctypes.windll.user32.SetCursorPos(int(tx + monitor["left"]), int(ty + monitor["top"]))
                        time.sleep(0.05); pydirectinput.click()
                        self.last_action_time = now; self.last_state = "MOVING"; self.update_log.emit("🎯 Yeni Hedef")

                self.last_hp = self.curr_hp; self.last_x = self.curr_x

    def stop(self):
        self.running = False

class ModernBotUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Bot - Library Edition")
        self.setMinimumSize(1000, 750)
        self.setStyleSheet("QMainWindow { background: #0f172a; } QLabel { color: #cbd5e1; } QLineEdit { background: #1e293b; color: white; border: 1px solid #334155; border-radius: 5px; padding: 10px; } QPushButton { background: #22c55e; color: white; font-weight: bold; padding: 15px; border-radius: 10px; } #StopBtn { background: #ef4444; } QTextEdit { background: #000; color: #38bdf8; border-radius: 10px; font-family: 'Consolas'; }")

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

        left.addStretch()
        self.start_btn = QPushButton("BAŞLAT"); self.start_btn.clicked.connect(self.start_bot)
        left.addWidget(self.start_btn)
        self.stop_btn = QPushButton("DURDUR"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop_bot)
        left.addWidget(self.stop_btn)

        right = QVBoxLayout(); self.radar = QLabel("RADAR"); self.log = QTextEdit(); self.log.setFixedHeight(180)
        right.addWidget(self.radar); right.addWidget(self.log)
        layout.addWidget(left_panel); layout.addLayout(right)

        self.worker = BotWorker(r"runs\detect\train6\weights\best.pt")
        self.worker.update_frame.connect(self.on_update_frame)
        self.worker.update_log.connect(self.on_update_log)
        self.worker.update_data.connect(self.on_update_data)
        self.worker.start()

    def start_bot(self): self.worker.autopilot = True; self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)
    def stop_bot(self): self.worker.autopilot = False; self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)
    def on_update_frame(self, f):
        q_img = QImage(cv2.cvtColor(f, cv2.COLOR_BGR2RGB).data, f.shape[1], f.shape[0], f.shape[1]*3, QImage.Format.Format_RGB888)
        self.radar.setPixmap(QPixmap.fromImage(q_img).scaled(self.radar.width(), self.radar.height(), Qt.AspectRatioMode.KeepAspectRatio))
    def on_update_log(self, m): self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}")
    def on_update_data(self, x, y, hp, atk, name, is_bar):
        self.hud_x.setText(f"X: {x:.2f}")
        self.hud_y.setText(f"Y: {y:.2f}")
        self.hud_hp.setText(f"HP: {int(hp)}")
        self.hud_atk.setText(f"ATK: {int(atk)}")
        
        if is_bar:
            self.hud_bar.setText("Hedef Barı: AÇIK"); self.hud_bar.setStyleSheet("color: #22c55e; font-weight: bold;")
        else:
            self.hud_bar.setText("Hedef Barı: KAPALI"); self.hud_bar.setStyleSheet("color: #ef4444;")
            
        if not self.name_input.hasFocus():
            self.name_input.setText(name)

    def change_name(self):
        new_name = self.name_input.text()
        self.worker.new_name_to_write = new_name
        self.log.append(f"✍️ İsim değiştiriliyor: {new_name}")

if __name__ == "__main__":
    app = QApplication(sys.argv); win = ModernBotUI(); win.show(); sys.exit(app.exec())
