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
                             QFrame, QTextEdit, QSizePolicy)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap

# YÖNETİCİ KONTROLÜ
def is_admin():
    try: return ctypes.windll.shell32.IsUserAnAdmin()
    except: return False
if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()

# Windows DPI Ayarı
try: ctypes.windll.user32.SetProcessDPIAware()
except: pass

class BotWorker(QThread):
    """Gelişmiş Bellek Teşhisli Bot Motoru"""
    update_frame = pyqtSignal(np.ndarray)
    update_log = pyqtSignal(str)
    update_data = pyqtSignal(float, float)

    def __init__(self, model_path, proc_name, addr_x, addr_hp):
        super().__init__()
        self.model = YOLO(model_path)
        self.proc_name = proc_name
        self.addr_x_hex = addr_x.strip()
        self.addr_hp_hex = addr_hp.strip()
        self.running = True
        self.pm = None
        self.target_x = 0
        self.target_hp = 0
        
        self.last_state = "IDLE"
        self.last_hp = 100.0
        self.last_x = 0.0
        self.last_action_time = time.time()
        self.is_rotating = False
        self.rotation_start_time = 0
        self.stuck_start_time = 0 # Debug log zamanlayıcısı için gerekli

        try:
            self.pm = Pymem(self.proc_name)
            self.target_x = int(self.addr_x_hex, 16)
            self.target_hp = int(self.addr_hp_hex, 16)
            print(f"Debugger: Adreslere bağlanıldı. X:{self.addr_x_hex} HP:{self.addr_hp_hex}")
        except Exception as e:
            print(f"❌ Başlatma Hatası: {e}")

    def run(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while self.running:
                now = time.time()
                sct_img = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(sct_img)[:, :, :3])
                
                # 1. HİBRİT BELLEK OKUMA (KESİN MOD)
                curr_x, curr_hp = 0.0, np.nan
                val_int, val_float, val_double = 0, 0.0, 0.0
                if self.pm:
                    try:
                        curr_x = self.pm.read_float(self.target_x)
                        val_int = self.pm.read_int(self.target_hp)
                        val_float = self.pm.read_float(self.target_hp)
                        val_double = self.pm.read_double(self.target_hp)

                        # Filtreyi kaldırdık, ne okuyorsak o!
                        curr_hp = val_double
                            
                    except Exception:
                        curr_hp = np.nan

                self.update_data.emit(curr_x, curr_hp)
                
                # Periyodik teşhis logu
                if int(now) % 5 == 0 and int(now) != self.stuck_start_time:
                    self.stuck_start_time = int(now)
                    self.update_log.emit(f"🔍 Canlı Veri -> Dbl: {val_double:.2e} | Int: {val_int}")

                # 2. YOLO TESPİTİ
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
                now = time.time()
                is_nan = np.isnan(curr_hp)
                
                # Can azalması tespiti
                hp_decreased = not is_nan and curr_hp < self.last_hp and 0.1 < curr_hp < 10000000
                if hp_decreased: self.last_action_time = now

                # SIKIŞMA KONTROLÜ
                if self.last_state == "MOVING" and (now - self.last_action_time) > 2.0:
                    if is_nan and abs(curr_x - self.last_x) < 0.02:
                        self.update_log.emit("⚠️ ENGEL TESPİT EDİLDİ! Kurtarma başlatılıyor...")
                        for k in ['s', 'a', 'd', '3', 'w']:
                            pydirectinput.keyDown(k); time.sleep(0.4); pydirectinput.keyUp(k)
                        self.last_state = "IDLE"; self.last_action_time = now

                # ANA BOT MANTIĞI
                is_attacking = not is_nan and curr_hp >= 0.1 and (now - self.last_action_time) < 1.0
                
                if is_attacking:
                    if self.last_state != "ATTACKING":
                        self.update_log.emit("⚔️ Metin Kesiliyor...")
                        self.last_state = "ATTACKING"
                elif is_nan or curr_hp < 0.1:
                    if self.last_state == "ATTACKING":
                        self.update_log.emit("💎 Metin Bitti. Yeni hedef aranıyor...")
                        self.last_state = "IDLE"; self.last_action_time = 0

                    if (now - self.last_action_time) > 2.2:
                        if len(oyun_hedefleri) > 0:
                            if self.is_rotating:
                                pydirectinput.keyUp('q'); pydirectinput.keyUp('t'); pydirectinput.keyUp('g')
                                self.is_rotating = False
                            
                            abs_x, abs_y = int(oyun_hedefleri[0][0] + monitor["left"]), int(oyun_hedefleri[0][1] + monitor["top"])
                            ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                            time.sleep(0.06)
                            pydirectinput.click()
                            self.last_action_time = now
                            self.last_state = "MOVING"
                            self.update_log.emit("🎯 Hedefe Tıklandı!")
                        elif not self.is_rotating:
                            pydirectinput.keyDown('q'); self.is_rotating = True; self.rotation_start_time = now
                        else:
                            dur = now - self.rotation_start_time
                            if 5 < dur < 7.5: pydirectinput.keyDown('t'); pydirectinput.keyUp('g')
                            elif 7.5 < dur < 10: pydirectinput.keyUp('t'); pydirectinput.keyDown('g')
                            elif dur > 10: pydirectinput.keyUp('t'); pydirectinput.keyUp('g'); self.rotation_start_time = now

                self.last_hp = curr_hp
                self.last_x = curr_x
                self.update_frame.emit(frame)

    def stop(self):
        self.running = False
        for k in ['q','t','g','w','s','a','d','3']: pydirectinput.keyUp(k)

