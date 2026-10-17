# Mühendislik Kuralları

Bu belge, `SYSTEM_PROMPT.md` politikasının PLC tasarımına nasıl uygulanacağını açıklar. Hedefe özgü gerçekler burada değil, proje `TARGET_PROFILE.md` dosyasında tutulur.

## 1. Önce sözleşme, sonra kod

Koddan önce aşağıdakiler açık olmalıdır:

- Fiziksel proses ve başarı ölçütü
- Modlar ve mod geçişleri
- Her girişin aktif seviyesi, normal durumu ve arıza yorumu
- Her çıkışın tek sahibi ve enerjisiz/güvenli beklentisi
- Normal stop, kontrollü fault stop, emergency stop, reset ve power return davranışları
- Parametrelerin birimi, sınırı, kalıcılığı ve değişiklik zamanı
- Task sırası, scan varsayımları ve asenkron komut handshake'i

Bir alan bilinmiyorsa tahmin yerine `TBD` kullanılır. Normal modda kritik `TBD` kodlamayı durdurur; Fast modda taslak içinde görünür bırakılabilir.

## 2. LD/ST seçimi

Dil seçimi ideolojik değil, hedef ve bakım bağlamına bağlıdır.

1. Kullanıcı açıkça ST ister ve hedef desteklerse ST seçilir.
2. Güvenilir IDE ölçümü eşdeğer ST çözümünün daha fazla step veya program belleği kullandığını gösterirse ve ST zorunlu değilse Ladder önerilir.
3. Ölçüm yoksa step sayısı uydurulmaz.
4. Ayrık I/O, interlock, coil, basit timer ve kısa sekanslar Ladder için güçlü adaydır.
5. Matematik, dizi, ölçekleme, veri dönüşümü ve yoğun hesap ST için güçlü adaydır.
6. Bellek sınırı kritikse iki tam uygulama yazılmaz; küçük bir LD/ST mikrotaslağı geçerli kapsamda gerçek IDE ile ölçülür.
7. Hibrit kullanımda diller aynı fiziksel çıkışı yazmaz; `request/status` arayüzüyle ayrılır.

Karar `templates/LANGUAGE_DECISION.md` ile kaydedilir. Bilinmeyen compiler davranışı genel IEC davranışıymış gibi sunulmaz.

## 3. Modül ve sahiplik

- Bir modülün tek fiziksel veya mantıksal sorumluluğu vardır.
- Her fiziksel çıkışın tek yazarı vardır.
- Kritik state, alarm latch'i, sayaç ve parametre migration alanının tek sahibi vardır.
- Sekans modülü fiziksel çıkışı doğrudan yazmak yerine bir istek üretir; çıkış sahibi isteği kabul veya reddeder.
- Ham HMI command, iç state, acknowledge ve result ayrı sembollerdir.
- Task sırası ve modüller arası okuma/yazma ilişkisi `CONTROL_CONTRACT.md` içinde görünürdür.
- Paralel ajanlar aynı dosya, çıkış, state veya sinyal kataloğunu değiştirmez.
- Bağımlılık yönü üst seviye sekanslardan sözleşmeli servis/FB arayüzlerine doğrudur; alt modül üst modülün iç state'ine geri yazmaz.
- Modül grafiğinde döngü varsayılan olarak yasaktır. Zorunlu feedback, paylaşılan mutable state yerine adı, zaman tabanı ve sahibi belirli bir status/feedback arayüzüyle kurulur.

Önerilen asenkron arayüz:

```text
request
→ accepted | rejected
→ active/busy
→ done | fault | aborted
```

Yeni request'in eski `done`, `fault` veya `busy` durumunu tüketmesi açık bir reset/ack sözleşmesine bağlıdır.

## 4. Normal modda kesin hata sayılanlar

- Aynı fiziksel çıkışın veya kritik state'in birden fazla yazarı
- Açıklamasız Ladder input, değişken veya network
- Timeout ve abort yolu olmayan bekleme
- Sözleşmesiz otomatik restart
- Birimsiz sabit veya örtük ölçek
- Belgesiz task sırası
- Command/state/ack için aynı bitin kullanılması
- Güvenilir baseline olmadan toplu dosya veya sembol tablosu değiştirme
- Aktifmiş gibi duran obsolete doküman

Fast mod bu değerlendirme kapılarını zorunlu tutmaz; bulunan riskler taslak notunda görünür kalır.

## 5. Bölme veya ADR gerektiren eşikler

