# KZ-20261011-001 — Taşınabilir ve hedefe bağlı PLC çalışma zamanı

- Baseline/hash: bd707bcebc11b767c95535880ae0aa28deb61d9e; AX kaynak fc74073456b0417815faddcb937648facd4c85ac.
- 10. Adam karşı görüşü: Yalnız klasör taşımak bağımsızlık kanıtı değildir; geniş model regex'i yanlış hedefi kabul edebilir; iptal edilmiş bir backend işi bağımlı adımı başlatabilir.
- Yanlış genelleme sınırı: Windows wheel kurulumu başka işletim sisteminde vendor IDE çalışmasının veya bütün PLC modellerinin kabulü değildir.

## Gözlem

Kaynak AX birikimi makine programıyla aynı çalışma ağacındaydı. Universal ana repo çalışan adaptör içermiyordu. İlk karşı inceleme AS workspace'inden DVP üretimi, iptal sonrası devam ve uydurma modele katalog eşleşmesi buldu. Boş AX projesindeki task-call düğümü ile aynı adlı gerçek POU birbirine karışabiliyordu.

## Kök neden hipotezi

Dosya işlemlerinin hedef bağlamı ve gerçek proje metadatası arasında bağlantı yoktu. Workflow yalnız birkaç başarısızlık durumunu engelliyordu. Model ailesi deseni tam model kimliği gibi kullanılıyordu. Kaynak checkout'unda çalışan import, kurulmuş pakette kaynak bulunacağını kanıtlamıyordu.

## En küçük değişiklik

Her adaptöre değişmez hedef profili aktarılır; DVP işlemi AS üzerinde reddedilir. Workflow yalnız tanınan başarılı terminal durumdan devam eder. Tam model listesi aile regex'inin önüne geçer. İki wheel kaynaklarıyla birlikte kurulur; yollar config dosyasına veya çalışma köküne göre çözülür. AX factory gerçek textual POU ile task-call düğümünü ayırır ve vendor görevlerini korur.

## Doğrulama

[Core testleri](../tests/test_universal_runtime.py), [Delta testleri](../tests/delta_files/test_file_adapters.py), [AX testleri](../tests/ax/test_project_factory.py), [temiz kurulum](../docs/public/VALIDATION.md) ve [native kanıt](../docs/public/VALIDATION.md) birlikte tutulur. Native çağrılan ST POU için 0 hata → kasıtlı hata → düzeltme sonrası 0 hata görüldü. İlk katalog uyarıları silinmedi. Sonraki public denemede compiler özeti ile farklı kategorideki IDE uyarılarının sayımı ayrıldı; ham tanılar uyuşmazlıkta da korunuyor. Tüm hata/uyarı kapıları aynı kaldı: nötr ST/build/Ladder preflight çevre uyarıları nedeniyle BLOCKED, freeze uygun derleme bulunmadığı için reddedildi; kaynak ve revizyon değişmedi. Bu bulgu başka cihaz profillerinde uyarı toleransı varsayımına dönüştürülemez. Canlı PLC sonucu yoktur.

## Rollback

Yerel commit'ler yeni commit ile geri alınabilir. Kaynak çalışma ağaçları ve kullanıcı projeleri değiştirilmedi. Üretilmiş adayın byte'ları sonradan yeniden yazılmaz; düzeltme yeni aday ve yeni kanıt üretir. Live rollback bu yazılım kaydından yetki devralmaz.

## Sonuç

Taşınabilir paket ve hedef bağlama kazanımı doğrulandı. WPLSoft/ISPSoft native kabulü, taşınan AX Ladder native zinciri ve canlı hedef doğrulaması açık hedeflerdir. Kaynak AX'in 17 dersinin kayıt bazlı değerlendirmesi [AX ders envanterindedir](../backends/ax/docs/AX_LESSONS.md).
