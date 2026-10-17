# Universal PLC Mühendisliği Kültür Aktarımı

Bu belge, farklı PLC aileleri ve IEC 61131-3 dilleri arasında taşınabilen mühendislik derslerini anonimleştirir. Bir ürün kataloğu, hazır program veya saha onayı değildir. Her kart aynı düşünme zincirini izler: gözlenen sorunu ayır, kök nedeni sınanabilir kur, universal kuralı uygula, kanıt iste ve kuralın nerede geçersiz olabileceğini açıkla.

## 1. Tek kaynak iddiası, çift otorite gerçeği

- **Sorun →** I/O, parametre veya çalışma sırası için “tek kaynak” denir; ancak aynı bilgi programda, tabloda ve dokümanda ayrı ayrı düzenlenebilir.
- **Kök neden →** Kaynak olma iddiası ile üretim zincirindeki gerçek yazma yetkisi karıştırılır; türetilen çıktı yeniden elle düzenlenir.
- **Universal kural →** Her bilgi alanının tam bir sahibi olmalı; diğer gösterimler o kaynaktan üretilmeli veya açıkça yalnız referans sayılmalıdır. İkinci bir düzenleme noktası varsa sistem tek kaynak değildir.
- **Gereken kanıt →** Alan bazlı sahiplik matrisi, kaynak-türev izi, aynı değişikliğin bütün çıktılara yansıdığını gösteren kontrol ve elle düzenleme noktalarının listesi.
- **Yanlış genelleme sınırı →** Bütün proje için tek dosya zorunlu değildir; farklı alanların farklı kanonik sahipleri olabilir. Kural, tek bir bilginin iki bağımsız otoritesi olmamasıdır.

## 2. Kaynak, derlenmiş çıktı ve yüklü proje ayrımı

- **Sorun →** Okunabilir kaynak günceldir diye çalışan PLC’nin de aynı mantığı yürüttüğü varsayılır.
- **Kök neden →** Kaynak metin, IDE projesi, derlenmiş binary ve kontrolöre yüklenmiş sürüm tek nesne gibi ele alınır.
- **Universal kural →** Bu dört katman ayrı kimliklerle izlenmeli; bir katmandaki inceleme diğerinin eşitliğini kanıtlamamalıdır.
- **Gereken kanıt →** Kaynak sürüm kimliği, derleme kaydı, binary özeti, yükleme kaydı ve mümkünse çalışan sistemden alınmış doğrulanabilir sürüm bilgisi.
- **Yanlış genelleme sınırı →** Bazı araç zincirleri katmanları tek paket içinde saklayabilir; yine de paket içeriği ile sahada çalışan sürümün eşitliği ayrıca kanıtlanır.

## 3. Statik kontrol ve test drift’i

- **Sorun →** Kontrol aracı yeşil sonuç verirken gerçek program yeni bir sözleşmeye aykırı davranır.
- **Kök neden →** Testler eski isimleri, eski dosya düzenini veya artık geçerli olmayan varsayımları doğrulamaya devam eder.
- **Universal kural →** Ürün sözleşmesi değiştiğinde test sözleşmesi de gözden geçirilmeli; bir testin varlığı değil güncel riski yakalaması değerlendirilmelidir.
- **Gereken kanıt →** Gereksinim-test izlenebilirliği, bilerek bozulan örnekte testin kırıldığını gösteren mutation veya negatif test ve eski kabul ölçütlerinin temizlendiğine dair inceleme.
- **Yanlış genelleme sınırı →** Her değişiklik bütün testlerin yeniden yazılmasını gerektirmez; yalnız değişen davranışa, arayüze ve riske bağlı testler güncellenir.

## 4. Fiziksel çıkış ve kritik state için tek yazar

- **Sorun →** Aynı fiziksel çıkış veya kritik state farklı POU, task, network ya da dillerde yazılır; son taranan mantık görünmez biçimde kazanır.
- **Kök neden →** Fonksiyonlar sahiplik yerine doğrudan donanıma veya paylaşılan state’e bağlanır.
- **Universal kural →** Her fiziksel çıkışın ve kritik state’in tek yazarı olmalı; diğer modüller request, permit ve inhibit gibi anlamlı arayüzler üretmelidir.
- **Gereken kanıt →** Output/state sahiplik tablosu, çapraz referans raporu, task sırası ve bütün çalışma modlarında tek yazarı doğrulayan inceleme.
- **Yanlış genelleme sınırı →** Birden çok koşul tek yazarın içinde birleşebilir; yasak olan çok sayıda okuyucu değil, bağımsız yazma yetkisidir.

