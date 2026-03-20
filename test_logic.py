import cv2
import numpy as np
import os

# Kullanıcının gönderdiği test resmi
img_path = 'test_img.png'

if not os.path.exists(img_path):
    print("Klasörde 'test_img.png' adında bir dosya yok! Lütfen resmin adının bu olduğundan emin olun.")
    exit(1)

# Görüntüyü oku
img = cv2.imread(img_path)
if img is None:
    print("Resim okunamadı!")
    exit(1)

# Görüntüyü BGR (Opencv varsayılan) formatından HSV formatına çevir
hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

# Kodumuzdaki Metin taşı / cyan, açık mavi renk filtresi aralıkları
lower_cyan = np.array([75, 120, 160], dtype=np.uint8)
upper_cyan = np.array([105, 255, 255], dtype=np.uint8)

# Maske (Sadece belirtilen renkteki piksellerin olduğu siyah/beyaz resim)
mask = cv2.inRange(hsv, lower_cyan, upper_cyan)

# Algılanan maskeyi hataları denetlemek için bilgisayara kaydet
cv2.imwrite('mask_result.png', mask)

# Tespit edilen beyaz bölgelerin etrafındaki çizgileri / alanları (konturları) bul
contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

# Çizim yapmak için resmin bir kopyasını alıyoruz ki orijinali bozulmasın
output_img = img.copy()

for contour in contours:
    area = cv2.contourArea(contour)
    # Alanı 10 pikselden büyük olan cisimleri dikkate al (Küçük yanılsamaları veya yazıları süzmek için)
    if area > 10:
        # En uygun çemberin merkez (x, y) koordinatlarını ve yarıçapını hesaplat
        (x, y), radius = cv2.minEnclosingCircle(contour)
        
        # Sadece belirli bir büyüklükteki hedefleri içine alsın (örn, 5'ten büyük, 300'den küçük yarıçaplar)
        if 5 < radius < 300:
            # Merkeze kırmızı(0,0,255), 3 piksel kalınlığında bir çember çiz
            # Cisme tam yapışmaması için radius'e 10 piksel dış boşluk (padding) ekliyoruz
            cv2.circle(output_img, (int(x), int(y)), int(radius + 10), (0, 0, 255), 3)

# Sonuç resmini kaydet
cv2.imwrite('test_result.png', output_img)
print("İşlem başarılı! Lütfen klasörünüzdeki 'test_result.png' resmini açarak kontrol edin.")
