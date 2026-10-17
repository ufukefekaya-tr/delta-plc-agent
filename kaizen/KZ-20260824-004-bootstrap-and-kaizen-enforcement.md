# KZ-20260824-004 — Bootstrap ve Kaizen Sözleşmesi Denetimi

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-24
- Baseline/hash: `5f384f1625207cd42e1acc819ae7d1a0b8b94197`
- Requirement: Bootstrap öz-beyanını Git control-plane eşitliğiyle desteklemek, doğrudan `python -m unittest` keşfini çalışır kılmak ve Kaizen alanlarının yalnız anahtar kelime yığınıyla geçmesini önlemek.

## Gözlem

- Beklenen davranış: Ajan control plane'in tracked, committed ve `HEAD` ile aynı olduğunu makinece kontrol edebilmeli; iki belgelenmiş unittest komutu aynı testleri bulmalı; Kaizen kaydı gerçek başlık ve dolu alanlar taşımalı.
- Gerçek davranış: Bootstrap yalnız metinsel sözleşme ve ACK idi; `python -m unittest` sıfır test buluyordu; eski Kaizen kontrolü alan adlarını düz metinde görmeyi yeterli sayabiliyordu.
- Kanıt seviyesi: Güncel kaynak incelemesi, olumsuz birim testleri ve bağımsız iki geçişli diff review.

## Kök neden hipotezi

- Checker, control plane için Git index/HEAD durumunu okumuyor; Kaizen sözleşmesini yapı yerine token varlığıyla sınamaya çalışıyordu.
- `tests` paketi discovery giriş dosyasını taşımadığı için kısa unittest komutu sessizce boş çalışıyordu.
- 10. Adam karşı görüşü: Git byte-eşitliği ajanın okuduğunu veya anladığını kanıtlamaz; checkout filtreleri ya da kasıtlı satır-sonu dönüşümü insan yorumuna ihtiyaç duyabilir. Kontrol sonucu yalnız bütünlük ön koşuludur.
- İlgili BLIND inceleme: [TM-20260824-003 — BLIND](reviews/TM-20260824-003-BLIND.md)
- İlgili COMPARATIVE inceleme: [TM-20260824-003 — COMPARATIVE](reviews/TM-20260824-003-COMPARATIVE.md)

## En küçük değişiklik

- `--verify-bootstrap`, üç control-plane dosyasının Git root, tracked, `HEAD` blobu, staged/unstaged durum ve raw çalışma blobunu doğrular.
- Kaizen denetimi dosya adı, zorunlu `##` başlıkları ve dolu satır alanlarını ayrı ayrı kontrol eder.
- `tests/__init__.py`, kısa unittest komutunun testleri keşfetmesini sağlar.
- Negatif token-yığını, eksik/yetersiz kayıt, geçerli asgari kayıt ve control-plane drift testleri eklenir.
- Checker kapsamının anlamsal kavrayış veya PLC/IDE/FAT kanıtı olmadığı README ve INDEX'te açık tutulur.

## Doğrulama

- Staged commit ağacı repo kontrolü: `PASS — REPO_CHECK_OK`
- Staged commit ağacı `python -m unittest`: `PASS — 14/14`
- Staged commit ağacı explicit discovery: `PASS — 14/14`
- Token-yığını negatif testi: `PASS — olumsuz fixture reddedildi`
- Staged commit nesnesinde `--verify-bootstrap`: `PASS — HEAD eşitliği doğrulandı; gerçek commit sonrası yeniden koşturulacak`
- İnsan IDE/bench/FAT: `UYGULANAMAZ — yalnız repo kontrol düzlemi`

## Rollback

- Bu KZ ile ilişkili checker, test-discovery, README/INDEX ve KZ kayıt commit'i birlikte revert edilir.
- KZ-20260824-003 ile gelen cerrahi kapsam, rol kartı ve iki geçişli inceleme belgeleri korunur.
- Rollback sonrasında bootstrap yeniden yalnız öz-beyan düzeyine iner; bu sınırlama görünür biçimde kaydedilir.

## Sonuç ve sınır

- Sonuç: `Başarılı — staged commit ağacı statik kontrolleri geçti; gerçek HEAD kontrolü handoff öncesi tekrarlanacaktır.`
- Universal kural: Belge tabanlı bootstrap, mümkün olan yerde committed kaynak eşitliği ve başarısızlığı sınayan testlerle desteklenmelidir.
- Yanlış genelleme sınırı: Hash/Git eşitliği okuma, kavrama, görev yetkisi veya doğru mühendislik kararı kanıtı değildir; runtime enforcement yerine geçmez.
