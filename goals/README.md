# Universal PLC — Hedefler ve kabul durumu

**Güncel DVP teslim hedefi:** [G-DVP-FINAL](dvp-final.json), [ajan workflow](../workflows/dvp-final.json) ve [teslim/kabul durumu](../docs/public/VALIDATION.md). Aşağıdaki 11–13 Ekim V7/V10 kayıtları tarihsel hedeflerdir; güncel program veya canlı yetki seçmez.

13 Ekim ek işi: [DVP seri MCP FAST hedefleri](DVP_SERIAL_FAST.md). Online bağlantı ve program aktarımı ayrı kabul kayıtlarıdır. Tarihsel ve engelli [G-DVP-5 aktarım hedefi](dvp-transfer.json), o oturumun V7 talebini kaydeder; yeni görevde V5/V7/V8 adı canlı aday veya yetki seçmez.

Yeni aktarım yazılımı, canlı okuma ve açık fiziksel kabul koşulları: [SS2 MCP teslimi](../docs/public/VALIDATION.md).

17 Ekim 2026 genel uzlaştırma. AX/ISP kanıtları kendi tarihleriyle, DVP 0.3.1 gelişmeleri güncel alt hedefleriyle izlenir. Bu liste onaylanan G0–G6 planının gerçekleşen durumudur; yazılım testi ile gerçek IDE ve canlı kabul birbirinden ayrılır. Makinece okunabilir karşılık: [goals.json](goals.json). Tek bir tamamlanma yüzdesi verilmez.

| Goal | Bağımlılık | Teslim ve gerçekleşen iş | Kabul durumu / açık iş |
|---|---|---|---|
| G0 | — | Kaynak sürümleri, ZIP özeti, MIT lisansları; 240 AX kaynak kaydı ve 17 kaizen adayı | Tamam: kaynak/disposition envanteri korunuyor. |
| G1 | G0 | Marka bağımsız bootstrap, exact model kataloğu, 11 araçlı tek MCP, hedef bağlı workspace, kanıt, W1–W7 | Yazılım kabulü tamam. Sentetik ikinci üretici testi gerçek başka marka desteği değildir. |
| G2 | G1 | AX backend ve 19 eski operasyon; public `project.create`; ST/Ladder işçileri, deployment/supervisor, genel kaizenler | Kısmi: nötr AX308 proje oluşturma ve save/reopen public MCP'de geçti. Doğrudan vendor derlemesi pozitif/negatif/düzeltilmiş olarak geçti. Düzeltilmiş public ST/build/Ladder, 21 boş slot çevre uyarısında BLOCKED; freeze uygun derleme yokluğunda reddedildi. Bu native zincirin kabulü açık. Diğer AX modelleri katalog adayıdır. |
| G3 | G2 | ISP kapsayıcıları, ST/POU patch, paket ve library export/import | Kısmi: dosya regresyonları geçti. AS native oluşturma/edit/build/save–reopen açık. Pencere kontrolü elde edilemedi; belgelenmiş native CLI bulunamadı. |
| G4 | G2 | DVP dosya araçları, WPLSoft ajan üretim/derleme/yükleme tarifi ve 0.3.1 DVP kurulumu | Kısmi: yazılım ve iş devri hazır; 17 Ekim vendor aşaması BLOCKED_VENDOR_UI. WPLSoft MCP derleyicisi veya genel DVP→AS dönüşümü yok. DIADesigner native yolu açık. |
| G5 | Hazır adaptör | AX plan/authority/observe; DVP seri ASCII/CDC, durum/kimlik, profil bağlı M/D, watch ve toparlanma; deneysel SS2 aktarımı | Kısmi: DVP yazılımı uygulanmış, tarihsel sınırlı canlı kayıtlar var. 0.3.1 güncel fiziksel watch/M-D NOT_RUN; seri port yoktu. Native program aktarımının canlı kabulü, AX ve ISP diğer açıkları kapanmadı. |
| G6 | G1–G5 | Universal paket ve AX kanıtları; DVP 0.3.1 için 228 test, temiz kurulum ve bağımsız PASS_SOFTWARE; güncel genel indeks/rapor | Yazılım teslimleri kayıtlıdır; G2–G5 açık kabulleri nedeniyle tam PLC kabulü tamamlandı sayılamaz. |

## Devam iş paketleri

1. `G2-NATIVE`: Önce boş slot/device-description çevre uyarılarının kök nedenini exact model/device paketiyle doğrulamak. Ardından aynı nötr proje ve exact AX profiliyle public workspace→ST/Ladder→build→freeze zincirini gerçek IDE'de sınamak. Her işlem için girdi/çıktı hash'i, worker sürümü ve sonucu kaydetmek.
2. `G3-NATIVE`: ISPSoft için gözlenebilir pencere erişimi veya belgelenmiş API sağlandığında nötr AS projesini oluşturmak. Çağrılan POU hatasıyla negatif derleme ve düzeltme sonrası save/reopen kabulü almak. Internal DLL export adları desteklenmiş API sayılmaz.
3. `G4-WPL`: Nötr DVP projesinde IDE ile `.dvp` oluşturma ve WPL→ISP geçişini adres/sembol/task karşılaştırmasıyla doğrulamak. Semantik kayıp raporunu zorunlu tutmak.
4. `G5-LIVE`: Kullanıcının seçtiği exact hedef/revizyon ve güncel fiziksel koşullar kaydedildiğinde ilgili adaptörün gerçek upload/download/RUN/STOP/watch ve kurtarma zincirini doğrulamak. Önceki makinenin yetkisi taşınmaz.
5. `G6-CLOSE`: Bu iş paketlerinin kanıtlarıyla capability qualification'larını güncellemek, son bağımsız inceleme ve yeni yerel commit almak.

Her hedef kaydı; `status`, bağımlılık, teslim, kabul ölçütü, kanıt ve açık engel taşır. `implemented` ile `native_verified` farklı alanlardır. Eksik IDE veya donanım sonucu başarıya çevrilmez.

## Kanıtlar

- [AX gerçek IDE kaydı](../docs/public/VALIDATION.md)
- [Temiz wheel kurulum kaydı](../docs/public/VALIDATION.md)
- [AX kaynak envanteri](../docs/public/PROVENANCE.md)
- [Delta kaynak envanteri](../backends/delta_sources/manifest.json)
- [İş akışları](../workflows/README.md)
- [Uygulama raporu](../docs/public/VALIDATION.md)

Universal DVP profil bağlı kontrol ve açık canlı sınırlar: [genel MCP teslimi](../docs/public/VALIDATION.md). Önceki aktarım hedefi ayrı ve BLOCKED kalır.

13 Ekim tarihsel kullanıcı yönlendirmesi: [Universal DVP M/D kontrol hedefi](dvp-controls.json). D parametre düzenleme dahildir; bağlantı tarama/auto-detect soruları yeni hedef kapsamına eklenmedi. [Mevcut/eksik özellik envanteri](../docs/public/VALIDATION.md) final sürüm/commit için kullanılacaktır.