## 5. PLC scan’i boyunca command/ack handshake

- **Sorun →** Tek taramalık komut kaybolur, sürekli tutulan komut tekrar tekrar işlenir veya haberleşme yenilemesi aynı işlemi çoğaltır.
- **Kök neden →** Üretici ve tüketici farklı scan, task veya iletişim çevrimlerinde çalışırken olayın yaşam döngüsü tanımlanmamıştır.
- **Universal kural →** Komutun kabul, işleme ve tamamlama aşamaları açık bir handshake ile modellenmeli; tekrar, gecikme ve bağlantı kesintisi davranışı tanımlanmalıdır.
- **Gereken kanıt →** Zaman diyagramı, command/accepted/busy/done/fault geçişleri, bir ve çok scan süren darbeler için testler ve yeniden bağlanma senaryosu.
- **Yanlış genelleme sınırı →** Aynı task içindeki basit, sürekli seviye sinyali her zaman handshake gerektirmez; olay, sınır veya farklı zaman tabanı varsa gerekir.

## 6. State beklemelerinde timeout ve abort

- **Sorun →** Bir sensör, eksen veya alt sistem beklenen cevabı vermezse sekans sonsuza kadar aynı state’te kalır.
- **Kök neden →** Mutlu yol tasarlanmış; süre sınırı, iptal yolu ve kontrollü terk davranışı sözleşmeye alınmamıştır.
- **Universal kural →** Dış koşul bekleyen her state için ilerleme koşulu, makul timeout, abort kaynağı, fault sonucu ve yeniden giriş politikası tanımlanmalıdır.
- **Gereken kanıt →** State transition tablosu, her bekleme için zaman bütçesi, sensör hiç gelmedi/geç geldi/kayboldu testleri ve abort sonrası output incelemesi.
- **Yanlış genelleme sınırı →** İnsan onayı gibi kasıtlı sınırsız beklemeler olabilir; bunlar sınırsız oldukları ve güvenli çıkış yoluna sahip oldukları açıkça belirtilirse istisnadır.

## 7. Stop, reset, emergency ve power return aynı olay değildir

- **Sorun →** Farklı durdurma ve yeniden başlatma olayları tek bir “reset” dalında birleşir; beklenmeyen otomatik hareket veya geri dönülemez kilit oluşur.
- **Kök neden →** Operasyonel stop, fault reset, emergency stop ve enerji dönüşünün amaçları ile yetki seviyeleri ayrıştırılmamıştır.
- **Universal kural →** Her olayın output tepkisi, state etkisi, yeniden başlatma koşulu, operatör onayı ve retained veri davranışı ayrı sözleşme olarak yazılmalıdır.
- **Gereken kanıt →** Olay-davranış matrisi, her olay için enerji kesme ve geri verme testleri, otomatik restart kontrolü ve güvenlik fonksiyonunun yetkili insan doğrulaması.
- **Yanlış genelleme sınırı →** Risk analizi bu olaylardan bazılarına aynı sonuç atayabilir; eşitlik varsayımla değil belgeli tehlike değerlendirmesiyle kurulmalıdır.

## 8. Parametre snapshot’ı ve çevrim tutarlılığı

- **Sorun →** Operatör veya üst sistem çevrim ortasında bir parametreyi değiştirir; aynı ürün çevrimi iki farklı değerle yürür.
- **Kök neden →** Ayar değeri ile çalışan çevrimin sabitlenmiş değeri ayrılmamıştır.
- **Universal kural →** Çevrim bütünlüğü gerektiren parametreler tanımlı bir kabul noktasında doğrulanıp çalışma snapshot’ına alınmalı; etkinleşme zamanı kullanıcıya görünür olmalıdır.
- **Gereken kanıt →** Parametre yaşam döngüsü, kabul sınırları, çevrim ortası değişiklik testi ve hangi değerin hangi çevrimde kullanıldığını gösteren kayıt.
- **Yanlış genelleme sınırı →** Gerçek zamanlı ayarlanması amaçlanan setpoint’ler snapshot gerektirmeyebilir; bunun güvenli değişim hızı ve sınırları ayrıca tanımlanır.

## 9. Retention ve enerji dönüşü

