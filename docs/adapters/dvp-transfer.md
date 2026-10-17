# DVP-SS2 native program aktarım sözleşmesi

**Tarihsel deneysel aktarım sözleşmesi.** Aşağıdaki V7/COM6 bilgileri 13 Ekim oturumuna aittir, güncel program/port veya yetki değildir. 17 Ekim tesliminde istenen program yüklemesi WPLSoft Computer Use üzerinden yürütülür; bu MCP protokolü varsayılan yol değildir. Her yeni işte en son onaylı manifest kullanılır. [Güncel iş teslim rehberi](../DVP_AGENT_RUNBOOK.md).

13 Ekim 2026 — NORMAL. Bu kılavuz, sabit WPLSoft ikilisinde görülen **program belleği** aktarımını tarif eder. FC03/FC10 taşıması, genel D register erişimiyle aynı yetki değildir. Statik sözleşme canlı kabul kanıtı sayılmaz. Belge hazırlanırken private ACK kalifikasyonu, yeni adayın yüklenmesi ve bağımsız geri doğrulaması **NOT_RUN** durumundadır; kapanış hükmü güncel ham işlem kaydından verilmelidir.

## Güncel kaynak, hedef ve port sahipliği

Her prepare/execute öncesinde kullanıcı veya yetkili üretim ajanından **en son onaylanmış, kaydedilmiş native dosyayı** doğrulayın. Her yeni iş kendi açık kaynak/revizyon kaydını seçer. Daha yeni revizyon bildirildiğinde önceki enrollment geçersiz olur. Kaynak dosya değiştirilmez; gerekiyorsa checksum düzeltmesi ayrı, digestli türev adaya uygulanır.

Kayıt; onay kaynağı, kaynak yolu/revizyonu, `source_sha256`, değişmez adayın `candidate_sha256` değeri ve çözülmüş programın `payload_sha256` değerini ayrı tutar. Tam `.dvp` dosya SHA'sı program byte SHA'sının yerine kullanılmaz. Native checksum doğrulaması ve PLC syntax kontrolü **IDE derlemesi** olarak raporlanmaz. [Native dosya katmanı](../../src/universal_plc/dvp_native.py) ile [aktarım katmanı](../../src/universal_plc/dvp_transfer.py) ayrı sorumluluk taşır.

Bu hostta yol PC → COM6 Delta HMI CDC → PLC RS485'tir. USB `05DD:0100` gateway'i tanımlar; CPU parça numarasını kanıtlamaz. COM6'yı kullanan diğer ajanla oturum sahibi ve bırakma zamanı uzlaştırılır; tek owner lease tutulur. Meşgul portu zorla devralmayın, diğer ajan/IDE sürecini kapatmayın. Lease devri, yeniden bağlantı veya araç hatasında gerçek hedef, son işlem ve checkpoint yeniden uzlaştırılır. Aynı portta iki transport veya okuma döngüsü çalışmaz. Computer Use/GUI ve doğrudan DLL çağrısı bu yolun parçası değildir.

