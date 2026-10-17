# Task Envelope

Bu zarf, bir ajanın neyi okuyup değiştirebileceğini ve hangi yetkiyle çalıştığını sabitler. `BOOTSTRAP_ACK` bu zarfın yerine geçmez.

Uzun veya mutasyonlu görevde bu insan görünümüyle birlikte [TASK_ENVELOPE.json](TASK_ENVELOPE.json) kullanılır. JSON dosyasının raw-byte SHA-256 değeri checkpoint'e yazılır; scope listeleri repo-relative exact yol veya `/` ile biten dizin prefix'i taşır.

## Kimlik

- Task ID:
- Baseline commit/hash:
- Policy epoch:
- Policy epoch algoritması: `sha256-path-nul-bytes-nul-v1`
- Mod: `NORMAL | FAST`
- Rol:
- Owner/orkestratör:

## Exact scope

- Okunacak yollar/kanıt:
- Yazılabilecek yollar:
- Dokunulmayacak yollar:
- Önceden dirty yollar:
- Sahip olunan output/state/sinyal alanı:
- Yasak fiziksel/harici işlemler:

## Yetki

- Yetki kaynağı:
- Dosya değiştirme: `VAR | YOK`
- Yerel commit: `SCOPED_MANDATORY` — başarılı mutasyon görevi ayrıca izin sorulmadan atomik commit ile kapanır
- Push/remote/history rewrite: `VAR | YOK`
- Worktree/branch veya global commit lease:

## Teslim

- Tek beklenen artifact:
- Kabul ölçütleri ve testler:
- İnsan kanıtı gerekenler:
- Stop/escalation koşulları:
- Handoff alıcısı:

## ACK

- `BOOTSTRAP_ACK`:
- Edit/commit öncesi epoch ve scope yeniden doğrulandı mı:
- Compaction/handoff sonrası `RESUME_ACK`:
