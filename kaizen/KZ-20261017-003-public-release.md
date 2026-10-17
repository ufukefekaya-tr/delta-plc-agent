# KZ-20261017-003 — Ayrı yayın geçmişi ve denenebilir başlangıç

- Baseline/hash: `e440fefd45ba14cd351c9bea79d64b62295c4d1b`.
- 10. Adam karşı görüşü: Adları temizlemek eski Git geçmişindeki saha kayıtlarını gizlemez; profil silinmesi ona bağlı testleri kırabilir.
- Yanlış genelleme sınırı: Kaynak temizliği, açık lisans ve CI fiziksel PLC kabulü değildir.

## Gözlem

Genel çekirdek ve saha test kayıtları aynı yerel repoda birikmişti. Halka açık dağıtımda ürün ve özel geçmiş ayrılmalı; başlangıç denenebilir olmalıydı.

## Kök neden hipotezi

Yerel mühendislik kanıtı ile halka açık dağıtımın sınırları farklıdır. Tanıtım da runtime yetenek kaydının yerine geçmez.

## En küçük değişiklik

İngilizce/Türkçe ürün girişi, özellik haritası, offline demo, katkı akışı ve MIT atfı eklendi. Yayın aracı temiz kaynak üretir; özel geçmiş taşınmaz. DVP testleri için makine metadatası ve gerçek parametreleri çıkarılmış nötr regresyon fixture’ı kullanılır. Runtime korunur.

## Doğrulama

[Workflow](../workflows/public-launch.json), [hedef](../goals/public-launch.json), [doğrulama](../docs/public/VALIDATION.md) ve [kaynak ayrımı](../docs/public/PROVENANCE.md). Yayın ağacı test, temiz kurulum ve bağımsız son incelemeyle değerlendirilir.

## Temiz ortamdan öğrenilen ders

Temiz Linux CI ilk çalışmada geliştirme bağımlılığının eksik olduğunu ve Windows pencere testlerinin host varsayımını gösterdi. Test kurulumu sabit sürümlü geliştirme bağımlılıklarını yükler; Windows davranış testleri yalnız hedef modülün platform bağını taklit eder. Global `os.name` değiştirilmez, assertion veya test kapsamı azaltılmaz. Runtime değişmeden odaklı 19 test geçti; tam Windows/Linux kabulü GitHub CI kaydına bağlanır. Kaynak ZIP, host satır sonu ayarlarından bağımsız LF ile üretilip her girdisi Git blobuyla karşılaştırılır.

Windows kısa/uzun geçici yol adları test fixture'larında kanonikleştirilir. Heartbeat testi sabit 50 ms uyku yerine üç gerçek kaydın sınırlı sürede oluşmasını gözler; kayıt sayısı ve fingerprint assertion'ları korunur. CI ortamının zamanlama farkı ürünün liveness sözleşmesiyle karıştırılmaz.

## Rollback

Yerel geçmiş korunur; hazırlık ayrı revert commit’iyle geri alınabilir. Lisansı değiştirmek önceki açık kaynak kopyalarının izinlerini ortadan kaldırmaz.

## Sonuç

Kullanıcı demo ile deneyebilir; ajan doğru yeteneklere yönlenir. Yayın geçmişi makine oturumlarından ayrılır. Kabul yayımlanan commit ve CI ile izlenir.
