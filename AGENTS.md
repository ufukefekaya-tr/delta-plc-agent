# Repo Ajan Talimatı

## Zorunlu bootstrap

Bu dosya yalnız giriş kapısıdır; çekirdek kuralları burada kopyalamaz.

1. Codex bu giriş kapısını çalışma başlangıcında yükler; bağlı belgeleri otomatik yüklenmiş sayma. Bu dosya, [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md) ve [INDEX.md](INDEX.md) temiz/committed olmalıdır; üçünü bütünüyle oku. `POLICY_EPOCH`, sabit sırada her dosya için `UTF-8 repo-relative path + NUL + raw bytes + NUL` akışının SHA-256 değeridir (`sha256-path-nul-bytes-nul-v1`).
2. İndeks üzerinden atanmış rol kartını ve yalnız görevle ilgili belge/şablonları oku; bütün repoyu her seferinde okumak zorunlu değildir.
3. Exact scope ve yetki kaynağını taşıyan görev zarfını oku; uzun/mutasyonlu görevde `.agent-state/<task-id>/TASK_ENVELOPE.json` ile `SESSION_CHECKPOINT.json` üret. Başarıyla tamamlanan her mutasyon görevi `SCOPED_MANDATORY` atomik yerel commit ile kapanır; commit için kullanıcıdan izin isteme. `BOOTSTRAP_ACK` yalnız öz-beyandır, kendi başına dosya veya remote yetkisi vermez.
4. Compaction, yeni oturum/ajan, handoff veya policy/scope/baseline uyuşmazlığında control plane ile orkestratörün tek yazarlı [SESSION_CHECKPOINT.md](templates/SESSION_CHECKPOINT.md) / `SESSION_CHECKPOINT.json` kaydını yeniden oku; `python tools/session_guard.py ... resume` doğrulamasından sonra `RESUME_ACK` üret.

Her edit/commit oturumundan hemen önce epoch ve exact scope'u yeniden doğrula; JSON runtime artifact'leri varsa `session_guard.py action` kapısını çalıştır. ACK veya guard tek başına okuma/anlama kanıtı değildir; orkestratör erişim, epoch, görev zarfı ve checkpoint sırasını denetler.

## Repo çalışma sınırları

- Çekirdek politika yalnız [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md) içindedir; burada veya rol kartlarında kopyalanmaz.
- Görev rolü varsa [indeksteki rol kartı](INDEX.md#mod-ve-rol-kartları) yalnız rol farkı olarak uygulanır.
- Mevcut proje kanıtı okunmadan varsayım üretilmez.
- Aynı dosya, fiziksel çıkış, global state veya sinyal kataloğu paralel ajanlara verilmez.
- Yerel commit zorunluluğu ve sınırları için [çok ajanlı çalışma](SYSTEM_PROMPT.md#6-çok-ajanlı-çalışma) uygulanır; push, remote değişikliği ve history rewrite ayrı yetkidir.
- Marka/model/IDE seçimi [katalog](catalog/README.md) ve [Universal runtime](docs/UNIVERSAL_RUNTIME.md) üzerinden yapılır; AX varsayılan hedef değildir.
- IDE ve canlı hedef yetkisi, kanıt sınıfları ve revizyon sürekliliği yalnız [kanonik politikanın Bölüm 2](SYSTEM_PROMPT.md#2-yetki-çevrimdışı-ide-ve-canlı-hedef) hükümleriyle belirlenir. Eski makine oturumunun yetkisi taşınmaz.
