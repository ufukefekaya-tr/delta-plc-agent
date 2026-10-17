# KZ-20261013-003 — Genel DVP profil komutları

- Durum: `STATIC_CHECKED`
- Tarih: 2026-10-13
- Baseline/hash: `d6e09f510cc532bc068526e8c4ec34173a145ce0`; engine `d6c4dfd94906392d4b3f986edee613de3af6c09b1432a18dfc636baffb607471`
- Requirement/ADR: Kullanıcının Universal DVP MCP düzeltmesi, exact profil komutları, Computer Use yok ve PLC ajanı port önceliği.

## Gözlem

Kullanıcı Universal MCP istedi; proje özel sıcaklık komutlarının core catalog'a eklenmesi doğru sınır değildi. Ayrıca kayıp release ACK ve yeni command_id eski yüksek bitin güvenli olduğunu kanıtlamaz.

## Beklenen / gerçek

Tek genel motor; bütün proje adres/anlamları host-pinned profil. Request intent, tek release, logical ack ve hedef çapında uncertainty fence. Erken taslak temperature.* API'ye ve workspace path recovery'ye bağlıydı; bağımsız SDK/negatif testleri interface ve recovery path/boundary açıklarını buldu.

## Kök neden hipotezi

İşlev adı ile vendor transport yetkisinin birleştirilmesi, proje koşulunu universal core'a taşıyordu. Yerel command kimliği ve port token'ı tek başına cross-process/future-command admission sağlamıyordu.

## 10. Adam karşı görüşü

- 10. Adam karşı görüşü: Profil yalnız dosya adı olabilir; source/target/revision drift hâlâ yanlış PLC'ye komut verebilir. Request gönderilmeden guard fail olursa otomatik OFF başka operatörün bitini silebilir. Başarılı ACK veya eski status2 yeni tuning tamamlandı kanıtı değildir.

## En küçük değişiklik

Generic machine.command/parameter.write/read/status/observe/reconcile/profile API; explicit digestli proje kataloğu, Service context ve source/liveproof/lease gate. Mutating send'den önce fsynced intent; yalnız kendi olası request'i için bir release. Shared process lock +kalıcı target fence, read-only reconcile exact request/edge low. Legacy vendor status'ta sensor adresi/birimi kaldırıldı. İçeride yakalanan gate/IO hatası da sahip olunan COM portunu kapatır; kalıcı belirsizlik kaydı korunur.

## Doğrulama

147 DVP +25 Universal =172 test PASS; ikinci ilgisiz pump/valve profili aynı motordan geçti. [Test ve SDK kanıtları](../docs/public/VALIDATION.md). V10 dosyasına veya port/hardware'e yazılmadı. Live M/D kumanda/ısıtma/tuning NOT_RUN. Güncel source drift eski bağlamayı engelledi. D için tek FC06 + bağımsız FC03, profil bounds/guards/relations ve aynı kalıcı hedef engeli kullanılır.

## Rollback

Host machine_control_enabled/allow_machine_commands/allow_machine_parameters false tutulur, yeni profile enrollment yapılmaz. Belirsiz işlem fence'i rollback/restart sırasında silinmez. Kod rollback'i ayrı scoped commit ile yapılır; eski source/live kapsamı tekrar kullanılmaz.

## Sonuç ve genellenebilirlik sınırı

Universal çekirdek proje recipes taşımadan typed komutları yürütür; profile bağlı okumalar ölçüm birimini taşır. Mevcut vendor qualification SS2 ile sınırlıdır. Pump/valve örneği yazılım yeniden kullanım kanıtıdır; ikinci gerçek makine/CPU kabulü değildir.

## Genellenebilir ders

- Universal kural: vendor taşıması ile proje komut/sinyal anlamı explicit profile sınırıyla ayrılır.
- Gereken kanıt: source/context/target/lease gate, no-replay ve ikinci farklı profile testi.
- Yanlış genelleme sınırı: test profili başka gerçek CPU veya makine desteği kanıtı değildir; SS2 hardware ve fiziksel kabul NOT_RUN.
