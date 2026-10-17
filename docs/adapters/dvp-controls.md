# Universal DVP — profile bağlı komut ve gözlem

17 Ekim 2026 / 0.3.1 güncellemesi: M/D strict yetkisinde `user_present=true` korunur; `heaters_isolated=true` veya birlikte `attended_machine_test_authorized=true` ve `independent_power_cutoff_available=true` gerekir. İkinci yol yalnız gözetimli M/D komutlarına aittir; RUN/STOP ve program aktarımı izinlerini açmaz. Bütün alanlar gerçek JSON boolean olmalıdır. Yeni paket kabulü ile tarihsel saha kopyası ayrıdır: [güncel teslim](../public/VALIDATION.md). [Ajan workflow rehberi](../DVP_AGENT_RUNBOOK.md).

Universal MCP'nin DVP adaptörü genel bir yürütücü kullanır. PLC programı değişmez. Çekirdekte makine adresi, kanal sayısı, sıcaklık birimi veya tuning algoritması bulunmaz; bunlar kullanıcı projesinin digest ile sabitlenmiş profilindedir. Yeni projede aynı çekirdek ve API korunur, profil değiştirilir. Desteklenen vendor taşıması mevcut SS2 ASCII/CDC sözleşmesidir; başka CPU aileleri ayrıca kalifiye edilir.

## Genel API

| İşlem | Argüman | Davranış |
|---|---|---|
| `machine.profile` | `{}` | Seçilmiş profilin komutlarını ve gözlem sözleşmesini gösterir; port açmaz |
| `machine.command` | `command`, tekil `command_id`, gerekirse sayısal `selector` | Profilin tanımladığı request → release → mantıksal durum okumaları |
| `machine.parameter.write` | `parameter`, tam sayı `value`, tekil `command_id` | Profilde izinli normal D registerını sınır/kilitlerle bir kez yazar ve bağımsız geri okur |
| `machine.read` | `device`, `count` | Profilin bütünüyle izin verdiği sınırlı M/D/Y aralığını okur |
| `machine.status` | `{}` | Profildeki alanların bir durum örneğini döndürür |
| `machine.observe` | `{}` | Zaman damgalı tek durum örneği; uzun gözlem istemcide yürür |
| `machine.reconcile` | `journal_path` | Belirsiz komutu salt okumayla inceler; komutu yeniden göndermez |

Komut argümanında adres, byte, değer, süre veya keyfi profil gönderilmez. Komut isimleri çekirdekte sabit değildir. `temperature.channel.start` yalnız [nötr yazılım regresyon fixture’ında](../../tests/dvpserial/fixtures/thermal-regression.json) bir anahtardır; farklı projede `pump.enable` veya `valve.close` aynı yürütücüyü kullanabilir. Ayrı ısıtma ve kontaktör grup komutları, profilin ayrı listelerini uygular; birleşik bir global bitin anlamını tahmin etmez.

`machine_catalog_path` ve `machine_catalog_sha256` host profilinde seçilir. Proje kataloğu; `contract_id`, `revision`, kaynak/payload referans SHA'ları, komutlar, exact `read_devices` ve genel `observations` tanımlarını içerir. `selector` katalogdaki listeyi seçer; `is_start` açık bir profil özelliğidir. Her gözlem `device`, `count`, `scale`, `signed`, `unit` ve isteğe bağlı `enum` tanımlar. Bağlı neden alanının bastırılması da profil verisidir. Etiketler yalnız live revizyon doğrulanmışsa yorumlanır; ham değerler korunur. Çok paketli snapshot atomik değildir.

## Oturum ve kanıt

Port/USB/istasyon/süre profili mevcut [DVP seri sözleşmesini](dvp-serial.md) kullanır. Ek host alanları: `machine_source_path/_sha256`, `machine_state_dir`, `port_lease_path`, `port_lease_owner`, `port_lease_token`, `live_revision_evidence_path/_sha256`, `machine_control_enabled`, `allow_machine_commands`, `allow_machine_parameters`, `pulse_ms`. Komut profili güncel yerinde görevli ve izolasyon koşullarını taşır. `pulse_ms` 50–500 ms ile sınırlıdır. PLC RUN/STOP'u değişmez; ON talebinde taze RUN/nonboot gerekir.

Live proof, kaynak/contract/revizyon, payload, target identity, USB/route/istasyon ve Service workspace context ile bağlanır. İnsan tarafından doğrulanmış revizyon bilgisi bağımsız program geri okuması veya vendor derlemesi olarak gösterilmez. Dosya adı ve mtime tek başına live revizyon değildir. Profil, kaynak, proof veya port sahipliği değişirse yeni paket durur. `machine.profile` çevrimdışı sözleşme doğrulamasıdır; fiziksel kabul sağlamaz.

Cooperative port lease `schema_version:1`, `status:HELD`, `owner`, `token`, `port`, aware `expires_at` alanlarını içerir. İnsan/PLC ajanının portu kendiliğinden alınmaz. Süreli lease yanında hedefte process lock vardır; iki istemci aynı token'la da paketleri iç içe gönderemez. COM6 şu teslim sırasında insan/PLC ajanındadır; yeni komutlar canlıda çalıştırılmamıştır.