- **Sorun →** Enerji dönüşünde eski komutlar, yarım kalmış state veya uygun olmayan parametreler sessizce geri gelir.
- **Kök neden →** “Retain edilebilir” ile “retain edilmesi güvenli ve anlamlı” aynı kabul edilir.
- **Universal kural →** Her retained alan için gerekçe, geçerlilik süresi, başlangıç doğrulaması ve geçersizse güvenli varsayılan tanımlanmalı; hareket isteği varsayılan olarak retained sayılmamalıdır.
- **Gereken kanıt →** Retention envanteri, soğuk/sıcak başlangıç testleri, bozuk veya eski veri senaryosu ve enerji dönüşü onay akışı.
- **Yanlış genelleme sınırı →** Bütün state’in sıfırlanması da her proses için doğru değildir; ölçüm toplamları veya kalıcı kalibrasyon gibi veriler kontrollü biçimde korunabilir.

## 10. Parametre ve veri migration’ı

- **Sorun →** Yeni yazılım eski kalıcı veriyi yeni anlamıyla okur; değer aralıkta görünse bile yanlış davranış üretir.
- **Kök neden →** Veri yapısının sürümü, dönüşüm kuralı ve başarısız migration davranışı yoktur.
- **Universal kural →** Kalıcı veri şeması sürümlenmeli; desteklenen sürümler açıkça dönüştürülmeli, bilinmeyen sürüm sessizce kabul edilmemelidir.
- **Gereken kanıt →** Şema sürüm tablosu, eski-yeni örneklerle migration testleri, geri dönüş planı ve dönüşüm sonrası operatör doğrulaması.
- **Yanlış genelleme sınırı →** Hiç kalıcı veri taşımayan veya her yüklemede doğrulanmış başlangıç seti kullanan sistemde migration katmanı gerekmeyebilir.

## 11. Birimler ve ölçek sözleşmesi

- **Sorun →** Aynı değer bir modülde mühendislik birimi, diğerinde ham sayı veya farklı zaman tabanı olarak yorumlanır.
- **Kök neden →** Değişken adı, veri tipi ve sabitler birim bilgisini taşımıyor; dönüşüm sınırı belirsizdir.
- **Universal kural →** Her sayısal arayüz için fiziksel birim, ölçek, geçerli aralık ve dönüşüm sahibi tanımlanmalı; birimsiz magic number kullanılmamalıdır.
- **Gereken kanıt →** Değişken sözlüğü, sınır değer testleri, bağımsız elle hesap ve I/O zincirinin uçtan uca ölçek kontrolü.
- **Yanlış genelleme sınırı →** Boyutsuz oranlar ve sayımlar doğal olarak birimsiz olabilir; yine de anlamı ve geçerli aralığı belgelenir.

## 12. Fixed-point, yuvarlama ve overflow

- **Sorun →** Taşma, işaret değişimi veya tamsayı bölme nedeniyle nadir ama büyük kontrol hataları oluşur.
- **Kök neden →** Ara işlemlerin veri tipi, işlem sırası, ölçek çarpanı ve yuvarlama politikası yalnız son değişken tipine bakılarak varsayılır.
- **Universal kural →** En kötü durum ara değerleri hesaplanmalı; widening, saturation, rounding ve sıfıra bölme davranışı açıkça seçilmelidir.
- **Gereken kanıt →** Min/max ve işaretli sınır hesabı, limit çevresi testleri, bağımsız referans hesap ve hedef runtime’ın tip davranışını doğrulayan IDE/bench sonucu.
- **Yanlış genelleme sınırı →** Daha geniş tip kullanmak tek başına doğruluk sağlamaz; çözünürlük, tarama maliyeti ve hedef desteği birlikte değerlendirilir.

## 13. HMI command, state ve ack ayrımı

- **Sorun →** Ekranın yazdığı bit hem istek hem gerçek makine durumu gibi kullanılır; ekran ile proses birbirini yanlış doğrular.
- **Kök neden →** Kullanıcı niyeti, kontrol kararının kabulü ve fiziksel sonucun gözlemi tek alanda birleştirilmiştir.
- **Universal kural →** Command, accepted/acknowledged, active state, completion ve fault anlamları ayrılmalı; HMI gerçek state’i doğrudan yazmamalıdır.
- **Gereken kanıt →** Sinyal yönü ve sahiplik tablosu, reddedilen komut testi, bağlantı kaybı/tekrar bağlanma testi ve fiziksel feedback karşılaştırması.
- **Yanlış genelleme sınırı →** Salt görüntüleme alanları veya yerel momentary input’lar bütün aşamaları gerektirmeyebilir; yazma yetkisi ve sonuç belirsizliği varsa ayrım zorunludur.

