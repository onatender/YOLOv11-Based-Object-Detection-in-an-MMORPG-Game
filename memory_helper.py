import pymem
from pymem import process
import time

class MemoryHelper:
    def __init__(self, process_name):
        """
        Oyunun ismini alarak Pymem bağlantısını kurar ve 
        ana modül adresini (Base) korumalı şekilde yakalar.
        """
        try:
            self.pm = pymem.Pymem(process_name)
            self.process_name = process_name
            self.module_base = self._find_module_base()
        except Exception as e:
            raise Exception(f"Bellek Bağlantısı Hatası: {str(e)}")

    def _find_module_base(self):
        """Modül adresini 'Signs' (FE00...) hatasını önleyerek 32-bit formatında bulur."""
        try:
            modules = list(self.pm.list_modules())
            for module in modules:
                if module.name.lower() == self.process_name.lower():
                    return module.lpBaseOfDll & 0xFFFFFFFF
        except: pass
        return self.pm.base_address & 0xFFFFFFFF

    def get_mz_signature(self):
        """Base adresin doğruluğunu (MZ Header) teyit etmek için imza döner."""
        try:
            return self.pm.read_uint(self.module_base)
        except:
            return 0

    def resolve_pointer(self, base_offset, offsets):
        """
        Cheat Engine tarzı pointer çözer.
        Örnek: [[Base + base_offset] + 0] + 88
        Kullanım: helper.resolve_pointer(0x0356148C, [0x0, 0x88])
        """
        try:
            # İlk yeşil adres (Base + Offset)
            addr = self.pm.read_uint(self.module_base + base_offset) & 0xFFFFFFFF
            if not addr or addr == 0: return None
            
            # Ara duraklar (Dereferencing)
            # Eğer [0x0, 0x88] varsa, son offset dereference değil toplama işlemidir.
            for offset in offsets[:-1]:
                addr = self.pm.read_uint(addr + offset) & 0xFFFFFFFF
                if not addr or addr == 0: return None
            
            # Final toplama
            return (addr + offsets[-1]) & 0xFFFFFFFF
        except:
            return None

    # --- KOLAY OKUMA METOTLARI ---
    def read_float(self, address):
        try: return self.pm.read_float(address)
        except: return 0.0

    def read_uint(self, address):
        try: return self.pm.read_uint(address)
        except: return 0

    def read_int(self, address):
        try: return self.pm.read_int(address)
        except: return 0

    def read_string(self, address, length=32):
        """Bellekten string (metin) değeri okur."""
        try:
            val = self.pm.read_string(address, length)
            return val.strip()
        except:
            return "Bilinmiyor"

    def write_string(self, address, value):
        """Bellekteki string değeri günceller."""
        try:
            self.pm.write_string(address, value)
            return True
        except:
            return False

    # --- KOLAY YAZMA METOTLARI ---
    def write_float(self, address, value):
        """Belleğe float değeri yazar."""
        try:
            self.pm.write_float(address, float(value))
            return True
        except: return False

    def write_int(self, address, value):
        """Belleğe integer değeri yazar."""
        try:
            self.pm.write_int(address, int(value))
            return True
        except: return False

    def write_uint(self, address, value):
        """Belleğe unsigned integer değeri yazar."""
        try:
            self.pm.write_uint(address, int(value))
            return True
        except: return False
