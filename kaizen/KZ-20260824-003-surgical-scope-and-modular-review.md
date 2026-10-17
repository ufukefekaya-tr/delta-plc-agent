# KZ-20260824-003 — Cerrahi Kapsam ve Modüler Karşı İnceleme

- Durum: `STATIC_CHECKED`
- Tarih: 2026-08-24
- Baseline/hash: `f0ef52532e7545fec4943a114cb3dcb97140bd66`
- Requirement: Geçmiş ajan çalışma kültüründeki cerrahi değişiklik, seçici yükleme, bağımsız karşı inceleme ve okuma dürüstlüğünü vendor/kişi ayrıntısı taşımadan güçlendirmek.

## Gözlem

- Beklenen davranış: Ajan exact görev kapsamıyla başlar, en küçük diff'i önceden tarif eder, modül bağımlılığını görünür tutar ve riskli karşı incelemeyi implementasyondan bağımsız yürütür.
- Gerçek davranış: Cerrahi yaklaşım, bootstrap ve 10. Adam vardı; fakat görev zarfı/expected-diff şablonu yoktu, bağımlılık döngüsü açık invariant değildi ve rol kartı iki geçişli kör/karşılaştırmalı incelemeyi taşımıyordu.
- Kanıt seviyesi: Güncel kaynak incelemesi, prompt inşa geçmişinin anonimleştirilmiş çok ajanlı karşılaştırması, prompt senaryoları ve bağımsız diff review.

## Kök neden hipotezi

- Tek sorumluluk dosya/POU büyüklüğüyle tanımlanmış, bağımlılık yönü ve mutable-state cycle açık yazılmamıştı.
- “Cerrahi değişiklik” kültür kartında vardı; fakat expected diff, dokunulmayacak invariant ve abort koşulları görev artifact'ına bağlanmamıştı.
- 10. Adam karşı görüşü: Yeni şablonlar her küçük işte zorunlu tutulursa yalınlık bürokrasiye döner; rol kartı iki geçişi taşımıyorsa seçici yükleme ana çözümün dilini tekrarlar.
- Kör artifact: [TM-20260824-003 — BLIND](reviews/TM-20260824-003-BLIND.md)
- Karşılaştırmalı artifact: [TM-20260824-003 — COMPARATIVE](reviews/TM-20260824-003-COMPARATIVE.md)
- 10. Adam verdict: `KORU`

## En küçük değişiklik

- `TASK_ENVELOPE.md` ile exact read/write/forbidden scope, owner, insan sınırı ve commit yetkisi tek küçük artifact'ta toplandı.
- `SURGICAL_CHANGE.md` ile expected diff, invariant, dokunulmayacak alan, stop koşulu, test ve rollback görünür yapıldı.
- Control Contract'a bağımlılık yönü ve cycle kontrolü; ADR/Review'a iki geçişli 10. Adam izi eklendi.
- 10. Adam rol kartına kör ilk geçiş, ana çözüm sonrası karşılaştırmalı geçiş, maddi değişiklikte yenileme ve beşli verdict eklendi.
- Kültür aktarımına envanter/tam okuma ayrımı, kanonik çekirdek, compaction resume, framework/tüketici sınırı ve döngüsüz modül kartları eklendi.
- Davranışsız küçük yazım/yorum değişikliğinde ayrı boş Surgical Change yerine Task Envelope kısa alanının yeterli olduğu netleştirildi.

## Doğrulama

- Repo yapı/link/anchor denetimi: `PASS — REPO_CHECK_OK`
- Explicit unit-test discovery: `PASS — 9/9`
- Prompt senaryoları S19–S23: `PASS — sözleşme metninde mevcut`
- İki geçişli 10. Adam: `PASS — BLIND BÖL önerisi uygulandı; COMPARATIVE KORU`
- İnsan IDE/bench/FAT: `UYGULANAMAZ — bu değişiklik yalnız prompt/repo kontrol düzlemidir`
- Atlanan doğrulama: Ajanın promptu gerçekten kavradığını kanıtlayan çalıştırılabilir model eval'i V1 kapsamında değildir.

## Rollback

- Bu KZ ile ilişkili davranış/şablon commit'i revert edilir.
- Yeni iki şablon, rol kartı farkı, dependency alanları ve kültür kartları birlikte geri alınır.
- Checker/test enforcement ayrı KZ ve commit olduğu için bu rollback'e karıştırılmaz.

## Sonuç ve sınır

- Sonuç: Başarılı; cerrahi değişiklik ve iki geçişli karşı inceleme sözleşmesi bağımsız `KORU` verdict'i aldı.
- Universal kural: Prompt kültürü metaforla değil, scope, owner, invariant, kanıt, stop koşulu ve rollback sözleşmesiyle taşınır.
- Yanlış genelleme sınırı: Her küçük davranışsız düzeltme ayrı şablon veya 10. Adam kuruluna ihtiyaç duymaz; risk ve maddi davranış değişikliği yoğunluğu belirler.
