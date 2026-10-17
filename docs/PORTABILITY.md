# Bağımsız kurulum ve taşınabilirlik

## DVP kurulumu ve ajan yönergesi

`python tools/install.py --profile dvp` yalnız Universal paketi ve Python bağımlılıklarını kurar; AX kurulmaz. Varsayılan `all` önceki davranışı korur. Windows için `tools/bootstrap_dvp.ps1`, uygun Python/özel venv ve DVP paketini hazırlar. `-CheckOnly` kurulum yapmaz. [Tam ajan rehberi](DVP_AGENT_RUNBOOK.md), [resmi kaynak ve kurulum sınırları](../packaging/dvp-prerequisites.json), [iş şablonu](../examples/dvp-job.json).

`python tools/portable_smoke.py --profile dvp` yalnız Universal wheel'i yeni ortamda kurar, kaynak PYTHONPATH olmadan gerçek stdio MCP roundtrip sınar ve AX import edilmediğini kontrol eder. Vendor IDE/driver kurulumu veya canlı PLC kabulü değildir. WPLSoft/driver otomatik kurulum tarifleri doğrulanmış değilse eksik host bileşeni açık engeldir; paket bunları kurulmuş saymaz.

## Paket sınırı

Repo başka bilgisayara kopyalandığında `tools/install.py` yalnız kendi konumundan kökü bulur. Universal wheel Python modülleri, JSON model/workflow kaynakları ve lisanslı Delta dönüştürücülerini taşır. AX wheel kendi Python kodunu, işçilerini, şemalarını ve protokol kaynaklarını taşır. Eski kaynak checkout'una fallback import yoktur.

`UNIVERSAL_PLC_ROOT` çalışma verisinin host-local köküdür. `UNIVERSAL_PLC_READ_ROOTS` bu oturumda içeri aktarılabilecek klasörleri OS path separator ile belirtir. `UNIVERSAL_PLC_AX_CONFIG` o hostun AX yapılandırmasıdır. Bunların değeri dağıtılan repo içinde kullanıcı bilgisayarına sabitlenmez. CLI `--root` ve tekrarlanabilir `--allow-read` aynı amaçla kullanılabilir.

## Windows ve sunucu yerleşimi

Python host ve dosya işlemleri vendor IDE açmadan çalışır. AX native işçisi Windows ve uygun DIADesigner-AX/ScriptEngine/device paketlerini gerektirir. ISPSoft/WPLSoft/DIADesigner native işleri de ilgili Windows araç zincirini gerektirir. Bunların lisanslı binary dosyaları repoya kopyalanmaz.

Ajan başka bir sunucuda çalışıyorsa standart SSH üzerinden Windows hosttaki stdio MCP başlatılabilir; istemci taşıması SSH'ye aittir. Repo kendi uzak ağ erişim yetkisini veya kimlik bilgilerini üretmez. Bu teslimin Windows temiz-kurulum testi uzaktaki bir hostun veya Linux native IDE çalışmasının kanıtı değildir. Yeni hostta `doctor`, model/IDE fingerprint ve o hostun qualification'ı yeniden alınır.

## MCP istemci yapılandırması

Kurulumun Python ortamında çalışan `universal-plc-mcp` komutunu istemcinin stdio sunucusu olarak seçin. Alternatif komut `python -m universal_plc --root ./plc-agent-data serve` olur. Birden fazla Python kurulumu varsa istemcide bu hostun sanal ortam yorumlayıcısını seçin; örnek config içine geliştiricinin mutlak yolunu kopyalamayın.

## Tekrarlanabilir test

`python tools/portable_smoke.py` iki wheel'i üretir, repo dışındaki geçici ortamda kurar, farklı çalışma dizininden import/CLI/MCP kaynaklarını ve dosya işlemlerini sınar. Test ortamına kaynak repo PYTHONPATH'i taşınmaz. Sonuç açıkça SOFTWARE_ONLY'dir.

Kopyalanması gerekenler Git-tracked dosyalardır. `.agent-state`, venv, kullanıcı projeleri, IDE kurulumu, credential ve eski makine dosyaları dağıtım gereksinimi değildir. `reports` içindeki tarihsel commit/hash referansları kaynak izlenebilirliğidir; runtime girdisi değildir.

## Kaynak ZIP ile başka bilgisayara taşıma

Teslim ZIP’i yalnız commit edilmiş ürün dosyalarını içerir. ZIP’i istediğiniz klasöre açın, o bilgisayarda Python 3.11+ ortamıyla `python tools/install.py` çalıştırın, ardından `universal-plc doctor` ile paket ve yerel araçları kontrol edin. Kurulum normal Python bağımlılıklarını paket deposundan indirir; internetsiz kurulum için ayrıca uygun işletim sistemi/Python sürümüne ait wheel deposu gerekir.

ZIP çalıştırmak için Git geçmişine ihtiyaç duymaz. Repo üzerinde geliştirme ve session guard kullanımı için mevcut Git reposunu geçmişiyle kopyalayın veya açılmış kaynakları kendi Git reponuzda ilk commit olarak kaydedin. MCP çalışma dizini, proje erişim kökleri, hedef bilgisi ve IDE yolları yeni bilgisayarda seçilir; kaynak bilgisayardaki `.agent-state` klasörü taşınmaz.
