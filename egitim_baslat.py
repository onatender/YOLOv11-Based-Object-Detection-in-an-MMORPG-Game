import json
import os
import shutil
from ultralytics import YOLO

# Kullanıcının yanlış formatta indirdiği COCO dosyasını YOLO txt'ye çevirelim
coco_file = "train/_annotations.coco.json"

with open(coco_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

yolo_ana = 'c:/Users/onate/Desktop/Dosyalar/burahileyazmaolayi/yolo_train'
images_dir = os.path.join(yolo_ana, 'images/train')
labels_dir = os.path.join(yolo_ana, 'labels/train')

if os.path.exists(yolo_ana):
    shutil.rmtree(yolo_ana)

os.makedirs(images_dir, exist_ok=True)
os.makedirs(labels_dir, exist_ok=True)

images = {img['id']: img for img in data['images']}
kategoriler = {cat['id']: idx for idx, cat in enumerate(data['categories'])}
kategori_isimleri = [cat['name'] for cat in data['categories']]

# Resimleri yeni yolo dizinine taşı
for img in data['images']:
    src = os.path.join('train', img['file_name'])
    
    # Windows'un zip çıkarma sonrası Türkçe karakterleri bozmasını engellemek için kontrol:
    if not os.path.exists(src):
        try:
            # Utf-8'in Latin-1 gibi çevrilmesindeki tipik bozulmayı test edelim
            bozuk_ad = img['file_name'].encode('utf-8').decode('cp1252')
        except:
            bozuk_ad = img['file_name'].replace('görüntüsü', 'gÃ¶rÃ¼ntÃ¼sÃ¼')
            
        src_bozuk = os.path.join('train', bozuk_ad)
        
        if os.path.exists(src_bozuk):
             src = src_bozuk
        else:
             # Eğer illa bulamazsa düz düzeltme yap:
             src_bozuk_2 = os.path.join('train', img['file_name'].replace('görüntüsü', 'gÃ¶rÃ¼ntÃ¼sÃ¼'))
             if os.path.exists(src_bozuk_2):
                 src = src_bozuk_2
                 
    dst = os.path.join(images_dir, img['file_name'])
    shutil.copy(src, dst)
    # Ayrıca boş bir txt oluştur ki boş resimlerde hata vermesin
    txt_name = img['file_name'].rsplit('.', 1)[0] + '.txt'
    open(os.path.join(labels_dir, txt_name), 'w').close()

# İlgili resimlere etiket koordinatları (X,Y) yaz
for ann in data['annotations']:
    img_info = images[ann['image_id']]
    w_img, h_img = float(img_info['width']), float(img_info['height'])
    yolo_sinifi = kategoriler[ann['category_id']]
    # COCO Box formatı: (Üstsol_X, ÜstSol_Y, Genişlik, Yükseklik). String'leri Float'a dönüştürüyoruz:
    x, y, w, h = map(float, ann['bbox']) 
    
    # YOLO için yüzde oranlarına (0.00 ile 1.00 arası) çevir
    x_center = (x + w / 2.0) / w_img
    y_center = (y + h / 2.0) / h_img
    norm_w = w / w_img
    norm_h = h / h_img
    
    txt_name = img_info['file_name'].rsplit('.', 1)[0] + '.txt'
    with open(os.path.join(labels_dir, txt_name), 'a') as f:
        f.write(f"{yolo_sinifi} {x_center:.6f} {y_center:.6f} {norm_w:.6f} {norm_h:.6f}\n")

# Konfigürasyon Dosyası
yaml_content = f"""
path: '{yolo_ana}'
train: 'images/train'
val: 'images/train'

nc: {len(kategori_isimleri)}
names: {kategori_isimleri}
"""
with open('data.yaml', 'w', encoding='utf-8') as f:
    f.write(yaml_content.strip())

print("✅ Format dönüşümü başarılı! Eğitime geçiliyor...")

if __name__ == '__main__':
    # Yeni ve Daha Güçlü YOLOv11 Eğitimi
    print("🚀 Daha Güçlü Yapay Zeka Beyni Eğitimi (YOLO11 Nano) Başlatılıyor...")
    # Modelimizi YOLOv11'e yükseltiyoruz
    # Epochs=150: Daha fazla tur atarak modelin uzmanlaşmasını sağlar.
    # Imgsz=1024: Uzaktaki küçük taşları daha iyi tanıması için yüksek çözünürlükte eğitilir.
    model = YOLO('yolo11n.pt') 
    
    # patience=50: Eğer model 50 tur boyunca gelişmezse eğitimi akıllıca bitirir.
    results = model.train(
        data='data.yaml', 
        epochs=150, 
        imgsz=1024, 
        batch=-1,    # RTX 3050'nin belleğine göre otomatik en iyi batch size'ı seçer
        workers=0, 
        patience=50,
        device=0     # Ekran kartınızı (GPU) kullanır
    )

    print("🎯 MÜKEMMEL! Güçlendirilmiş eğitim başarıyla bitti! Yeni oluşan '.pt' modeli artık çok daha isabetli çalışacak.")
