import sys
import cv2
import numpy as np
import mss
from PyQt5 import QtWidgets, QtCore, QtGui
import time

class ScreenProcessor(QtCore.QThread):
    circles_detected = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.running = True
        
        # 4bf8ff ve 53fbff (Light Blue / Cyan) tonlarında RGB renk kodlarıdır.
        # Bu renklerin HSV (Hue, Saturation, Value) karşılıklarına tekabül eden hassas ayarları yapıyoruz.
        # OpenCV'de Hücre (H) [0-179], Doygunluk (S) [0-255] ve Değer (V) [0-255] aralığındadır.
        # Bu renklerin OpenCV Hue karşılığı yaklaşık olarak 90-95 aralığına denk gelmektedir.
        
        self.lower_cyan = np.array([75, 120, 160], dtype=np.uint8)
        self.upper_cyan = np.array([105, 255, 255], dtype=np.uint8)

    def run(self):
        # mss kütüphanesini ana thread yerine, ekran işlemesinin yapılacağı arkaplan thread'i içerisinde başlatıyoruz
        with mss.mss() as sct:
            # Tüm monitörünüzün ekran görüntüsünü almak için monitor 1 kullanılır (ana ekran)
            monitor = sct.monitors[1]
            while self.running:
                # 1. Ekranı Yakala
                sct_img = sct.grab(monitor)
                # 2. Numpy dizisine dönüştür
                img = np.array(sct_img)
            
            # Görüntüyü maskelemek ve renk analizi yapmak için HSV renk uzayına çeviriyoruz
            # BGRA'dan BGR'ye geçmeden direkt BGR2HSV yapmak sorun çıkartabilir,
            # Bu yüzden BGRA'dan BGR'ye ve ardından BGR'den HSV'ye çeviriliyoruz.
            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR) # sct, bgra çeviriyor
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            
            # 3. Belirttiğimiz renk tonları için bir maske oluşturuyoruz (beyaz kısımlar tespit edilen yerlerdir)
            mask = cv2.inRange(hsv, self.lower_cyan, self.upper_cyan)
            
            # 4. Tespit edilen alanların konturlarını bul
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            points = []
            for contour in contours:
                area = cv2.contourArea(contour)
                if area > 10:  # Çok küçük piksel yanılgalarını veya görüntü kirliliğini engellemek için minimum alan
                    # Tespit edilen rengin etrafını çevreleyecek en küçük çemberin x,y koordinatlarını ve yarıçapını bulur
                    (x, y), radius = cv2.minEnclosingCircle(contour)
                    
                    # Sayfayı kaplayan devasa çemberler çizilmesini engelle
                    if radius < 500:
                        # (x uzaklığı, y uzaklığı, yarıçap + 5 kalınlık/margin)
                        points.append((int(x), int(y), int(radius + 5)))
            
            # Bulunan çemberleri UI (Saydam Ekran) tarafına gönderiyoruz
            self.circles_detected.emit(points)
            
            # FPS Kısıtlaması (Çok hızlı olursa CPU'yu 100% yorar ~60 FPS için 16 ms)
            time.sleep(0.016)

    def stop(self):
        self.running = False
        self.quit()
        self.wait()


class OverlayWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        
        # Pencereyi, tıklanabilir olmayan (arkasına tıklanabilen), saydam ve her zaman en üstte duran bir araca çeviriyoruz.
        self.setWindowFlags(
            QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.Tool |
            QtCore.Qt.WindowTransparentForInput
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        
        # Pencerenin boyutunu ana ekranla aynı ölçüye getir
        screen_geometry = QtWidgets.QApplication.desktop().screenGeometry()
        self.setGeometry(screen_geometry)
        
        # Ekranda çizilecek olan noktalar / çemberler
        self.points = []
        
        # Ekran Görüntü İşleme Thread'ini (iş parçacığı) başlatıyoruz
        self.processor = ScreenProcessor()
        self.processor.circles_detected.connect(self.update_points)
        self.processor.start()

    @QtCore.pyqtSlot(list)
    def update_points(self, points):
        self.points = points
        # PaintEvent'i tetikler ve ekrandaki çizimler güncellenir
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        
        # Anti-Aliasing ile yumuşak/kaliteli bir daire çizimi olmasını sağlıyoruz
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        
        # Kırmızı ve kalınlığı 3 piksel olan bir çizici nesne (kalem) ayarlıyoruz
        pen = QtGui.QPen(QtCore.Qt.red, 3)
        painter.setPen(pen)
        
        # Arraydeki tüm nokta ve daireleri gezip ekrana çiziyoruz
        for x, y, radius in self.points:
            # x,y noktası merkezlidir ve x, y yarıçap genişliğine sahiptir
            painter.drawEllipse(QtCore.QPoint(x, y), radius, radius)

    def closeEvent(self, event):
        self.processor.stop()
        super().closeEvent(event)

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    
    # Overlay (Şeffaf Katman) başlat
    overlay = OverlayWindow()
    overlay.show()
    
    # Programı kapatmak için konsolda işlemi durdurmalısınız (Ctrl + C)
    print("Çizim programı başlatıldı. Kapatmak için terminale tıklayıp Ctrl+C'ye basabilirsiniz.")
    
    sys.exit(app.exec_())
