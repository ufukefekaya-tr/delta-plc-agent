# PLCWeave

**PLC mühendisliğini ajanlarla birlikte yürütün.**
Tek MCP. Modele göre araç seçimi. İzlenebilir sonuçlar.

[English](README.md) · Türkçe

PLCWeave; marka bağımsız MCP çekirdeğini, AX mühendislik araçlarını, ISPSoft dosya işlemlerini, DVP izlemeyi ve yeniden kullanılabilir ajan iş akışlarını bir araya getirir. Ajan tam hedefi tanır, uygun adaptörü seçer, çalışma kopyasında ilerler ve işlem kanıtını kaydeder.

## İlk deneme

Python 3.11+ sanal ortamında repo kökünden:

```text
python tools/install.py
python tools/demo.py
```

Demo; küçük bir komut listesini MPU biçimine ve geri dönüştürür, hedef yönlendirmesini ve kanıt kayıtlarını doğrular. IDE veya fiziksel PLC gerektirmez. Yalnız DVP/ISPSoft için `python tools/install.py --profile dvp` kullanılabilir.

## Neler sunuyor?

- **11 ortak MCP aracı:** hedef, yetenek, çalışma alanı, işlem, kanıt ve workflow yönetimi.
- **AX araçları:** proje oluşturma, ST/Ladder işlemleri, derleme ve job yönetimi, kaynak geri okuma ve artifact sözleşmeleri.
- **ISPSoft dosya araçları:** kapsayıcı inceleme, mevcut ST/değişken düzenleme, paket işlemleri ve desteklenen DVP IL↔MPU dönüşümleri.
- **DVP seri MCP:** `DVP14SS211R` için kimlik/durum, profil bağlı M/D işlemleri, parametre geri okuma ve watch.
- **WPLSoft ajan akışı:** Computer Use ile proje/derleme/yükleme adımları ve MCP ile online gözlem.
- **Altı ajan rolü:** analist, mimar, programcı, doğrulayıcı, 10. Adam ve Kaizen küratörü.
- **Taşınabilir kurulum:** proje yolları ve vendor araçları çalıştırıldığı bilgisayarda tanımlanır; çekirdek yeni üreticilere açıktır.

[Kurulum](docs/QUICKSTART.md) · [Özellikler ve doğrulama kapsamları](docs/FEATURES.md) · [İndeks](INDEX.md) · [Katkı rehberi](CONTRIBUTING.md)

Kurucu ve bakım sorumlusu: **[ufukefekaya-tr](https://github.com/ufukefekaya-tr)**. [MIT lisansı](LICENSE); üçüncü taraf katkıları [atıflarıyla](THIRD_PARTY_NOTICES.md) korunur.