## 14. Import encoding ve tam tablo bütünlüğü

- **Sorun →** İçe aktarma başarılı görünürken bazı satırlar, karakterler, yorumlar veya alanlar kaybolur; kısmi tablo geçerli kabul edilir.
- **Kök neden →** Encoding, delimiter, locale, satır sonu ve beklenen kayıt sayısı sözleşmeye bağlanmamıştır.
- **Universal kural →** Import/export bir metin kopyası değil veri sözleşmesidir; encoding ve kolonlar sabitlenmeli, tam kayıt sayısı ve benzersiz anahtarlar doğrulanmalıdır.
- **Gereken kanıt →** Kaynak ve import sonrası satır/kolon sayıları, anahtar kümesi karşılaştırması, özel karakter örnekleri, hata raporu ve insanın IDE’de tam tablo kontrolü.
- **Yanlış genelleme sınırı →** Görsel eşitlik veri eşitliğini garanti etmez; tersine farklı biçimlenmiş iki tablo semantik olarak eşit olabilir. Karşılaştırma anlamlı alanlar üzerinden yapılır.

## 15. Cerrahi değişiklik ve baseline hash

- **Sorun →** Küçük bir düzeltme beklenmedik biçimde ilgisiz mantığı, formatı veya IDE metadata’sını değiştirir.
- **Kök neden →** Başlangıç durumu sabitlenmeden geniş kapsamlı yeniden üretim ya da toplu düzenleme yapılır.
- **Universal kural →** Önce baseline kimliği alınmalı; değişiklik en küçük sahiplik alanında uygulanmalı ve beklenen diff önceden tarif edilmelidir.
- **Gereken kanıt →** Başlangıç hash’i, sınırlı diff, değişen/değişmeyen öğe listesi, hedefli regresyon ve geri dönüş için doğrulanmış önceki artifact.
- **Yanlış genelleme sınırı →** Bilinçli migration veya büyük refactor geniş diff üretebilir; bu durumda kapsam ADR ile açıkça onaylanır ve parçalı doğrulanır.

## 16. Simülasyon saha kanıtı değildir

- **Sorun →** Simülasyonda çalışan mantık; gerçek zamanlama, elektriksel polarite, mekanik gecikme veya cihaz davranışı nedeniyle sahada başarısız olur.
- **Kök neden →** Modelin sınırları kaydedilmez ve simülasyon sonucu commissioning onayı gibi sunulur.
- **Universal kural →** Simülasyon mantık hipotezini sınar; gerçek I/O, zamanlama ve tehlike davranışı için IDE, bench ve saha kanıtı ayrı tutulmalıdır.
- **Gereken kanıt →** Simüle edilen/edilmeyen özellik listesi, model varsayımları, bench testleri, FAT/SAT kayıtları ve sapmaların kapatılması.
- **Yanlış genelleme sınırı →** Simülasyon değersiz değildir; deterministik state ve fault senaryolarında güçlü erken kanıt sağlar, fakat fiziksel katmanın yerine geçmez.

## 17. Bağımsız doğrulama

- **Sorun →** Programı yazan kişi veya ajan kendi varsayımını testte de tekrarlar; aynı kör nokta hem tasarımda hem onayda kalır.
- **Kök neden →** Review, çözümün açıklamasını yeniden okumaya indirgenir; karşı örnek üretilmez.
- **Universal kural →** Kritik davranış, implementasyondan bağımsız bir doğrulayıcı tarafından gereksinim ve kanıt üzerinden incelenmelidir.
- **Gereken kanıt →** Ayrı review kaydı, negatif/fault senaryoları, bulgu-kapanış izi ve doğrulayıcının kullandığı bağımsız beklenen sonuçlar.
- **Yanlış genelleme sınırı →** Yazım düzeltmesi gibi davranışsız değişiklikler tam bağımsız kurul gerektirmez; review yoğunluğu riskle orantılıdır.

## 18. Toplu işlemden önce temsilî örnek

