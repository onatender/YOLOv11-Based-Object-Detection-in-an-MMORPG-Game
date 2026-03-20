import cv2
import numpy as np
import mss
import time
import ctypes
import pydirectinput
from ultralytics import YOLO
import threading
import sys
import os
import json
from pymem import Pymem
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLineEdit, QLabel, 
                             QFrame, QTextEdit)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap

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
    update_data = pyqtSignal(float, float) # X , HP

    def __init__(self, model_path, proc_name, addr_x, addr_hp):
        super().__init__()
        self.model = YOLO(model_path)
        self.proc_name = proc_name
        self.addr_x_hex = addr_x.strip()
        self.addr_hp_hex = addr_hp.strip()
        self.running = True
        
        # Paylaşılan Veriler (Thread-Safe)
        self.curr_x = 0.0
        self.curr_hp = 4294967280
        self.pm = None
        
        # Bot Mantığı
        self.last_state = "IDLE"
        self.last_hp = 100.0
        self.last_x = 0.0
        self.last_action_time = time.time()
        self.is_rotating = False
        self.rotation_start_time = 0
        self.stuck_retry_count = 0 # Kademeli sıkışma sayacı

        # Görsel Teyit
        self.template = None
        if os.path.exists("search_for.png"):
            self.template = cv2.imread("search_for.png", cv2.IMREAD_COLOR)

        try:
            self.pm = Pymem(self.proc_name)
            self.target_x = int(self.addr_x_hex, 16)
            self.target_hp = int(self.addr_hp_hex, 16)
            
            # --- TURBO BELLEK THREAD'İ BAŞLAT ---
            threading.Thread(target=self._memory_scanner, daemon=True).start()
        except: pass

    def _memory_scanner(self):
        """Saniyede 100 kez bellek okuyan bağımsız döngü"""
        while self.running:
            if self.pm:
                try:
                    self.curr_x = self.pm.read_float(self.target_x)
                    self.curr_hp = self.pm.read_uint(self.target_hp)
                except:
                    self.curr_hp = 4294967280
            time.sleep(0.01) # 10ms - Ultra Hızlı

    def run(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self.running:
                now = time.time()
                
                # Mevcut verileri kopyala (Hızlıca eriş)
                c_x = self.curr_x
                c_hp = self.curr_hp

                sct_img = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(sct_img)[:, :, :3])
                
                # 1. GÖRSEL TEYİT (Her karede)
                is_bar_visible = False
                if self.template is not None:
                    res = cv2.matchTemplate(frame, self.template, cv2.TM_CCOEFF_NORMED)
                    if np.max(res) > 0.8: is_bar_visible = True

                self.update_data.emit(c_x, float(c_hp))

                # 2. YOLO TESPİTİ (Ağır İşlem)
                results = self.model.predict(source=frame, conf=0.10, device='0', half=True, imgsz=1024, verbose=False)
                oyun_hedefleri = []
                for result in results:
                    for box in result.boxes:
                        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                        xc, yc = int((x1+x2)/2), int((y1+y2)/2)
                        radius = int(max((x2-x1), (y2-y1))/2) + 5
                        cv2.circle(frame, (xc, yc), radius, (0,0,255), 2)
                        oyun_hedefleri.append((xc, yc))

                if len(oyun_hedefleri) > 1:
                    h, w = frame.shape[:2]
                    oyun_hedefleri.sort(key=lambda p: (p[0] - w//2)**2 + (p[1] - h//2)**2)

                # 3. DURUM ANALİZİ
                is_empty = not is_bar_visible or c_hp == 4294967280
                hp_decreased = is_bar_visible and 0 < c_hp <= 90 and c_hp < self.last_hp
                if hp_decreased: self.last_action_time = now

                # EVRENSEL SIKIŞMA KONTROLÜ (Kademeli Müdahale)
                if is_bar_visible and (now - self.last_action_time) > 2.0:
                    if abs(c_x - self.last_x) < 0.01:
                        if self.stuck_retry_count == 0:
                            self.update_log.emit("⚠️ ENGEL-1: 1sn Mob temizliği (Space)...")
                            # 1. Aşama: Yol açmak için 1sn Space bas
                            pydirectinput.keyDown('space'); time.sleep(1.0); pydirectinput.keyUp('space')
                            self.last_state = "IDLE"; self.last_action_time = 0; self.stuck_retry_count = 1
                        else:
                            self.update_log.emit("⚠️ ENGEL-2: Space yetmedi! Akıllı kaçış başladı...")
                            old_x = self.curr_x; kurtuldu_mu = False
                            for k in ['w', 's', 'a', 'd']:
                                pydirectinput.keyDown(k); time.sleep(0.25); pydirectinput.keyUp(k)
                                time.sleep(0.2)
                                if abs(self.curr_x - old_x) > 0.02:
                                    self.update_log.emit(f"✅ Engel '{k}' ile aşıldı!"); kurtuldu_mu = True; break
                            
                            if not kurtuldu_mu:
                                self.update_log.emit("⚠️ Manevra yetersiz! Son çare: 3")
                                pydirectinput.press('3'); time.sleep(0.5)
                            
                            self.last_state = "IDLE"; self.last_action_time = time.time(); self.stuck_retry_count = 0

                # ANA DÖNGÜ
                is_attacking = is_bar_visible and (now - self.last_action_time) < 1.0
                if hp_decreased: self.stuck_retry_count = 0 # Can azaldığı an sayacı sıfırla
                
                if is_attacking:
                if is_attacking:
                    if self.last_state != "ATTACKING":
                        self.update_log.emit(f"⚔️ Metin Kesiliyor (HP: {c_hp})")
                        self.last_state = "ATTACKING"
                elif is_empty:
                    if self.last_state == "ATTACKING":
                        self.update_log.emit("💎 Metin Bitti. Ara...")
                        self.last_state = "IDLE"; self.last_action_time = 0
                    
                    if (now - self.last_action_time) > 2.2:
                        if len(oyun_hedefleri) > 0:
                            if self.is_rotating:
                                pydirectinput.keyUp('q'); pydirectinput.keyUp('t'); pydirectinput.keyUp('g')
                                self.is_rotating = False
                            abs_x = int(oyun_hedefleri[0][0] + monitor["left"])
                            abs_y = int(oyun_hedefleri[0][1] + monitor["top"])
                            ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                            time.sleep(0.06); pydirectinput.click()
                            self.last_action_time = now; self.last_state = "MOVING"
                            self.update_log.emit("🎯 Hedefe Tıklandı!")
                        elif not self.is_rotating:
                            pydirectinput.keyDown('q'); self.is_rotating = True; self.rotation_start_time = now
                        else:
                            dur = now - self.rotation_start_time
                            if 5 < dur < 7.5: pydirectinput.keyDown('t'); pydirectinput.keyUp('g')
                            elif 7.5 < dur < 10: pydirectinput.keyUp('t'); pydirectinput.keyDown('g')
                            elif dur > 10: pydirectinput.keyUp('t'); pydirectinput.keyUp('g'); self.rotation_start_time = now

                self.last_hp = c_hp
                self.last_x = c_x
                self.update_frame.emit(frame)

    def stop(self):
        self.running = False
        for k in ['q','t','g','w','s','a','d','3']: pydirectinput.keyUp(k)

class ModernBotUI(QMainWindow):
    def __init__(self):
        super().__init__()
        cfg = {"process_name": "metin2client.bin", "addr_x": "0A509268", "addr_hp": "43D7FD88"}
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r") as f: cfg.update(json.load(f))
            except: pass

        self.setWindowTitle("AI BOT - TURBO SYNC")
        self.setMinimumSize(1000, 750)
        self.setStyleSheet("QMainWindow { background: #0f172a; } QLabel { color: #cbd5e1; } QLineEdit { background: #1e293b; color: white; border: 1px solid #334155; border-radius: 5px; padding: 10px; } QPushButton { background: #22c55e; color: white; font-weight: bold; padding: 15px; border-radius: 10px; } #StopBtn { background: #ef4444; } QTextEdit { background: #000; color: #38bdf8; border-radius: 10px; font-family: 'Consolas'; }")

        central = QWidget(); self.setCentralWidget(central); layout = QHBoxLayout(central)
        left = QVBoxLayout(); left_panel = QFrame(); left_panel.setLayout(left); left_panel.setFixedWidth(320)
        left.addWidget(QLabel("🤖 TURBO SYNC SYSTEM", styleSheet="font-size: 18px; color: #38bdf8; font-weight: bold;"))
        self.proc_i = QLineEdit(cfg["process_name"]); left.addWidget(self.proc_i)
        self.addr_x = QLineEdit(cfg["addr_x"]); left.addWidget(self.addr_x)
        self.addr_hp = QLineEdit(cfg["addr_hp"]); left.addWidget(self.addr_hp)

        self.hud_x = QLabel("X: 0.00"); left.addWidget(self.hud_x)
        self.hud_hp = QLabel("HP: 0"); self.hud_hp.setStyleSheet("font-size: 18px; color: #f472b6; font-weight: bold;")
        left.addWidget(self.hud_hp)

        left.addStretch()
        self.start_btn = QPushButton("BAŞLAT"); self.start_btn.clicked.connect(self.start)
        left.addWidget(self.start_btn)
        self.stop_btn = QPushButton("DURDUR"); self.stop_btn.setObjectName("StopBtn"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop)
        left.addWidget(self.stop_btn)

        right = QVBoxLayout(); self.radar = QLabel("RADAR"); self.log = QTextEdit(); self.log.setFixedHeight(180)
        right.addWidget(self.radar); right.addWidget(self.log)
        layout.addWidget(left_panel); layout.addLayout(right); self.worker = None

    def start(self):
        self.worker = BotWorker(r"runs\detect\train6\weights\best.pt", self.proc_i.text(), self.addr_x.text(), self.addr_hp.text())
        self.worker.update_frame.connect(lambda f: self.radar.setPixmap(QPixmap.fromImage(QImage(cv2.cvtColor(f, cv2.COLOR_BGR2RGB).data, f.shape[1], f.shape[0], f.shape[1]*3, QImage.Format.Format_RGB888)).scaled(self.radar.width(), self.radar.height(), Qt.AspectRatioMode.KeepAspectRatio)))
        self.worker.update_log.connect(lambda m: self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}"))
        self.worker.update_data.connect(lambda x, hp: (self.hud_x.setText(f"X: {x:.2f}"), self.hud_hp.setText(f"HP: {int(hp) if hp != 4294967280 else 'BOS'}")))
        self.worker.start(); self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)

    def stop(self):
        if self.worker: self.worker.stop(); self.worker.wait()
        self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)

if __name__ == "__main__":
    app = QApplication(sys.argv); win = ModernBotUI(); win.show(); sys.exit(app.exec())
