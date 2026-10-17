# KZ-20260820-001 — Ajanlara Kapsamlı Commit Yetkisi

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-20
- Baseline/hash: `037d610`
- Requirement: Yetkili ajanlar düzgün ve izlenebilir Git commitleri atabilmeli.

## Gözlem

- Beklenen davranış: Orkestratör, ayrık kapsam verdiği ajana commit yetkisi verebilir.
- Gerçek davranış: Kanonik prompt ve repo talimatı bütün commitleri yalnız orkestratöre ayırıyordu.
- Kanıt seviyesi: Kullanıcı kararı ve metin kaynak incelemesi.

## Kök neden hipotezi

- İlk repo planında entegrasyon sahipliği ile commit yazma yetkisi aynı kavram sayıldı.
- Bu kısıt çakışmayı azaltırken ayrık ve doğrulanmış ajan teslimlerinin atomik tarih üretmesini gereksiz yere engelledi.
- 10. Adam karşı görüşü: Yalnız seri commit planlamak yeterli değildir. Staging–test–commit penceresinde başka ajan index'i veya çalışma ağacını değiştirirse commit kapsamı ve test kanıtı kirlenir; rollback başka işi de geri alabilir.

## En küçük değişiklik

- Commit yetkisini görev kartında açık bir alan yap.
- Yetkili ajanı kendi yolları, staged diff, ilgili test ve atomik commit ile sınırla.
- Ayrık branch/worktree'yi tercih et. Paylaşılan worktree zorunluysa baseline'dan post-commit doğrulamaya kadar tek sahipli global commit lease kullan; başka stage/commit ve test ağacı mutasyonlarını dondur.
- Lease başında beklenen parent, boş index ve exact allowlist'i; sonunda parent, değişen yollar, temiz index ve test edilen commit ağacını doğrula.
- Final entegrasyon, çatışma çözümü ve release geçmişi doğrulamasını orkestratörde bırak.
- Açık kullanıcı yetkisi olmadan push, remote değişikliği ve history rewrite yapma.

## Doğrulama

- Repo yapı/link/anchor denetimi: `PASS`
- Birim testleri: `PASS`
- Prompt senaryosu S16: `PASS — sözleşme metninde mevcut`
- Staged kapsam incelemesi: `PASS`

## Rollback

- Bu commit revert edilir.
- Önceki tek-orkestratör commit kuralı geri gelir.
- Kabul ölçütü: Kanonik prompt, workflow ve repo talimatı yeniden aynı sınıra bağlanır.

## Sonuç ve sınır

- Sonuç: Başarılı; 10. Adam bulgusuyla commit lease şartı eklendi.
- Universal ders: Entegrasyon sahipliği ile atomik commit üretme yetkisi ayrı sözleşmelerdir.
- Yanlış genelleme sınırı: Açık görev kapsamı, temiz staging ve test kanıtı olmayan ajan commit atmamalıdır.