- **Sorun →** Hatalı bir dönüşüm, şablon veya varsayım yüzlerce öğeye yayıldıktan sonra fark edilir.
- **Kök neden →** Batch işlemin mekanik başarısı semantik doğruluk sanılır.
- **Universal kural →** Önce normal, sınır ve istisna durumlarını temsil eden küçük örnek küme uçtan uca doğrulanmalı; sonra aynı sözleşmeyle batch çalıştırılmalıdır.
- **Gereken kanıt →** Örnek seçme gerekçesi, öncesi/sonrası karşılaştırma, istisna listesi, batch özeti ve rastgele son örnekleme.
- **Yanlış genelleme sınırı →** Temsilî örnek bütün kombinasyonları kanıtlamaz; yüksek riskte otomatik tam-küme invariants ve ek örnekleme gerekir.

## 19. Dirty tree ve “final” klasör yanılsaması

- **Sorun →** Aktif kaynak, geçici çıktı, yedek ve “final” adlı kopyalar birlikte durur; hangisinin kanonik olduğu belirsizleşir.
- **Kök neden →** Dosya adı veya klasör etiketi sürüm ve provenance yerine kullanılır; yerel değişiklikler kaydedilmeden yeni işlem başlatılır.
- **Universal kural →** Çalışmaya başlamadan repository state envanterlenmeli; kaynak, generated output, backup ve teslim artifact’ı açıkça ayrılmalı, “final” adı kanıt sayılmamalıdır.
- **Gereken kanıt →** VCS durumu, dosya hash’leri, üretim manifesti, artifact provenance’ı ve işlem sonrası yalnız beklenen değişikliklerin kaldığını gösteren kontrol.
- **Yanlış genelleme sınırı →** Dirty tree her zaman hata değildir; kullanıcıya ait devam eden işler korunarak görev kapsamı izole edilebiliyorsa çalışma güvenle sürdürülebilir.

## 20. Yorum ile gerçek davranış ayrımı

- **Sorun →** Yorum güvenli bir davranış tarif ederken yürütülen mantık farklıdır; inceleme yoruma inanır.
- **Kök neden →** Yorum, executable davranışın kanıtı veya gereksinimin yerine geçen otorite olarak kullanılır.
- **Universal kural →** Yorum “neden”, birim, polarite ve niyeti açıklar; gerçek davranış kod, konfigürasyon ve test üzerinden doğrulanır.
- **Gereken kanıt →** Yorum-kod tutarlılık incelemesi, executable path analizi, ilgili test ve eski yorumları yakalayan review.
- **Yanlış genelleme sınırı →** Yorumlar gereksiz değildir; özellikle fiziksel anlam ve karar gerekçesi için değerlidir, fakat kendi başına çalışma kanıtı değildir.

## 21. Evidence ladder ve doğru iddia seviyesi

- **Sorun →** “İncelendi”, “derlendi”, “test edildi” ve “sahada çalışıyor” ifadeleri aynı kesinlikte kullanılır.
- **Kök neden →** Kanıt türü ile sonuç iddiası arasında açık bir seviye sistemi yoktur.
- **Universal kural →** İddia, ulaşılmış en yüksek kanıt seviyesini aşmamalıdır: metin inceleme → statik kontrol → simülasyon → insan IDE derlemesi → bench/FAT → kontrollü saha doğrulaması.
- **Gereken kanıt →** Her sonuç için tarihli artifact, uygulayan kişi/araç, kapsam, sonuç, bilinen sınırlamalar ve bir üst seviyede hâlâ doğrulanacak maddeler.
- **Yanlış genelleme sınırı →** Basamaklar her zaman doğrusal değildir; bazı projelerde simülasyon mümkün olmayabilir. Atlanan basamak ve doğurduğu belirsizlik açıkça kaydedilir.

## 22. Rollback, revert ve öğrenmenin korunması

- **Sorun →** Bir değişiklik geri alındığında neden başarısız olduğu kaybolur; ekip daha sonra aynı yolu tekrar dener.
- **Kök neden →** Revert yalnız kod hareketi olarak görülür, hipotez ve kanıt zinciri saklanmaz.
- **Universal kural →** Her geri dönüş; başlangıç sürümü, tetikleyen bulgu, başarısız varsayım, geri alınan kapsam ve gelecekteki koruma testiyle birlikte kaizen kaydı üretmelidir.
- **Gereken kanıt →** Önce/sonra hash’leri, revert diff’i, hata yeniden üretimi, geri dönüş sonrası regresyon ve kalıcı ders bağlantısı.
- **Yanlış genelleme sınırı →** Revert edilen her fikir evrensel olarak yanlıştır denemez; ders, başarısız olduğu bağlam ve önkoşullarla sınırlanmalıdır.

