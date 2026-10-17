# KZ-20261013-002 — SS2 program aktarımı ve belirsiz sonuç

- Durum: `STATIC_CHECKED`
- Tarih: 2026-10-13
- Baseline/hash: `1314f26b814f8cb574046ea4ad576683f3447b31`; WPLSoft binary `f0c002d6dc7fab80ea942498eac9ceec19aaef037c4d3e7e418a7f64df5f42c1`
- Requirement/ADR: Kullanıcının MCP ile güncel onaylı V7/en son programı yedekleme, yükleme ve bağımsız geri doğrulama talebi; Computer Use yok.

## Gözlem

- Beklenen davranış: Online okuma sonrasında gerçek program aktarımı ve bağımsız karşılaştırma.
- Gerçek davranış: Önceki FAST sürümünde program aktarımı unavailable guard idi. V5 adayının kullanıcı V7'ye geçtikten sonra korunması yanlış revizyon riski yarattı.
- İlk containment: Eski aday/enrollment kapatıldı; kaynak değişmedi, yeni goal'da PLC'ye program/mode paketi gönderilmedi. Kullanıcı ayrıldığı için eski attended/izole koşullar taşınmadı.
- Kanıt seviyesi: Native ikiliden statik sözleşme ve yazılım testleri; yeni canlı program aktarımı NOT_RUN.

## Kök neden hipotezi

- Ana hipotez: Modbus D-register okuması ile native program alanı, kaynak dosya SHA'sı ile PLC bytecode SHA'sı ve geçmiş yetki ile güncel fiziksel koşullar birbirine karıştırılabilir.
- Alternatif hipotezler: HMI CDC/COM paylaşımı, private64 ACK farkı, protected hedef veya initial-value block mevcut hedefte destek sınırına neden olabilir.
- 10. Adam karşı görüşü: Geçerli ACK veya fixture PASS, gerçek PLC'nin doğru programı çalıştırdığını ve güç çevriminde koruduğunu kanıtlamaz.

## En küçük değişiklik

- Değişen davranış: Ayrı native codec ve typed program-contract qualification, raw upload, immutable prepare, once-only execute, independent reconcile eklendi.
- Etkilenen owner/task/interface: delta.dvpserial, Service context/evidence, mevcut W1/W5/W6/W7; G-DVP-5, YALDIZ_PLC_YAZILIM kaynak/port koordinasyonu.
- Değişmeyen davranış: Varsayılan kapalı aktarım profili guard'ı, bounded M/D okuması ve ayrı sabit M1072 kontrolü.
- Risk ve safe-state: RUN'da aktarım reddedilir; otomatik STOP/RUN yok. Her mode/yazma öncesi fsync journal ve süreçler arası hedef kilidi; belirsiz yazma UNKNOWN, replay yok. Güvenli son durum taze STOP gözlemidir.

## Doğrulama

| Test | Tür | Sonuç | Kanıt |
|---|---|---|---|
| Native SS2 çözümü, checksum, operand/END sınırı | Yazılım | PASS, 10 test | tests/dvpserial/test_native.py |
| Mevcut seri/kontrol/MCP regresyonu | Yazılım | PASS, 44 test | tests/dvpserial/ |
| Universal runtime | Yazılım | PASS, 25 test | tests/test_universal_runtime.py |
| Yeni transfer/SDK/fault senaryoları | Yazılım | PASS, 38 yeni test; toplam DVP 92/runtime 25 | reports/dvp-transfer/software-tests.json |
| Private ACK, full PLC backup, download/readback | Canlı | NOT_RUN | reports/dvp-transfer/qualification.json |

## İnsan kapıları

- IDE compile: `PENDING` — Güncel onaylı ve kaydedilmiş kaynak üretim ajanından doğrulanacak; codec/checksum repair derleme değildir.
- Bench: `PENDING` — Güncel attended/izole koşullar ve port devri yok; eski RUN testi yeni yükleme yetkisi değildir.
- FAT: `PENDING` — Fiziksel ısıtma ve güç çevrimi yapılmadı.

## Rollback

- Yedek: Değişmez kaynak kopyası, tam 7920-word raw image, descriptor ve parametre manifesti; ham bytecode'dan sahte editor container üretilmez.
- Adımlar: Belirsiz hedefte yeni yazma yok; journal ile bağımsız reconcile yapılır. Geri yükleme, ayrıca geçerli güncel hedef/oturum ve onaylı native kaynakla yeni plan ister; bu paket otomatik rollback başlatmaz.
- Kabul ölçütü: Bağımsız program/tail/descriptor/parametre karşılaştırması ve taze STOP.

## Sonuç

- Başarılı/başarısız/kısmi/revert: Yazılım ve 117 test PASS; canlı yükleme kabulü açık.
- Saha sonucu: Bu goal PLC programını değiştirmedi; private ACK ve gerçek yükleme kanıtı bekliyor.
- Supersedes/revert ilişkisi: KZ-20261013-001 içindeki unavailable aktarım sınırını ayrı enrollment ile genişletir; geçmiş canlı okuma kanıtı korunur.

## Genellenebilir ders

- Universal kural: En son onaylı kaynak, native payload, hedef bağlamı ve işlem sonucu ayrı digest/kanıtlarla tutulmalı; belirsiz paket tekrar edilmemelidir.
- Gereken kanıt: Exact vendor sözleşmesi, güncel yetki/kimlik, ham TX/RX, fsync işlem kaydı ve bağımsız readback.
- Yanlış genelleme sınırı: Bu D8/SS2/WPLSoft hash sözleşmesi başka PLC ailelerine, firmware'e, protected/initial-value projelerine veya flash power-cycle kabulüne doğrulanmadan taşınmaz.