Canlı yetki, [kanonik politika Bölüm 2](../../SYSTEM_PROMPT.md#2-yetki-çevrimdışı-ide-ve-canlı-hedef) uyarınca güncel hedef/revizyon, fiziksel koşullar, süreli typed operation ve kurtarma kaydına bağlıdır. Kullanıcı ayrılmışsa eski `user_present=true`/RUN yetkisi taşınmaz. İzolasyon veya yerinde durdurma koşulları güncel değilse program mutation yapılmaz. Salt okuma yetkisi program-modu paketini veya yüklemeyi kendiliğinden açmaz.

## MCP istemcisinin kullanımı

[İstemci](../../tools/dvp_transfer_e2e.py), bağımlılıkları kurulmuş ortamdan repo kökünde çalıştırılır. Aşağıdaki yollar örnektir; mevcut, süreli host profilini ve her çalıştırma için **yeni** evidence dosyasını kullanın. İstemci evidence dosyasını MCP/port açılmadan önce oluşturur ve her workflow çağrısından önce diske yazar.

```powershell
$dvpProfile = 'C:\PLC-session\profile.json'
$dvpRuntime = 'C:\PLC-session\runtime'
$dvpApproval = 'C:\PLC-session\latest-approved.json'
python -B tools/dvp_transfer_e2e.py --mode observe --config $dvpProfile --runtime-dir $dvpRuntime --evidence-path 'C:\PLC-session\observe.json'
python -B tools/dvp_transfer_e2e.py --mode qualify --config $dvpProfile --runtime-dir $dvpRuntime --approved-manifest $dvpApproval --evidence-path 'C:\PLC-session\qualify.json'
$dvpWorkspace = '<qualify raporundaki workspace.workspace_id>'
python -B tools/dvp_transfer_e2e.py --mode deploy --workspace-id $dvpWorkspace --config $dvpProfile --runtime-dir $dvpRuntime --approved-manifest $dvpApproval --evidence-path 'C:\PLC-session\deploy.json'
```

`observe` yalnız W1 kimlik ve W5 durum okumalarını yapar; program moduna girmez. `qualify`, onaylı güncel kaynakla bağlı çalışma alanı, yerinde görevli, güncel izolasyon ve zaten STOP olan hedef gerektirir. Typed private entry/exit probe gözlenen cevap şekli, hedef, context ve parser/protokol digest'lerini kanıt dosyasına bağlar. Sınırı **ACK_SHAPE_ONLY**'dır: exact echo cevap gateway/serial echo'dan ayırt edilemez; tek başına mode etkisini veya program aktarımını kanıtlamaz. Başarılı sonuçtan sonra istemci profilde yalnız `contract_path`, `contract_sha256` ve `verified_contract` alanlarını günceller; attendance/isolation veya süreyi kendiliğinden açmaz. Bu flag yalnız cevap şekli kapısını karşılar; sonraki tam yedek, FC10 ACK ve bağımsız program geri okuması zorunludur. Fiziksel kalifikasyon bu oturumda **NOT_RUN**'dır.

`deploy` için qualify raporundaki **aynı `--workspace-id` ve runtime kökünü** kullanın. Yeni çalışma alanı aynı byte'ları içerse bile context-bound proof ile eşleşmez. Onay manifesti ve **özgün source yolu/SHA'sı**, qualify/deploy workflow'larının her adımından önce tekrar doğrulanır. Host profili her transport kapısında diskten taze okunur; revocation, expiry veya açık bağlantının route/port/USB/station bağının değişmesi işlemi engeller. İstemci otomatik STOP/RUN göndermez; RUN gözlemi deploy'u durdurur.

Kesilen işlemin kurtarması özgün çalışma alanını ve journal'ın `workspace/files` altındaki göreli yolunu kullanır:

```powershell
python -B tools/dvp_transfer_e2e.py --mode reconcile --workspace-id $dvpWorkspace --journal-path '<journal_path>' --config $dvpProfile --runtime-dir $dvpRuntime --evidence-path 'C:\PLC-session\reconcile.json'
```

`deployment.reconcile` yeni program yazmaz veya replay yapmaz. Bilinen, plan içermeyen qualify/upload journal'ı için mode kapanışı, tam ham okuma ve taze STOP doğrulaması `observed_only` verir; bu sonuç `program_verified=false` / `readback_verified=false` olup yükleme kanıtı değildir. Planlı journal'da immutable aday ve yedek sınıflandırılır; `partial_or_unknown` halinde hedef lease korunur. Güncel recovery enrollment, aynı pinned manifest/context/hedef ve immutable aday/plan/yedek kapıları korunur. Yalnız reconcile, üreticinin dış source dosyası sonradan değişse de eski işlemin salt-okunur sınıflandırmasına izin verir; eski manifesti koruyun. Prepare/execute özgün source SHA'sını taze doğrulamaya devam eder. Başarısız/yarım execute için `program_downloaded=null` belirsizliktir; başarılı geri okuma daha sonra cleanup hatası olsa da ayrı kaydedilir.

## Vendor kanıtı ve byte düzeni

**GÖZLEM:** Bu hostta kaynak `C:\Program Files (x86)\Delta Industrial Automation\WPLSoft 2.52\WPLSoft.exe`; SHA-256 `f0c002d6dc7fab80ea942498eac9ceec19aaef037c4d3e7e418a7f64df5f42c1`. Klasör 2.52, dosya sürüm metadatası 2.50'dir. Adresler yalnız bu hash için PE virtual address'tir. Kurulumdaki `DeviceRange.ini`, `SS2_Series=5` ve P0–P7919 aralığını ayrıca gösterir; bu kaynaklar başka sürüme doğrulanmadan taşınmaz.

| Statik kanıt | VA / sonuç |
|---|---|
| CDC başlatma ve ortak seri yol | `0x54268C`: `!BYPASS2E\r\n`; hazırlık BRDY/RUN, sonra gerçek ASCII cevap gerekir. Ayrıntı: [önceki seri kanıtı](../public/VALIDATION.md). |
| Native kimlik isteği | `0x52BA9C`, `0x530D50`: FC67, adres `0010`, 5 word. |
| Kimlik çözümü | `0x52BCEC`: 5 big-endian word; ilk ikisi firmware version çiftidir, **ikinci kapasite değildir**. Üçüncü `0001`, dördüncü `000E` → `0x52BFC7` group 5 / 15840 byte; `0x52C718` → `0xA7E460` literal `SS2`. Beşinci word fingerprint'te korunur; anlamı varsayılmaz. |
| Private frame builder | `0x4BBEF8`: station, FC64, mode, subcommand, bytecount, payload; `0x52A438` ASCII hex/LRC/CRLF katmanı. |
| Program okuma/yazma | `0x53777C` / `0x537320`: `2000 + byte_offset/2`; CDC connection type **3** için 32 byte = 16 word. Model group **5**, connection type 5 ile karıştırılmaz. |
| Program payload | `0xA349B6` → `_memcpy`; `0xA34A10` → `_swab`. Program yükleme `_memcpy` kullanır: çözülmüş **little-endian** byte çiftleri wire'da aynen korunur. Register/kimlik/descriptor word'leri ayrıca big-endian'dir. |
| Download ilerleme ve kontrol | `0x5336F0` request / `0x5343B8` response; `0x536248` ACK sonrası offset ilerletir; `0x534055` initial-value descriptor; `0x533C27` M1073; `0x533D2B` D1004. |
| Initial-value block | `0x523B93` offset/bytecount; `0x523E05` program sonuna block ekleme; `0x5327AE` upload sırasında block çözümü. |

**GÖZLEM:** `0x52F218` ve `0x529E3C` END `0F00` değerini iki-byte sınırlarında arar; instruction-aware oldukları iddia edilmez. Tam yedek 7920 word / 15840 byte'tır. 495 adet 16-word okuma `2000–3EEF` aralığını kapsar. Ham yedekte END sonrasındaki byte'lar da korunur; vendor upload'ın yerel FF normalizasyonu ham yedeğe uygulanmaz.

## Wire sözleşmesi ve açık ACK sınırı

Aşağıdaki station-1 frame'leri statik builder'dan çıkarılmıştır; canlıya gönderme örneği veya bağımsız yetki değildir. Station enrollment'dan gelir; broadcast kullanılmaz.

| Typed işlem | ASCII request (`\r\n` gerçek CRLF) | Kabul |
|---|---|---|
| Native identity | `:01670010000583\r\n` | FC67 + bytecount `0A` + 10 byte, geçerli station/LRC; SS2 marker/model eşleşmesi. D1002 kapasite ayrıca 7920 olmalı. Exact CPU parça numarası ayrıca doğrulanır. |
| Program-mode entry | `:0164011901017F\r\n` | Private64/sub19 `01`; exact ACK şekli canlı kalifikasyon gerektirir. |
| Program-mode exit | `:01640119010080\r\n` | Private64/sub19 `00`; upload/geri-okuma kapanışı. |
| PLC-ID status | `:0164012A01006F\r\n` | `0x536F97` payload status `00`: ID kapalı; `FF`: ID koruması. Native bytecount `0xB4E90B`, payload başlangıcı `0xB4E90C`. Koruma/authentication bypass yapılmaz. |
| İlk program segmenti | `:010320000010CC\r\n` | FC03 + tam 32-byte payload; sonraki adres `2010`. Payload byte'ları swap edilmez. |
| Syntax check | `:01050C31FF00BE\r\n` | M1073 FC05 tam echo ACK; bu RUN komutu değildir. |
| Syntax sonucu | `:010313EC0001FC\r\n` | D1004 FC03 1 word; sonuç `0000` olmalı. |

Her cevap station, function, LRC, uzunluk ve izinli durumlarla denetlenir; exception/timeout/eksik gönderim başarısızlıktır. Native private callback checksum ve nonexception sonrası ilerler, ancak sub19 ACK byte'larını strict echo olarak sınamaz. Bu nedenle strict echo test fixture'ı vendor ACK kanıtı olamaz. **Private ACK live qualification NOT_RUN:** canlı probe yalnız **ACK_SHAPE_ONLY** sonucu verir. Cevap request ile aynı olduğundan gateway/serial echo olabilir; mode etkisi ve program aktarımı ayrıca doğrulanmalıdır. Aktarım kabulünü tam ham yedek, request echo'sundan farklı FC10 address/quantity ACK'leri, D1004 ve bağımsız tam program/tail/descriptor/parametre geri okuması belirler.

M1069 password flag için FC01 `0C2D`, quantity 1 okunur; protected hedefte credential tahmini veya başka yoldan program okuma yapılmaz. Blank program-ID adayında private2A `FF` sonucu reddedilir. Upload-disable mekanizmasına erişim engelini aşmak için dokunulmaz: native GUI private64/sub32 kullanır; `00` paketinin salt-okunur query olduğu kanıtlanmamıştır. Genel coil/register/raw-frame yazıcısı açılmaz.

## Bounded işlem ve kurtarma

1. Güncel source/candidate/payload digest'lerini ve exact V7 enrollment'ı sabitleyin; COM6 lease, süre ve fiziksel koşulları doğrulayın. Native identity, koruma, STOP ve D1408/D1409 ile ilgili hedef parametrelerini snapshot alın. STOP işlemi ACK + taze M1000=OFF gerektirir; RUN yapılmaz.
2. Private ACK kalifikasyonu ve gerekli yetki geçerliyse program-mode entry → **tam 7920-word ham backup** → mode exit. Ham wire trace, backup SHA ve parametre snapshot'ı kaydedilir. Okuma sonunda program, runtime parametreleri ve STOP durumu değiştirilmiş gösterilmez.
3. `D1408/D1409` (`1580`, 2 word) **initial-value block byte offset/bytecount** descriptor'ıdır; PLC ID/password değildir. Desteklenen no-initial-values profilinde aday ve hedef `FFFF/FFFF` olmalı. NonFFFF hedefte eski descriptor'ı kör koruma/sıfırlama yoktur: block relocation ve CRC desteği kalifiye olana kadar **UNAVAILABLE**. Runtime parametre snapshot'ı ile native initial-value block aynı şey değildir.
4. Yazma açıkça yetkili ve bütün kapılar geçerliyse entry → FC10 `2000+offset/2`, en fazla 32 byte segment → her segment için tam address/quantity ACK → no-initial-values descriptor tail `1580`, 2 word, dört FF → M1073 → D1004=0. Son segment kalan çift byte sayısına göre küçülür; her adım bounded ve tek gönderimdir. Native download'da ayrı sub19 `00` finalize görülmemiştir; yeni paket icat edilmez.
5. Yeni upload/readback oturumunda entry → bağımsız program okuma → exit; yazılan aktif image ve END, descriptor tail ve parametre snapshot'ını karşılaştırın. Native download yalnız aktif byte'ları yazar, tüm kapasiteyi silmez; END sonrası eski byte'lar full-image eşitliği için yanlış negatif oluşturabilir. Tam ham yedek korunur. Final STOP taze doğrulanır ve port/lease bırakılır.
6. Eksik ACK veya bağlantı kopması yazmanın gerçekleşmediğini kanıtlamaz. Otomatik replay/download retry yapılmaz. Son ACK'li offset ve belirsiz segment kaydedilir; hedef STOP/identity uzlaştırılır, bağımsız okuma ile gerçek image belirlenir. Rollback yalnız yedek digest'i, aynı hedef ve geçerli typed yetkiyle yürütülür; belirsiz hedefe yazılmaz.

Native akışta `0xA34974` `_gettime` çağrısından oluşturulan host saat/dakika/saniye FC10 `13F8` yazımı da görülür. Bu **RTC güncellemesi**, salt backup veya program-only kabulüne eklenmez. Password/ID, upload enable/disable, genel parametre/coil yazma ve otomatik RUN da bu sözleşmede bulunmaz.

## Kabul kanıtı

Frame/parser testleri ve native dosya çözümü **software** kanıtıdır. Canlı transfer hükmü; latest-approved source referansı, ayrı digest'ler, hedef/lease/enrollment, raw TX/RX, backup, bütün ACK'ler, D1004, bağımsız geri okuma/tail/parametre karşılaştırması ve final STOP ile verilir. Bağımsız NORMAL whole-packet review tamamlanmadan nihai kabul kapanmaz.

[Delta SS2 instruction sheet](https://filecenter.deltaww.com/Products/download/06/060301/Manual/DELTA_IA-PLC_DVP-SS2_I_TSE_20240222.pdf) program ve retained data'nın flash'ta tutulduğunu bildirir. Bu, M1073 ACK'sinin flash-save tamamlanma kanıtı olduğu anlamına gelmez. Ayrı power-cycle gözlemi yoksa kalıcı saklama **NOT_RUN / İNSAN_KANITI_GEREKİR** kalır; readback, fiziksel hareket, emniyet veya FAT/SAT kanıtına dönüştürülmez.