## 23. Task sırası ve zaman tabanı sözleşmesi

- **Sorun →** Aynı mantık farklı task sırası veya çevrim süresinde farklı sonuç verir; test bunu tesadüfen gizler.
- **Kök neden →** Okuma, karar, yazma ve haberleşme sırası ile timer zaman tabanı mimarinin görünmez parçası olarak kalır.
- **Universal kural →** Task sahipliği, öncelik, periyot ve modüller arası veri tazeliği açık bir arayüz sözleşmesi olmalıdır.
- **Gereken kanıt →** Task haritası, worst-case gecikme hesabı, bir scan eski veri senaryosu, jitter/uzun scan testi ve hedef runtime ölçümü.
- **Yanlış genelleme sınırı →** Tek task’lı küçük programlarda ayrı zamanlama mimarisi gerekmeyebilir; yine de mantığın varsaydığı scan davranışı belirtilir.

## 24. LD/ST seçimi ölçü ve okunabilirlikle yapılır

- **Sorun →** Dil tercihi alışkanlık veya sloganla belirlenir; sonuç ya gereksiz step maliyeti ya da bakımı zor mantık olur.
- **Kök neden →** Donanım kaynağı, ekip yetkinliği, arıza bulma yöntemi ve mantık türü birlikte değerlendirilmez.
- **Universal kural →** Ayrık interlock ve kısa sekanslarda Ladder; matematik, veri dönüşümü ve koleksiyon işlemlerinde ST adaydır. Step farkı iddia ediliyorsa hedef IDE’de ölçülmeli; ölçüm yoksa sayı uydurulmamalıdır.
- **Gereken kanıt →** Dil karar kaydı, küçük temsilî mikrotaslak, insan IDE step/bellek ölçümü, bakım okunabilirliği değerlendirmesi ve tek-yazar kontrolü.
- **Yanlış genelleme sınırı →** Hiçbir mantık türü bir dile zorunlu olarak ait değildir; kullanıcı gereksinimi, hedef desteği ve ölçülmüş kısıtlar kararı değiştirebilir.

## 25. Envanter ile tam okuma aynı değildir

- **Sorun →** Binlerce dosya listelenip hashlenir; ardından içeriklerin tümü anlaşılmış gibi karar verilir.
- **Kök neden →** Dosyanın varlığını görmek, metni bütünüyle okumak, ikiliyi envanterlemek ve türetilmiş özeti incelemek tek kanıt seviyesi sayılır.
- **Universal kural →** Kaynak başına `TAM_OKUNDU`, `KISMİ`, `ENVANTER_HASH`, `TÜRETİLMİŞ` veya `ERİŞİLEMEDİ` durumu tutulmalı; ajan yalnız ulaştığı seviyeyi iddia etmelidir.
- **Gereken kanıt →** Okuma manifesti, hash ile kopya tekilleştirme, truncation bilgisi, seçilen satır/bölüm kapsamı ve türetilmiş artifact provenance'ı.
- **Yanlış genelleme sınırı →** Her görev bütün repoyu semantik olarak okumayı gerektirmez; görevle ilgili kaynakların seçim gerekçesi ve kapsamı görünür olmalıdır.

## 26. Kanonik çekirdek ve seçici rol yükleme

- **Sorun →** Aynı kural global prompt, proje promptu ve rol kartlarında kopyalanır; sürümler zamanla çelişir ve bağlam gereksiz büyür.
- **Kök neden →** Yönlendirme ile kural sahipliği ayrılmamış; her ajan her bilgiyi yüklemeye zorlanmıştır.
- **Universal kural →** Tek kanonik çekirdek politika kullanılmalı; giriş dosyası ve rol kartları kuralı yeniden yazmak yerine çekirdeğe bağlanıp yalnız görev farkını taşımalıdır.
- **Gereken kanıt →** Kural-sahip dosya matrisi, tekrar taraması, rol bazlı okuma rotası ve çelişkide hangi katmanın kazandığını gösteren öncelik sözleşmesi.
- **Yanlış genelleme sınırı →** Hedefe özgü kritik gerçekler çekirdeğe gömülmez; Target Profile ve görev zarfı ayrı, sürümlü katmanlardır.

