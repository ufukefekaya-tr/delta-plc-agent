# KZ-20260820-002 — Bootstrap ve Compaction Yeniden Yükleme

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-20
- Baseline/hash: `1e5446e`
- Requirement: Ajan çekirdek kuralları okumadan başlamamalı ve compaction sonrası görev state'ini hafızadan uydurmamalı.

## Gözlem

- Beklenen davranış: Çekirdek politika her görevde yüklenir; görev durumu özet hafızadan değil kalıcı kayıttan geri kurulur.
- Gerçek davranış: `AGENTS.md` okuma emri veriyordu fakat tam okumanın ACK'i, prompt kimliği ve compaction sonrası resume sözleşmesi yoktu.
- Kanıt seviyesi: Kullanıcı soruları ve metin kaynak incelemesi.

## Kök neden hipotezi

- Navigasyon belgesi ile gerçekten yüklenmiş politika birbirine karıştırıldı.
- Sohbet bağlamının sürekliliği görev state'i için tek otorite kabul edildi.
- Bütün belgeleri her görevde okutmak ile yalnız `AGENTS.md` okumak arasında risk tabanlı yükleme katmanı kurulmadı.
- 10. Adam karşı görüşü: ACK yalnız öz-beyandır; stale/uncommitted control plane, checkpoint multiwriter yarışı ve fark edilmeyen compaction hâlâ yanlış güven üretebilir.

## En küçük değişiklik

- Temiz/committed üç dosyalık control plane için raw-byte birleşik `POLICY_EPOCH` hesapla; control-plane değişikliği aktif session kayıtlarını geçersiz kılsın.
- `BOOTSTRAP_ACK` ile epoch ve immutable görev zarfını eylemden önce öz-beyan et; ACK'i kanıt veya yetki sayma, commit varsayılanını `yok` tut.
- Bütün dokümanları değil, rol ve görevle ilgili olanları `INDEX.md` üzerinden seç.
- Orkestratörün yazdığı küçük, sıralı ve task-local `SESSION_CHECKPOINT.md` ile Intent, baseline, sahiplik, kanıt, açık kapı ve sıradaki tek adımı kalıcı tut; alt ajanlar yalnız handoff verir.
- Compaction/handoff/yeni ajanda yeniden okuma, kimlik doğrulama ve `RESUME_ACK` zorunlu olsun.
- Fast modu bootstrap/resume kapısını atlamasın.
- İlk edit/commit öncesi ucuz epoch/scope kontrolü yap; uyuşmazlıkta re-bootstrap uygula.
- Belge protokolünün runtime enforcement olmadığını ve fark edilmeyen compaction'ı garantiyle yakalayamayacağını açıkla.

## Doğrulama

- Sistem promptu 250 satır sınırı: `PASS — 240 satır`
- Repo yapı/link/anchor denetimi: `PASS`
- Birim testleri: `PASS — 9/9`
- Prompt senaryoları S17–S18: `PASS — sözleşme metninde mevcut`
- Policy epoch değişim testi: `PASS`
- 10. Adam incelemesi: `PASS — self-attestation, epoch ve tek checkpoint writer revizyonları işlendi`

## Rollback

- Bu commit revert edilir.
- Session checkpoint şablonu ve bootstrap checker kaldırılır.
- Önceki belge yönlendirmesi geri gelir; compaction riski yeniden açık olarak kaydedilir.

## Sonuç ve sınır

- Sonuç: Başarılı; `POLICY_EPOCH=1ad78c6489f713453d78bf93f6fff77e740bce6a359d772ac3a9505d25157b40` için bootstrap/resume sözleşmesi kuruldu.
- Universal ders: Kanonik politika, görev state'i ve sohbet hafızası ayrı katmanlardır.
- Yanlış genelleme sınırı: ACK metni ajanın gerçekten okuduğuna dair kriptografik kanıt değildir; runtime veya orkestratör denetimi gerekir.