class ModernBotUI(QMainWindow):
    def __init__(self):
        super().__init__()
        # Ayarları yükle
        cfg = {"process_name": "metin2client.bin", "addr_x": "0A509268", "addr_hp": "43D7FD84"}
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r") as f: cfg.update(json.load(f))
            except: pass

        self.setWindowTitle("AI BOT - HYBRID PRO")
        self.setMinimumSize(1000, 750)
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QLabel { color: #cbd5e1; font-family: 'Segoe UI'; font-size: 13px; }
            QLineEdit { background: #1e293b; border: 1px solid #334155; border-radius: 6px; padding: 10px; color: #fff; }
            QPushButton { border-radius: 10px; font-weight: bold; font-size: 15px; padding: 15px; color: white; background: #22c55e; }
            QPushButton:disabled { background: #334155; color: #94a3b8; }
            #StopBtn { background: #ef4444; }
            QTextEdit { background: #000; color: #38bdf8; border: none; border-radius: 10px; font-family: 'Consolas'; }
        """)

        central = QWidget(); self.setCentralWidget(central); layout = QHBoxLayout(central)
        left = QVBoxLayout(); left_panel = QFrame(); left_panel.setLayout(left); left_panel.setFixedWidth(320)
        
        left.addWidget(QLabel("🤖 AI METIN2 HYBRID", styleSheet="font-size: 20px; font-weight: bold; color: #38bdf8; margin: 10px 0;"))
        left.addWidget(QLabel("Proses:"))
        self.proc_i = QLineEdit(cfg["process_name"]); left.addWidget(self.proc_i)
        left.addWidget(QLabel("Koordinat X (Hex):"))
        self.addr_x = QLineEdit(cfg["addr_x"]); left.addWidget(self.addr_x)
        left.addWidget(QLabel("Metin Canı (Hex):"))
        self.addr_hp = QLineEdit(cfg["addr_hp"]); left.addWidget(self.addr_hp)

        self.hud_x = QLabel("KOORDİNAT X: 0.00"); self.hud_x.setStyleSheet("font-size: 16px; color: #fbbf24; margin-top: 20px; font-weight: bold;")
        self.hud_hp = QLabel("METİN CANI: 0.00"); self.hud_hp.setStyleSheet("font-size: 16px; color: #f472b6; font-weight: bold;")
        left.addWidget(self.hud_x); left.addWidget(self.hud_hp)

        left.addStretch()
        self.start_btn = QPushButton("BOTU BAŞLAT"); self.start_btn.clicked.connect(self.start)
        left.addWidget(self.start_btn)
        self.stop_btn = QPushButton("DURDUR"); self.stop_btn.setObjectName("StopBtn"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop)
        left.addWidget(self.stop_btn)

        right = QVBoxLayout(); self.radar = QLabel("RADAR BEKLENİYOR..."); self.radar.setAlignment(Qt.AlignmentFlag.AlignCenter); self.radar.setStyleSheet("background: black; border-radius: 15px;")
        self.log = QTextEdit(); self.log.setFixedHeight(180)
        right.addWidget(self.radar); right.addWidget(self.log)
        layout.addWidget(left_panel); layout.addLayout(right)
        self.worker = None

    def start(self):
        self.worker = BotWorker(r"runs\detect\train6\weights\best.pt", self.proc_i.text(), self.addr_x.text(), self.addr_hp.text())
        self.worker.update_frame.connect(self.display_frame)
        self.worker.update_log.connect(lambda m: self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}"))
        self.worker.update_data.connect(lambda x, hp: (self.hud_x.setText(f"KOORDİNAT X: {x:.2f}"), self.hud_hp.setText(f"METİN CANI: {('NaN' if np.isnan(hp) else f'{hp:.2f}')}")))
        self.worker.start(); self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True)

    def stop(self):
        if self.worker: self.worker.stop(); self.worker.wait()
        self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False)

    def display_frame(self, frame):
        h, w, ch = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qt_img = QImage(rgb.data, w, h, w*ch, QImage.Format.Format_RGB888)
        self.radar.setPixmap(QPixmap.fromImage(qt_img).scaled(self.radar.width(), self.radar.height(), Qt.AspectRatioMode.KeepAspectRatio))

if __name__ == "__main__":
    app = QApplication(sys.argv); win = ModernBotUI(); win.show(); sys.exit(app.exec())
