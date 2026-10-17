# Target Profile Rehberi

Universal çekirdek hedef PLC, IDE veya firmware gerçeğini uydurmaz. Her gerçek proje `templates/TARGET_PROFILE.md` dosyasını doldurur.

## Profilin amacı

Target profile, proses mantığı ile üreticiye özgü uygulama gerçekleri arasındaki sınırdır. Çekirdek “relative motion gerekir” diyebilir; hangi instruction, FB, task veya kanala çevrileceği profile ve resmî hedef kanıtına bağlıdır.

## Zorunlu başlıklar

- Controller/CPU ailesi ve tam model
- Firmware ve IDE/compiler sürümü
- Desteklenen IEC 61131-3 dilleri
- Program step/bellek ölçüm yöntemi
- Adres grameri ve kanal/bank sınırları
- Veri tipleri, word genişliği ve alignment
- Cast, division, rounding ve overflow davranışı
- Retentive alan ve power-cycle davranışı
- Cyclic/event task modeli ve gerçek yürütme sırası
- Timer/timebase ve scan/jitter bilgisi
- Edge/level çağrı semantiği
- Motion, HSC, analog ve communication capability'leri
- Busy/done/error/abort/stop handshake'i
- Elektriksel I/O tipi ve aktif seviye
- Import/export biçimi, encoding, delimiter ve BOM
- Simulator ve destek sınırları
- İnsan IDE import/build/download adımları

## Her iddiaya kanıt etiketi

| Etiket | Anlam |
|---|---|
| `VERIFIED` | Resmî hedef kaynağı veya insan IDE/ölçüm kanıtı var |
| `INFERRED` | Tutarlı fakat doğrudan kanıtlanmamış çıkarım |
| `UNVERIFIED` | Kullanıcı/rapor iddiası veya açık varsayım |
| `TBD` | Bilgi yok; tahmin yapılmayacak |

Kaynak revizyonu ve erişim tarihi yazılır. Farklı firmware veya IDE sürümündeki bilgi sessizce aynı kabul edilmez.

## Dil ve step karşılaştırması

ST ile Ladder step maliyeti yalnız hedefin gerçek compiler raporu veya güvenilir target belgesiyle karşılaştırılır. Ölçüm yoksa sayısal üstünlük iddia edilmez. Bellek kritikse küçük eşdeğer mikrotaslaklar geçerli kapsamda aynı compiler ayarlarıyla ölçülür.

## Profile taşınacak, çekirdeğe taşınmayacak bilgiler

- Özel register/relay adları
- Motion ve communication instruction isimleri
- Retentive adres aralıkları
- Pulse kanal eşlemeleri
- Import CSV kolonları ve encoding ayrıntıları
- Özel fault/done/stop bitleri
- Firmware workaround'ları

Bu bilgiler ancak uygulanabilirlik sınırıyla proje profile'ında bulunur. Bir projede doğru olmaları onları universal kural yapmaz.
