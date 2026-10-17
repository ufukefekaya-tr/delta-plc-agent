# Model ve araç kataloğu

Kanonik makine verisi [catalog.json](../src/universal_plc/data/catalog.json), AX cihaz kimlikleri [ax-models.json](../src/universal_plc/data/ax-models.json) içindedir. Paket resource API'siyle okunurlar; mevcut çalışma dizinine bağlı değillerdir.

30 AX model adı kurulu vendor cihaz kataloglarından metaveri olarak çıkarılmıştır; cihaz XML binary/üretici paketi dağıtılmaz. Bu liste gerçek native/hardware qualification değildir. Her host uygun device paketini sağlamalıdır.

AS/DVP kuralları kaynaklandırılmış uyumluluk adaylarıdır. Eski kataloglarla yeni kurulum kaynakları çeliştiğinde `mapping_uncertainties` görünürdür. ISPSoft 3.19 içindeki AS100 ve yeni DVP kaynaklarının varlığı native destek kanıtı değildir; aynı şekilde eski kitapçık tek başına kalıcı ret kanıtı sayılamaz. Yeni model profili eklemek önceki modelin live kanıtını devralmaz.

DIADesigner dosya uzantıları `.diade`, `.diabak`, `.diadelib`; AX `.project` ve `.projectarchive`; ISPSoft `.isp`; WPLSoft `.dvp` olarak kurulu dosya ilişkilendirmelerinden gözlenmiştir.

## DVP / WPLSoft 0.3.1 yönlendirmesi

Donanım modeli ile dosya IDE’si ve online adaptör ayrı seçilir. `.dvp` dosyası `delta.wplsoft` katalog yoluna gider; bu eşleşme `project.build` gibi bir MCP uygulaması bulunduğu anlamına gelmez. Üretim/derleme/yükleme ajan tarafından [WPLSoft rehberiyle](../docs/DVP_AGENT_RUNBOOK.md) yürütülür.

Online MCP için `toolchain=dvpserial` kullanılır; `project_format` boş bırakılır. Mevcut seri katalog kaydı yalnız tam model `DVP14SS211R` içindir. Başka DVP modelleri bu kayda zorlanmaz. Donanım kimliği ayrıca doğrulanır; mevcut kayıttaki model beyanı tüm CPU varyantlarının kabulü değildir.

`delta.dvpserial` seri durum/okuma, profil bağlı M/D işlemleri ve watch sunar. `capabilities.list` uygulanmış işlemleri; katalog ise olası model/araç eşlemesini gösterir. Katalogdaki `agent_workflow` alanı kaynak repo rehberine ait açıklamadır, yürütülebilir MCP operasyonu veya canlı yetki değildir. [0.3.1 yazılım ve dış kabul kaydı](../docs/public/VALIDATION.md).
