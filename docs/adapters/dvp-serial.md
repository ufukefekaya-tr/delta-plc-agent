# DVP seri okuma adaptörü

**17 Ekim 2026 güncel giriş:** [0.3.1 ajan rehberi](../DVP_AGENT_RUNBOOK.md), [genel M/D profili](dvp-controls.md) ve [yazılım/dış kabul kaydı](../public/VALIDATION.md). Aşağıdaki 13 Ekim FAST saha kayıtları tarihsel kanıttır; mevcut görev yetkisi veya bütün DVP modelleri için kabul değildir. Güncel kod NORMAL kapsamındaki kaynak/revizyon ve işlem sınırlarını uygular.

Güncel Universal arayüzünde target.status yalnız RUN/fault/firmware/capacity okur; eski örtük D100 sıcaklık okuması kaldırıldı. Sensör adres/birimleri [explicit proje profili](dvp-controls.md) üzerinden machine.observe ile seçilir. Aşağıdaki FAST canlı kayıtlar tarihsel kanıttır.

FAST_UNVERIFIED — 13 Ekim 2026. `delta.dvpserial`, Universal MCP üzerinden sınırlı Modbus ASCII **FC01/FC03 okuma** ve ayrıca yetkilendirilen sabit M1072 **RUN/STOP** kontrolü sunar. Önceki FAST profilinde program yüklemesi kapalıdır. Yeni ayrı enrollment ile [SS2 native aktarım katmanı](dvp-transfer.md) kullanılır; genel coil/register yazma veya bayt gönderme arayüzü yoktur. Dosya adaptörlerinin yerine geçmez.

**Bu hostta online okuma PASS:** 13 Ekim 2026 10:41:57 UTC kaydında COM6 → `!BYPASS2E` → BRDY → altı geçerli FC01/03 cevabı, iki tam durum okuması ve port kapatma görüldü. M1000=OFF (STOP), izlenen hata bitleri OFF, D100–D111=0, firmware ham `0x8840`, kapasite 7920. [Ham MCP kanıtı](../public/VALIDATION.md). Bu kayıt exact CPU kimliği, sıcaklıkların fiziksel doğruluğu veya program yükleme kanıtı değildir.

**Ayrı RUN → okuma → STOP testi PASS:** Kullanıcının güncel kontrol yetkisiyle, 10:52:34–10:52:40 UTC arasında sabit FC05 M1072 RUN/STOP ACK'leri ve her işlem sonrası taze M1000 durum doğrulaması görüldü. RUN sırasında iki durum okuması 24.6–26.2°C değerleri verdi; izlenen hata bitleri OFF kaldı. Son durum STOP doğrulandı ve port kapatıldı. [Kontrol ham kanıtı](../public/VALIDATION.md). Çalıştırılan program PLC'de mevcut programdır; yeni program yüklenmedi.

## HMI üzerinden bağlantı sınırı

