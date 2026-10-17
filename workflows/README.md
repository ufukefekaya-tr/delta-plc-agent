# Workflows

[Public launch workflow](public-launch.json): identity/license → audit → product story/demo → clean snapshot → validation → final tenth-man → GitHub release. [Launch goal](../goals/public-launch.json).

[DVP nihai iş teslimi](dvp-final.json), mevcut W1–W7 üzerine ajan orkestrasyon tarifidir: ortam hazırlığı → WPLSoft üretim/derleme → istenirse GUI yükleme → MCP-only watch → kapanış. GUI aşamaları `workflow.run` operasyonu değildir; ayrı vendor kanıtı taşır. Yeni derleyici/aktarım MCP fonksiyonu eklenmez. [Uygulama rehberi](../docs/DVP_AGENT_RUNBOOK.md).

Kanonik yürütülebilir tanımlar [workflows.json](../src/universal_plc/data/workflows.json) içindedir. Başlatma bir run kimliği ve context digest üretir; yürütme açık, typed adım listesini bir kez uygular.

| Kimlik | Amaç | Kabul |
|---|---|---|
| W1 | Uyanış, model ve araç seçimi | Model/format/araç çelişkisi yok; eksik bilgi görünür |
| W2 | Yeni proje | Native create, çağrılan POU, task, derleme ve reopen ayrı kanıt |
| W3 | Mevcut projeyi düzenleme | Kaynak hash'i korunur, değişmeyen alanlar karşılaştırılır |
| W4 | Format/araç geçişi | Adres, sembol, encoding, task ve kayıp raporu |
| W5 | Yükleme ve gözlem | Geçerli artifact/hedef/oturum yetkisi ve gerçek durum kanıtı |
| W6 | Hata ve devam | Bilinmeyen sonuç uzlaştırılmadan tekrar yan etki yok |
| W7 | Öğrenme ve kapanış | Test, kanıt, genel kaizen, yerel commit |

`steps_returned` yalnız istenen araç adımlarının döndüğünü gösterir. `waiting_backend`, `failed`, `unknown_outcome` veya `planned` kabul değildir. Native/hardware acceptance kalan hedeflerden ayrıca izlenir. Oturum yetkisi aynı revizyonun düzeltme döngüsünü kapsar; eski örnek izinler canlı yetki oluşturmaz.

## DVP seri FAST akışı

[DVP seri hedefleri](../goals/DVP_SERIAL_FAST.md): W1 `serial.ports`; W5 `target.connect` → `target.status` ile iki vendor durum okuması (tarihsel kayıt sıcaklık da içeriyordu); W6 `target.disconnect`; W7 `deployment.reconcile` ile uygulanmamış transferin kaydı. Transfer için W5 `project.upload` yedek guard'ı BLOCKED olunca yükleme adımı planlanmaz. Prepare yalnız sabit dosya SHA'sını kontrol eder. Hiçbir run yeniden oynatılmaz; yeni, yetkili salt okunur deneme yeni run alır.

[MCP istemcisi](../tools/dvp_serial_probe.py) bu işlemleri mevcut workflow motoruyla yapar ve işlem kanıtlarını [COM6 kaydında](../docs/public/VALIDATION.md) saklar. Yazılım fixture'ı PASS sonucu fiziksel online başarı değildir.

Kullanıcının ayrı RUN isteğinde [kontrol istemcisi](../tools/dvp_run_cycle.py), W5 `target.run` → iki `target.status` → ayrı cleanup W5 `target.stop` → taze `target.status` → W6 disconnect uygular. ACK sonrası M1000 doğrulaması gerekir; RUN tekrar edilmez. [Canlı kontrol kanıtı](../docs/public/VALIDATION.md). Sonraki G-DVP-5, program aktarımını ayrıca geliştirme hedefidir.

## DVP SS2 program aktarımı

[Önceki, engelli G-DVP-5](../goals/dvp-transfer.json) ve [native sözleşme](../docs/adapters/dvp-transfer.md) ayrı enrollment ister. [MCP istemcisi](../tools/dvp_transfer_e2e.py) W1 `target.identify`, W5 iki durum okuması → `project.upload` → `deployment.prepare` → `deployment.execute` → son durum okuması kullanır. Her bağımlı aşama önceki kanıtı doğrular; güncel onay manifesti ve orijinal dosyanın SHA'sı her aşamada yeniden kontrol edilir. RUN gözlenirse otomatik STOP/yükleme yapılmaz.

Kesilmiş yazma W6 `deployment.reconcile` ile bağımsız tam program okumasına gider; yeni execute eski belirsiz işlemi tekrar oynatmaz. Başarı için ACK yanında aktif program, değişmemiş kuyruk, descriptor, parametre ve STOP karşılaştırmaları gerekir. Host `program_transfer_enabled=false` profili önceki `PROGRAM_TRANSFER_UNAVAILABLE` guard davranışını korur. Statik protokol/test başarısı canlı özel ACK kalifikasyonu veya fiziksel kabul yerine geçmez.

## Genel DVP proje profili

W1 machine.profile port açmadan komut/gözlem sözleşmesini gösterir. W5 machine.command yalnız profil anahtarını bounded request/release ve logical ack ile uygular; machine.parameter.write profil anahtarını tek FC06 ve bağımsız geri okumayla günceller; machine.read/status/observe açık profil verilerini okur. W6 machine.reconcile kayıtlı M request/edge bitlerini veya D candidate/baseline değerini salt okuyarak uzlaştırır; replay yapmaz. Proje adı ve adresler workflow motoruna girmez. [Genel profil sözleşmesi ve örnekler](../docs/adapters/dvp-controls.md).