Eşik aşımı otomatik olarak yanlış kod demek değildir; modülü bölmek veya `ADR.md` ile gerekçe yazmak gerekir.

| Eşik | İnceleme nedeni |
|---|---|
| 250 mantıksal ST satırı | Sorumluluk ve review yüzeyi büyür |
| 20 Ladder network'ü | Tek POU içinde birden fazla iş birikmiş olabilir |
| Üç seviyeden fazla karar iç içeliği | Scan akışını ve arıza yolunu izlemek zorlaşır |
| Bir POU'da birden fazla bağımsız state machine | Sahiplik ve reset yolları karışır |
| 15'ten fazla state | Alt süreçlere ayırma ihtiyacı doğar |
| Aynı mantığın üç kez kopyası | Ortak FB/helper adayıdır |
| Bir network'te birden fazla ana write | Tek amaç ve tek yazar görünürlüğü kaybolur |

## 6. Ladder açıklama sözleşmesi

Her sembol için şu bilgiler zorunludur:

- Açıklayıcı mantıksal ad
- Sinyal türü ve veri tipi
- Girişse aktif seviye/polarite
- Birim ve ölçek
- Sahibi ve yazarları
- Normal başlangıç ve retentive durumu
- Güvenli beklenti
- HMI erişim rolü

Her network şunları açıklar:

- Kimlik ve kısa başlık
- Fiziksel/mantıksal amaç
- Girişlerin aktif anlamı
- Interlock ve öncelik
- Yazılan tek ana output/state
- Timeout, fault, reset ve power-return davranışı
- Sözdizimini değil, tasarım nedenini açıklayan yorum

## 7. ST açıklama sözleşmesi

Her ST POU başında şu kontrat bulunur:

- Amaç ve sahiplik
- Giriş/çıkış/in-out listesi
- Birimler ve fixed-point ölçekleri
- Kalıcı/geçici state
- Çağrı sırası ve scan varsayımı
- Stop/reset/power-return davranışı
- Hata ve timeout sözleşmesi

Yorumlar satırın Türkçesini tekrar etmez. Neden, sınır, birim, invariant veya beklenmeyen fakat bilinçli davranışı açıklar.

## 8. Sayısal ve zamansal kurallar

- Birim sembol adında veya tablo alanında görünür olmalıdır.
- Fixed-point çarpanının anlamı, ara işlem genişliği ve yuvarlama yönü yazılır.
- Bölme sıfır, signed/unsigned dönüşüm, overflow ve clamp sınırları negatif testlere girer.
- Timer değeri ile scan sayacı birbirine karıştırılmaz.
- Parametreler çevrim başında snapshot alınır; çevrim ortasında canlı değişiklik ancak açık gereksinimse kabul edilir.
- Retention ve migration hedef profile dayanır; başka PLC ailesinden taşınmaz.

## 9. Durma ve yeniden başlama

Şu olaylar ayrı sözleşmelerdir:

- Normal stop
- Kontrollü fault stop
- Emergency stop
- Operator reset
- Fault acknowledge
- Mode change
- Warm restart
- Cold/power restart

Her biri için state, metraj/sayaç, aktif komut, fiziksel çıkış, parametre snapshot ve tekrar başlatma koşulu ayrı tanımlanır. “Stop her şeyi sıfırlar” veya “kaldığı yerden devam eder” gibi toplu varsayımlar kullanılmaz.

## 10. Değişiklik disiplini

- Değişiklik öncesi baseline hash ve aktif kaynak kimliği alınır.
- En küçük davranış dilimi değiştirilir.
- Refactor ile davranış değişikliği aynı commit'e karıştırılmaz.
- Kaynak, symbol table, HMI/alarm sözleşmesi, test ve doküman aynı davranış için birlikte değerlendirilir.
- Eski dosyanın tamamı yeni kaynak üzerine kopyalanmaz; doğrulanmış fark cerrahi taşınır.
- Revert yeni bir kaizen kaydıdır; önceki kayıt silinmez.

Her maddi davranış veya kapsam değişikliğinden önce [Surgical Change](../templates/SURGICAL_CHANGE.md) kaydıyla beklenen diff, korunacak invariantlar, dokunulmayacak alanlar ve durma koşulları yazılır. Davranışsız küçük yazım/yorum düzeltmesinde aynı alanların kısa hali Task Envelope içinde tutulabilir; ayrı boş şablon üretmek gerekmez. Gerçek diff beklenen sınırı aşarsa geniş değişikliği sessizce kabul etme; dur, kapsamı yeniden kilitle veya ayrı ADR aç.