Bu görevdeki yol PC → Delta HMI USB CDC (COM6) → PLC RS485'tir. COM6 ve USB VID/PID, HMI ucunu tanımlar; PLC modeli veya başarılı yönlendirme kanıtı değildir. [Delta'nın resmî DirectLink CDC açıklaması](https://destek.deltaturkiye.com/viewtopic.php?t=205), HMI USB Commode=2, aktif HMI–PLC haberleşmesi ve Delta DVP sürücüsü gerektirir.

WPLSoft'tan statik olarak çıkarılan `!BYPASS2E\r\n` başlatması ve BRDY/RUN yanıt araması somut vendor kanıtıdır. CDC ve RS232'nin başlatma sonrasında aynı Modbus ASCII gönderme/cevap yolunu kullandığı kod üzerinde ve mevcut COM6 oturumunda doğrulandı. Port kapatma da MCP üzerinden tamamlandı; HMI'nin bütün çıkış/yeniden bağlanma koşulları ve **program-transfer sözleşmesi** doğrulanmadı. `allow_read_probe=true`, yalnız kayıtlı oturumdaki sınırlı okuma probuna izin verir. Cevapsızlık, tek başına PLC veya kablo arızası kanıtı değildir. [Başlatma/devam kanıtları ve transfer fizibilitesi](../public/VALIDATION.md).

`route=directlink_cdc` ve `cdc_initialize=true` seçildiğinde adaptör, portun ilk açılışında yalnız sabit `!BYPASS2E\r\n` mesajını gönderir. Beş saniye / 513 bayt sınırında BRDY veya RUN belirteci aranır; bulunamazsa `CDC_BRIDGE_NOT_READY` döner ve PLC sorgulanmaz. Belirteç yalnız HMI hazırlığıdır: online başarı için sonrasında gerçek FC01/03 cevapları da doğrulanmalıdır. Başlatma otomatik tekrar edilmez; bağlantı kapanınca port kapatılır. `cdc_initialize` varsayılanı false'tur; genel vendor komutu alanı yoktur.

## Host ayarı ve MCP kullanımı

Host-local JSON ayarını `UNIVERSAL_PLC_DVPSERIAL_CONFIG` ortam değişkeniyle verin. Güncel kullanıcı yetkisine ve gözlenen porta göre hazırlayın; süre dolunca okuma reddedilir. Aşağıdaki örnekte `expires_at` alanını oturumun gerçek UTC bitişiyle değiştirin:

```json
{
  "enabled": true,
  "expires_at": "YYYY-MM-DDTHH:MM:SSZ",
  "port": "COM6",
  "expected_vid": 1501,
  "expected_pid": 256,
  "route": "directlink_cdc",
  "allow_read_probe": true,
  "cdc_initialize": true,
  "baudrate": 9600,
  "station": 1,
  "target_model": "DVP14SS211R"
}
```

VID/PID değerleri bu görevde gözlenen `05DD:0100` HMI içindir; başka hedefe taşınmaz. Model katalog seçimi kullanıcı beyanıdır. İlk profil 9600/7E1'dir; PLC haberleşme kayıtları değiştirilmez.

```powershell
$env:UNIVERSAL_PLC_DVPSERIAL_CONFIG = 'C:\task\dvp-read-session.json'
universal-plc-mcp
```

Mevcut MCP araçlarını kullanın:

1. `workspace.create` için `target={"vendor":"delta","family":"DVP","model":"DVP14SS211R","toolchain":"dvpserial"}` verin. Seri okuma alanında `project_format` seçmeyin.
2. Dönen `workspace_id` ve `context_digest` ile `adapter.invoke(workspace_id, operation, arguments, expected_context_digest)` çağırın.
3. `serial.ports` port envanteri; `target.connect` / `target.status` sınırlı durum okuması; `target.read` için `{"device":"D100","count":12}`; son olarak `target.disconnect` kullanın.

`target.connect` yalnız port açılışını başarı saymaz: RUN/hata ve firmware/kapasite okumaları geçmelidir. `target.observe.start/read` aynı bounded durum okumasıdır; arka plan daemon'u oluşturmaz. Okuma isteği M/D cihazı ve 1–64 adetle sınırlıdır; M1535/D4095 sınırını aşamaz.

## Ayrı yetkili RUN/STOP

`target.run` ve `target.stop`, aynı süreli host profilinde `allow_run_stop`, `heaters_isolated` ve `user_present` alanlarının **true** olmasını gerektirir. Bunlar güncel kullanıcı RUN/STOP yetkisi, fiziksel yük ayırma ve yerinde insan beyanını taşır; okuma izni tek başına kontrol açmaz. Model/USB/oturum süresi kontrolleri de sürer. Alanlar normal okuma örneğinde bulunmaz ve kendiliğinden etkinleşmez.

Yalnız FC05 **M1072 / `0x0C30`** yazılır: RUN=`0xFF00`, STOP=`0x0000`. M1035 veya başka coil seçilemez. ACK tam istek eşleşmesi gerektirir; ACK/serial echo tek başına başarı değildir. Ardından taze FC01 M1000 okuması istenen durumu doğrulamalıdır; sınırlı okuma denemeleri sonunda uyuşmazlık `CONTROL_STATE_NOT_OBSERVED` döndürür. Kontrol paketi otomatik tekrar edilmez. RUN öncesi mevcut durum, SS2 kapasitesi ve izlenen hata bitleri kontrol edilir. [Delta resmî M1072 açıklaması](https://destek.deltaturkiye.com/viewtopic.php?t=2336).

## Adres ve kanıt

Delta SS2 aile kılavuzunun iletişim tablosu M0→`H0800`, D0→`H1000` eşlemesini verir. Bunlar sıfır tabanlı PDU adresleridir; 4xxxx ekran gösterimindeki +1 uygulanmaz.

| Veri | FC | PDU adresi |
|---|---:|---:|
| M1000 RUN | 01 | `0x0BE8` |
| M1004 syntax / M1008 watchdog / M1009 voltaj | 01 | `0x0BEC` / `0x0BF0` / `0x0BF1` |
| D100–D111 | 03 | `0x1064–0x106F` |
| D1001 firmware / D1002 kapasite | 03 | `0x13E9` / `0x13EA` |

Kaynak: [Delta SS2 aile programlama kılavuzu, Bölüm 2 ve 4](https://device.report/m/b6f3677a1eb063f5e12ab9ed2e46e8926aafe668c58c558f27436f45ab2b3727). D1001/D1002 exact CPU modeli kanıtlamaz. BRDY/RUN köprü yanıtları da PLC RUN kanıtı değildir; M1000 okunmalıdır. Bu makinede D100–D111 için 0.1°C yorumu proje programına bağlıdır; her DVP projesine genellenmez.

LRC, istasyon, fonksiyon ve byte sayısı kontrol edilir. Echo başarı kabul edilmez. Port meşgul (`PORT_BUSY`), USB kimliği uyuşmazlığı, oturum süresi, timeout ve bozuk cevap açık hata döndürür. Fiziksel sonuç ancak canlı okuma kanıtı ile raporlanır; yazılım testleri kendi başına online başarı değildir.

`program_transfer_enabled=false` olan önceki profilde `project.upload`, `deployment.prepare` ve `deployment.execute`, aday dosya/SHA doğrulaması ardından **`PROGRAM_TRANSFER_UNAVAILABLE`** döndürür. `deployment.reconcile`, hiç transfer uygulanmadığını bildirir; yükleme sonrası PLC karşılaştırması değildir. Program aktarımı kabiliyeti eklenmiş sayılmaz.

Tarihsel ve engelli [G-DVP-5](../../goals/dvp-transfer.json), o oturumun V7/en son onaylı dosya talebini kaydeder; yeni görevin kaynağını veya yetkisini seçmez. Güncel iş tesliminin varsayılan yükleme yolu WPLSoft Computer Use’tur. `program_transfer_enabled=true` yeni katmanı seçer; canlı özel ACK kalifikasyonu, güncel onay manifesti, orijinal kaynak SHA'sı, kimlik, STOP, süreli fiziksel koşullar ve tek sahipli işlem kaydı ayrı zorunlu kapılardır. `program.contract.qualify` yalnız kanıtlanmış mode giriş/çıkış cevaplarını kaydeder; program yazmaz. Statik/test sonucu canlı yükleme kanıtı değildir.
