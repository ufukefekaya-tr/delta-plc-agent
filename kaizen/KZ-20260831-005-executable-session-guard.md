# KZ-20260831-005 — Yürütülebilir Session Guard

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-31
- Baseline/hash: `universal=e0d5344; TZ=e4219f6`
- Requirement: Uzun görev ve compaction sürekliliği yalnız prompt öz-beyanına bağlı kalmamalı.

## Gözlem

- Beklenen davranış: Aynı control plane, immutable scope ve güncel checkpoint yeni oturumda makinece doğrulanabilmeli.
- Gerçek davranış: Epoch ifadesi ile uygulama farklı yorumlanabiliyor; Markdown checkpoint'in varlığı güncellendiğini kanıtlamıyor; repo denetleyicisi ignored/vendor dosyaları tarayabiliyor.
- Kanıt seviyesi: Güncel kaynak incelemesi, gerçek checker ve birim testi çıktıları.

## Kök neden hipotezi

- Talimat, runtime state ve doğrulayıcı aynı veri modelini paylaşmıyordu.
- Hash algoritmasının domain separation ayrıntısı belgelenmemişti.
- Repo sözleşmesi taraması framework sınırı yerine fiziksel dizin ağacını esas alıyordu.
- 10. Adam karşı görüşü: Guard çağrılmazsa veya yanlış artifact verilirse yine koruma sağlamaz; modelin metni kavradığını ve compaction'ı fark ettiğini kanıtlamaz.

## En küçük değişiklik

- Epoch algoritmasını `sha256-path-nul-bytes-nul-v1` olarak adlandır ve exact byte akışını kanonik belgelerde sabitle.
- JSON Task Envelope ve Session Checkpoint şablonları ekle; envelope raw-byte hash'ini checkpoint'e bağla.
- `session_guard.py` ile stale epoch, baseline, HEAD, worktree, scope, protected path, commit stage sınırı ve push yetkisini fail-closed doğrula.
- Repo checker'ını Git-tracked framework kapsamıyla sınırla; ignored/vendor çalışma alanlarını dışarıda bırak.
- Resume, drift ve scope reddi için çalıştırılabilir birim testleri ve S24-S28 davranış senaryoları ekle.

## Doğrulama

- Repo yapı/link/prompt denetimi: `PASS — REPO_CHECK_OK`
- Birim testleri: `PASS — 26/26`
- Bootstrap verify: `PASS — TZ 9f169c5 / universal d29e221 üzerinde BOOTSTRAP_VERIFY_OK`
- Yeni oturum/gerçek otomatik compaction soak: `İNSAN/ORKETRATÖR KANITI GEREKİR`

## Rollback

- Bu değişikliğin tek niyetli diff/commit'i revert edilir.
- JSON runtime artifact'leri ve guard kaldırılır; önceki Markdown-only protokol geri gelir.
- Eski epoch/checkpoint'ler algoritma kimliği değiştiği için otomatik olarak yeniden kullanılmaz.

## Sonuç ve sınır

- Sonuç: Yürütülebilir guard ve sentetik resume testleri eklendi; gerçek uzun oturum kanıtı testlerden sonra ayrıca kaydedilecek.
- Yanlış genelleme sınırı: Bu guard OS erişim kontrolü, Codex runtime hook'u, gerçek compaction bildirimi veya PLC doğruluğu değildir; orkestratörün her mutasyon kapısında çağırması gerekir.
