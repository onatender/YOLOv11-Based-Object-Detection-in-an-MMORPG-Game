import os
import cv2
import numpy as np
import shutil

# Klasör Yolları
kaynak_klasor = 'metin_ds'
dataset_ana_klasoru = 'yolo_dataset'
images_klasoru = os.path.join(dataset_ana_klasoru, 'images', 'train')
labels_klasoru = os.path.join(dataset_ana_klasoru, 'labels', 'train')

# Daha önceden oluşmuşsa silip yeni bir boş klasör ağacı yapıyoruz
if os.path.exists(dataset_ana_klasoru):
    shutil.rmtree(dataset_ana_klasoru)

os.makedirs(images_klasoru, exist_ok=True)
os.makedirs(labels_klasoru, exist_ok=True)

# Renk filtrelerimiz (önceki başarılı algoritma)
lower_cyan = np.array([75, 120, 160], dtype=np.uint8)
upper_cyan = np.array([105, 255, 255], dtype=np.uint8)

resim_dosyalari = [f for f in os.listdir(kaynak_klasor) if f.endswith(('.png', '.jpg', '.jpeg'))]

label_sayisi = 0

for resim_adi in resim_dosyalari:
    resim_yolu = os.path.join(kaynak_klasor, resim_adi)
    img = cv2.imread(resim_yolu)
    
    if img is None:
        continue
    
    # Resim yüksekliği ve genişliği
    h_img, w_img, _ = img.shape
    
    # Görüntüyü HSV renk tipine çevir ve filtrele
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lower_cyan, upper_cyan)
    
    # Konturları (şekillerin çizgilerini) bul
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    bboxes = []
    
    for contour in contours:
        area = cv2.contourArea(contour)
        if area > 5:  # Ufak tefek yanılgıları ele
            # Şekli çevreleyen en küçük düz dikdörtgenin (x, y koordinatları ile genişlik, yükseklik)
            x, y, w, h = cv2.boundingRect(contour)
            
            # YOLO Algoritması normal piksel biriminden ("Piksel: x=100, y=100") Anlamaz.
            # Yüzdelik oran biriminden ("Resmin %5'ini kaplıyor") anlar. 
            # Bu yüzden 0 ile 1 arasına sıkıştıracak bir formül üzerinden merkez, yükseklik ve genişlik x,y koordinatları oluşturuyoruz
            x_center = (x + w / 2.0) / w_img
            y_center = (y + h / 2.0) / h_img
            norm_w = w / float(w_img)
            norm_h = h / float(h_img)
            
            # Nesne sınıf numarası 0'dır (0 = Metin Taşı).
            bboxes.append(f"0 {x_center:.6f} {y_center:.6f} {norm_w:.6f} {norm_h:.6f}\n")
    
    # Eğer resmin içerisinden mantığımız metin taşını tespit ettiyse;
    if bboxes:
        label_sayisi += 1
        # Metin belgesini oluştur ve içerisine kaydet
        txt_dosya_adi = resim_adi.rsplit('.', 1)[0] + '.txt'
        with open(os.path.join(labels_klasoru, txt_dosya_adi), 'w') as f:
            f.writelines(bboxes)
        
        # Resmi de dataset klasörüne taşı (veya kopyala)
        shutil.copy(resim_yolu, os.path.join(images_klasoru, resim_adi))

# Son olarak YOLO'ya hedef verilerin ve sınıfların nerede olduğunu/ne olduğunu söyleyen Yapılandırma Dosyası (YAML) oluştur:
yaml_icerigi = f"""
path: c:/Users/onate/Desktop/Dosyalar/burahileyazmaolayi/yolo_dataset
train: images/train
val: images/train

nc: 1
names: ['metin_tasi']
"""

with open('metin_ai_config.yaml', 'w') as y_file:
    y_file.write(yaml_icerigi.strip())

print(f"Işlem Tamamlandı! {len(resim_dosyalari)} adet resmin {label_sayisi} tanesi makinenin eğitim yapabileceği %100 uyumlu etiketlere dönüştürüldü ve dataset hazırlandı.")