## Darbe ve hata sınırı

Durable intent, olası request ve release yazısından önce kaydedilir. Sabit FC05 yalnız katalogdaki M komut bitine gider; Y, CR28, force veya program belleği yazıcısı bulunmaz. D parametreleri yalnız ayrı profil tanımı ve izinle yazılır. ACK echo fiziksel etki kanıtı değildir. Release sonrası komut ve kaynakta belirtilen ON edge hafızası low, ardından request/latch durumları okunur. PLC korumaları ve algoritması tek sahip olarak kalır.

Release doğrulanamazsa sonuç `unknown_outcome` olur. Hedefteki kalıcı belirsizlik engeli yeni komut kimliği, yeni workspace, process restart veya lease expiry ile aşılamaz. Reconcile yalnız kayıtlı request/edge bitlerini ve durumları okur; yüksek bit veya uyumsuz kanıtta engel kalır. Batch yarıda kesilirse tamamlanan, belirsiz ve denenmemiş üyeler ayrı kaydedilir; kalanlara otomatik devam veya karşı komut gönderilmez.

Y okuması yalnız PLC çıkış bitidir, elektriksel feedback değildir. Y numaraları oktaldir; FC01 PDU adresi `0500 + octal(Y)` olur. Adres sınırı mevcut vendor taşıma sözleşmesidir; başka CPU adres tablosu ayrıca doğrulanır.

## Çağrı örneği ve gözlem

Mevcut `adapter.invoke` veya W5 kullanılır. Service context kullanıcı argümanıyla taklit edilemez. Yazılım fixture’ında örnek backend argümanları:

```json
{"command":"temperature.channel.start","selector":1,"command_id":"operator-new-request-001"}
```

Profil seçilmeden veya güncel live enrollment oluşmadan bu örnek çalıştırılmaz. Aynı `command_id` tekrar kullanılmaz. Okuma istemcisi [dvp_controls_observe.py](../../tools/dvp_controls_observe.py) yalnız MCP kullanır; varsayılanı port açmayan `machine.profile` işlemidir:

```powershell
python -B tools/dvp_controls_observe.py --config <host-profile.json> --runtime-dir <runtime> --evidence-path <fresh-profile-evidence.json>
python -B tools/dvp_controls_observe.py --config <host-profile.json> --runtime-dir <runtime> --evidence-path <fresh-observe-evidence.json> --observe --duration-seconds 90
```

Gözlem evidence dosyası MCP başlamadan exclusive ayrılır. Her adım yeni workflow run alır; source/profile SHA her turda kontrol edilir. Isıtma artışı veya tuning sonucu bu istemcinin salt okuma kaydından otomatik fiziksel kabul sayılmaz. Güncel test ve canlı sınırlar [teslim kaydındadır](../public/VALIDATION.md).

## D parametre düzenleme

Katalog `parameters` bölümünde yalnız açık normal D registerı, signed/unsigned 16 bit tipi, min/max, birim ve write_guards/relations tanımlanır. Genel register/adres/değer yazıcısı açılmaz; çağrı yalnız profil anahtarını seçer. `allow_machine_parameters` M kumandasından ayrı host yetkisidir. Örnek: `{"parameter":"pid_sample_s","value":5,"command_id":"new-setting-001"}`. Değer ham register biriminde tam sayıdır; sessiz ölçek/yuvarlama yoktur.

Mevcut kalifiye vendor sınırı normal D0–999 veya D2000–4095'tir; D1000–1999 sistem/iletişim/descriptor bölgesi yazılmaz. Parametre profili, ayar kilitlerini kaynak PLC/HMI sözleşmesine göre tanımlar. Finite relations yalnız eq/ne/lt/lte/gt/gte ve sabit offset karşılaştırmasıdır; arbitrary eval/script çalışmaz. Yalnız parametre içeren profilde M komutu veya pulse süresi gerekmez.

Her update tek FC06 intent ve tek write kullanır; ACK echo'yu bağımsız FC03 exact value okuması takip eder. ACK kaybında ikinci write yoktur; current register değeri bağımsız kanıtlanırsa ack_verified=false/readback_verified=true kaydedilir. Geri okuma kanıtlanamazsa aynı target fence bütün yeni M/D işlemlerini engeller. Parameter reconcile yalnız candidate/baseline/other sınıflar; güncel değer bilinmiyorsa ise fence kalır. D ayarı PLC STOP'ta da yapılabilir; M edge komutunun freshRUN koşulunu taşımak veya RUN açmak gerekmez. Register geri okuması, PLC izinleri/ısıtma/tuning uygulama kabulü değildir.

Protokol: [Modbus specification6.6 FC06](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf); SS2'yi kapsayan [Delta manual4.5.5](https://filecenter.deltaww.com/Products/download/06/060302/Manual/DELTA_IA-PLC_DVP-ES2-EX2-EC5-SS2-SA2-SX2-SE-SE2-TP_PM_EN_20241031.pdf). Dokümante request/ACK sözleşmesi mevcut firmware/canlı yazma sonucu yerine geçmez; canlı D işlemi bu teslimde NOT_RUN.