## 27. Compaction ve handoff hafıza değildir

- **Sorun →** Uzun oturum özetlenince sahiplik, baseline veya açık risk kaybolur; ajan eski hafızayla mutasyona devam eder.
- **Kök neden →** Sohbet özeti kanonik görev state'i ve güncel politika kimliği yerine kullanılır.
- **Universal kural →** Compaction, yeni ajan veya handoff sonrasında control plane ve tek-yazarlı checkpoint yeniden okunmalı; policy epoch, baseline, exact scope ve sıradaki tek adım uyuşmadan edit yapılmamalıdır.
- **Gereken kanıt →** Sıralı checkpoint, `RESUME_ACK`, control-plane hash'i, scope/owner listesi ve uyuşmazlıkta durma kaydı.
- **Yanlış genelleme sınırı →** ACK gerçek okuma veya runtime enforcement kanıtı değildir; orkestratör ve araç kontrolü hâlâ gerekir.

## 28. 10. Adam iki geçişli ve bağımsızdır

- **Sorun →** Karşı inceleme ana çözümün dilini tekrarlar veya sırf rol gereği yapay itiraz üretir.
- **Kök neden →** Doğrulayıcı önce bağımsız beklenen davranışı kurmadan implementer açıklamasına maruz kalır; verdict sözleşmesi yoktur.
- **Universal kural →** Riskliyse 10. Adam önce ana çözümü görmeden güçlü başarısızlık senaryosu çıkarır, sonra çözümü görüp karşılaştırmalı ikinci geçiş yapar. Maddi çözüm değişirse inceleme yenilenir.
- **Gereken kanıt →** Ayrı iki artifact, aynı baseline/hash, `KORU/DARALT/BÖL/GERİ ÇEK/VERİ YETERSİZ` verdict'i ve kapanan/açık risk izi.
- **Yanlış genelleme sınırı →** Davranışsız küçük düzenleme tam kurul gerektirmez; gerekçeli uzlaşma başarısız 10. Adam değildir.

## 29. Framework ile tüketici proje arasında mutasyon sınırı

- **Sorun →** Genel ajan sistemi iyileştirilirken kaynak PLC veya başka bir tüketici repo yanlışlıkla değiştirilir.
- **Kök neden →** Okuma kapsamı, yazma kapsamı ve commit yetkisi aynı “projeye erişim” kavramında birleştirilir.
- **Universal kural →** Her görev zarfı exact read/write scope, forbidden scope, owner ve commit yetkisini ayrı yazmalı; framework değişikliği tüketici projeye otomatik yayılmamalıdır.
- **Gereken kanıt →** Task envelope, path allowlist, temiz başlangıç index'i, staged diff incelemesi ve commit sonrası değişen yol doğrulaması.
- **Yanlış genelleme sınırı →** Kullanıcı açıkça migration isterse birden çok repo değişebilir; yine de her repo için ayrı baseline, test ve atomik commit gerekir.

## 30. Modül bağımlılığı yönlü ve döngüsüzdür

- **Sorun →** Modüller küçük görünür fakat birbirlerinin iç state'ine geri yazar; gerçek davranış gizli bir döngüye dönüşür.
- **Kök neden →** Tek sorumluluk yalnız dosya boyutuyla ölçülür, bağımlılık yönü ve feedback sahipliği sözleşmeye alınmaz.
- **Universal kural →** Komut akışı yönlü olmalı; feedback yalnız adı, sahibi ve zaman tabanı belirli status arayüzünden dönmeli. Paylaşılan mutable-state döngüsü reddedilmelidir.
- **Gereken kanıt →** Modül grafiği, izin verilen bağımlılık matrisi, cycle incelemesi, task sırası ve bir scan eski feedback senaryosu.
- **Yanlış genelleme sınırı →** Fiziksel kontrol doğal feedback içerir; yasak olan feedback değil, sahipliği ve zamanı belirsiz yazma döngüsüdür.

## Kullanım notu

Bu kartlar hazır hüküm değil, inceleme sorularıdır. Yeni bir projede yalnız ilgili kartlar seçilir; risk yükseldikçe kanıt seviyesi artırılır. Kanıt bulunmadığında doğru sonuç “doğrulanmadı”dır; varsayım, ölçüm veya insan onayı gibi sunulmaz.
