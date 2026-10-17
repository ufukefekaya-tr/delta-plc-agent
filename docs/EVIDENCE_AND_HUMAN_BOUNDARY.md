# Kanıt Seviyeleri ve İnsan Sınırı

Bu sistem, bir iddianın ne kadar doğrulandığını ve hangi faaliyetin yalnız insana ait olduğunu açık tutar.

## Kanıt seviyeleri

| Seviye | Anlam | Kim kapatabilir |
|---|---|---|
| `CLAIM` | Kullanıcı, rapor, sohbet veya commit iddiası | Yeni kanıt gerekir |
| `SOURCE_REVIEWED` | Güncel olduğu doğrulanan metin kaynak incelendi | Ajan |
| `STATIC_CHECKED` | Statik/yapısal kontroller geçti | Ajan |
| `SIMULATED` | Tanımlı model ve senaryolar geçti | Ajan |
| `IDE_COMPILED` | Hash/IDE/model bağlı gerçek vendor compile | Geçerli ajan kapsamı veya insan kanıtı |
| `FILE_TRANSFORM` | Kapsayıcı/IL/package işlemi; compile değildir | Dosya adaptörü |
| `HUMAN_IDE_COMPILED` | Gerçek hedef IDE import/build sonucu alındı | İnsan kanıtı |
| `HUMAN_BENCH_VERIFIED` | Enerjisiz/düşük riskli bench doğrulaması yapıldı | Yetkili insan |
| `HUMAN_FAT_ACCEPTED` | Tanımlı FAT sahada kabul edildi | Yetkili insan |

Bir üst seviye alttakinden otomatik türemez. Statik PASS, derlenebilirlik veya fiziksel güvenlik kanıtı değildir. Simülasyon gerçek sensör polaritesi, mekanik atalet, kablo ve sürücü ayarını doğrulamaz.

## Baseline ayrımı

Aşağıdaki varlıklar aynı şey değildir:

- Metin kaynak
- Symbol/import tablosu
- Vendor IDE içindeki proprietary proje
- Derlenen binary
- PLC'de çalışan yüklü sürüm
- HMI'da çalışan sürüm
- Saha kablolaması ve mekanik gerçeklik

Handoff her biri için kimlik, tarih/hash ve kanıt seviyesini ayrı yazar. “Dosyada var” ifadesi “PLC'de çalışıyor” anlamına gelmez.

## Ajanın üretebileceği işler

- Gereksinim, risk ve mimari analizi
- ST taslağı
- Açıklamalı Ladder network reçetesi
- I/O, değişken, alarm ve sahiplik tabloları
- Target profile ve import hazırlık notları
- Statik/property/fault test planları
- Monitor listesi ve beklenen trace
- İnsan IDE/FAT checklist'i
- Rollback ve kaizen kaydı
- İnsan tarafından getirilen build veya saha kanıtının analizi

## Yürütme yetkisi

Çevrimdışı vendor IDE ve canlı hedef işlemlerinin sınırı [kanonik politikanın Bölüm 2](../SYSTEM_PROMPT.md#2-yetki-çevrimdışı-ide-ve-canlı-hedef) içindedir. Katalog kaydı uyumluluk adayıdır; capability uygulaması, doğrulama düzeyi ve oturum yetkisi ayrı değerlendirilir. Repo guard PLC yetkisi üretmez. Fiziksel kablolama/emniyet/FAT/SAT kabulünü yetkili insan kapatır. Generic force veya safety bypass ortak araç değildir.

## Fast mod kanıtı

Fast çıktı `FAST_UNVERIFIED` durumundadır. Bu bir çalışma engeli değil, kanıt sınıfıdır. Fast mod tüm ajan kapılarını atlayabilir; geçerli execution/target yetkisini veya fiziksel kabul sınırını atlayamaz.

## Handoff asgari içeriği

- Baseline ve üretilen dosya hashleri
- Hedef profil ve açık `TBD` alanları
- Değişen davranış ve tek-yazar sahipliği
- Atlanan veya geçen ajan kontrolleri
- Derlenmemiş/sahada denenmemiş iddialar
- İnsan IDE adımları
- Enerjisiz, düşük hız ve fault test sırası
- Monitor/trace listesi
- Rollback paketi
- İnsan sonucu ve imza alanı
