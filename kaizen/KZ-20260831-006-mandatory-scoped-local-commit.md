# KZ-20260831-006 — Zorunlu Scope-İçi Yerel Commit

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-31
- Baseline/hash: `TZ=9f169c5; universal=d29e221`
- Supersedes: `KZ-20260820-001` içindeki opt-in yerel commit modeli
- Requirement: Tamamlanan mutasyonlar izlenebilir olmalı; ajan yerel commit için tekrar kullanıcı izni istememeli.

## Gözlem

- Beklenen davranış: Başarıyla tamamlanan her mutasyon exact scope içindeki atomik yerel commit ile kapanır.
- Gerçek davranış: Önceki şema `commit_authorized: false` varsayımıyla ajanı izin istemeye veya değişikliği commitsiz bırakmaya yöneltiyordu.
- Kanıt seviyesi: Kanonik politika, JSON şeması, guard ve prompt senaryosu incelemesi.

## Kök neden hipotezi

- Yerel ve geri alınabilir commit, push/remote/history yetkisiyle aynı alanda modellenmişti.
- “Yetki yoksa commit yok” güvenlik varsayımı iş takibini zayıflatıyordu.
- 10. Adam karşı görüşü: Zorunlu commit kirli ortak worktree'de başkasının değişikliğini kapsayabilir; exact stage allowlist'i, staged diff okuması ve global lease bu nedenle korunmalıdır.

## En küçük değişiklik

- Envelope şemasını v2 yapıp `commit_authorized` yerine sabit `commit_policy: SCOPED_MANDATORY` alanı kullan.
- Guard'ın commit izin bayrağını kaldır; boş index, protected yol ve scope dışı dirty/staged yol denetimlerini koru.
- Salt-okunur ve başarısız görevlerin commit üretmediğini; push/remote/history rewrite'ın ayrı yetki olduğunu kanonik metinde açıkla.
- S16, S17, S23 ve yeni S31 senaryolarıyla izin istememe ve commitsiz bırakmama davranışını sabitle.

## Doğrulama

- Repo yapı/link/prompt denetimi: `PASS — REPO_CHECK_OK`
- Birim testleri: `PASS — 26/26`
- Bootstrap verify: `PASS — iki implementation commit'inde BOOTSTRAP_VERIFY_OK`

## Rollback

- Bu değişikliğin atomik commit'i revert edilir.
- Envelope v1 ve `commit_authorized` alanı geri gelir; v2 runtime artifact'leri yeniden oluşturulmadan kullanılamaz.

## Sonuç ve sınır

- Sonuç: Yerel commit tamamlanmış mutasyonun zorunlu kapanış kapısıdır; ayrı izin sorusu kaldırılmıştır.
- Yanlış genelleme sınırı: Bu politika scope genişletmez, başarısız işi commit etmeyi zorunlu kılmaz ve push, remote değişikliği, amend/rebase veya history rewrite yetkisi vermez.
