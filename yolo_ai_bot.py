import cv2
import numpy as np
import mss
import time
import ctypes
import pydirectinput
from ultralytics import YOLO
import threading
import sys

# YÖNETİCİ YETKİSİ KONTROLÜ (Admin Check)
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

if not is_admin():
    # Eğer yönetici değilse, kendini yönetici olarak yeniden başlatır
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()

# Windows DPI ve OpenCV Optimizasyonları
try:
    ctypes.windll.user32.SetProcessDPIAware()
except:
    pass

cv2.setUseOptimized(True)
cv2.setNumThreads(4)

# Model ve Pydirectinput ayarları (Yeni ve Saf %99.5 Doğruluklu Model)
pydirectinput.FAILSAFE = False
pydirectinput.PAUSE = 0.0
model = YOLO(r"runs\detect\train6\weights\best.pt")

class FastCapture:
    """Ekranı en yüksek hızda (Arka planda) yakalayan motor"""
    def __init__(self, monitor):
        self.monitor = monitor
        self.frame = None
        self.stopped = False

    def start(self):
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self

    def update(self):
        # ÖNEMLİ: Windows'ta MSS objesi hangi thread'de kullanılacaksa orada yaratılmalıdır.
        with mss.mss() as sct:
            while not self.stopped:
                sct_img = sct.grab(self.monitor)
                # Alfa kanalını atıp bütünselliği (contiguous) koruyoruz
                self.frame = np.ascontiguousarray(np.array(sct_img)[:, :, :3])

    def stop(self):
        self.stopped = True

print("🚀 Turbo FPS Botu Başlatılıyor...")

with mss.mss() as sct:
    monitor = sct.monitors[1]
    # Yakalama motorunu ayrı thread'de başlat
    cap = FastCapture(monitor).start()
    
    cv2.namedWindow("YOLO_Radar", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("YOLO_Radar", 640, 480) # Radar daha az kaynak yesin
    
    last_click_time = 0
    is_rotating = False

    # FPS Hesaplayıcı
    p_time = time.time()

    while True:
        frame = cap.frame
        if frame is None:
            continue

        # Yapay Zeka Tespiti (TURBO + YÜKSEK ÇÖZÜNÜRLÜK: imgsz=1024)
        # Uzaktaki küçük nesneleri yakalamak için çözünürlüğü artırdık. 
        results = model.predict(source=frame, conf=0.10, device='0', half=True, imgsz=1024, verbose=False)
        oyun_hedefleri = []

        # Çizimleri ana kareye hızlıca işle
        for result in results:
            boxes = result.boxes
            for box in boxes:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                x_center, y_center = int((x1 + x2) / 2), int((y1 + y2) / 2)
                radius = int(max((x2 - x1), (y2 - y1)) / 2) + 5
                cv2.circle(frame, (x_center, y_center), radius, (0, 0, 255), 2)
                oyun_hedefleri.append((x_center, y_center))

        # En yakını sırala
        if len(oyun_hedefleri) > 1:
            h, w = frame.shape[:2]
            oyun_hedefleri.sort(key=lambda p: (p[0] - w//2)**2 + (p[1] - h//2)**2)

        # Bot Mantığı (Click & Rotation)
        curr = time.time()
        if (curr - last_click_time) > 10:
            if len(oyun_hedefleri) > 0:
                if is_rotating:
                    pydirectinput.keyUp('q')
                    is_rotating = False
                
                # Global koordinata çevir ve tıkla
                abs_x, abs_y = int(oyun_hedefleri[0][0] + monitor["left"]), int(oyun_hedefleri[0][1] + monitor["top"])
                ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
                time.sleep(0.04)
                pydirectinput.click()
                last_click_time = curr
                print(f"⚔️ {time.strftime('%H:%M:%S')} - Taşa tıklandı!")
            elif not is_rotating:
                pydirectinput.keyDown('q')
                is_rotating = True
                print("🔄 Arama yapılıyor...")

        # FPS Hesapla ve Yazdır
        c_time = time.time()
        fps = 1 / (c_time - p_time)
        p_time = c_time
        cv2.putText(frame, f"FPS: {int(fps)}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # Radarı göster - saniyede 1ms bekle (Maksimum hız için)
        cv2.imshow("YOLO_Radar", frame)
        if cv2.waitKey(1) & 0xFF == ord('x'):
            cap.stop()
            break

cv2.destroyAllWindows()
