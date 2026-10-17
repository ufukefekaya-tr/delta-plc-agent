# Kaizen Protokolü

Kaizen, art arda kod eklemek değil; gözlemi kanıta, kök nedeni küçük değişikliğe ve saha sonucunu kalıcı derse dönüştürmektir.

## 1. Durum zinciri

```text
OBSERVED
→ HYPOTHESIS
→ STATIC_CHECKED
→ HUMAN_IDE_COMPILED
→ HUMAN_BENCH_VERIFIED
→ HUMAN_FAT_ACCEPTED
→ GENERALIZED
```

Her aşama ayrı kanıt ister. Bir commit, rapor veya ajan görüşü zinciri tek başına ilerletmez.

## 2. Kayıt kimliği

Her davranış değişikliği şu kimliği alır:

```text
KZ-YYYYMMDD-NNN-kisa-konu
```

Commit subject örneği:

```text
fix(sequence): preserve state during controlled stop [KZ-YYYYMMDD-001]
```

## 3. Zorunlu kayıt alanları

- Gözlem ve beklenen/gerçek davranış
- Aktif baseline ve hash
- Kanıt seviyesi
- Kök neden hipotezi
- Alternatif hipotezler ve 10. Adam görüşü
- Seçilen en küçük değişiklik
- Etkilenen modül, owner, task ve arayüzler
- Risk, safe-state ve containment
- Pozitif, negatif, boundary ve fault testleri
- Statik/IDE/bench/FAT sonuçları
- Atlanan doğrulamalar
- Rollback
- Sonuç: başarılı, başarısız, kısmi veya revert
- Genellenebilir ders ve uygulanabilirlik sınırı

## 4. Commit kuralları

- Başarıyla tamamlanan her mutasyon görevi `SCOPED_MANDATORY` yerel commit ile kapanır; ajan commit için kullanıcıdan ayrıca izin istemez. Salt-okunur görev commit üretmez.
- Ajan base/branch veya worktree kimliğini doğrular, yalnız atanmış yolları stage eder ve `git diff --cached` çıktısını bütünüyle okur.
- Bir commit tek, bağımsız geri alınabilir niyet taşır.
- Refactor ile davranış değişikliği aynı commit'e karıştırılmaz.
- Davranış ve onun regression testi birlikte commit edilir.
- İlgili kontroller commit öncesi çalıştırılır; başarısız kontrolde görev tamamlandı sayılmaz. Sonuç ve commit hash'i orkestratöre bildirilir.
- Ajan commit'i ayrık branch/worktree'de yapılır veya baseline'dan post-commit kontrole kadar tek sahibinin olduğu global commit lease kullanılır.
- Lease başında beklenen `HEAD`, boş index ve exact dosya allowlist'i doğrulanır; lease boyunca başka stage/commit veya test ağacı mutasyonu yapılmaz.
- Commit sonrasında parent, değişen yollar, temiz index ve test edilen commit ağacı doğrulanır.
- Yerel commit push yetkisi değildir; açık kullanıcı talimatı olmadan push, remote değişikliği veya history rewrite yapılmaz.
- Ajan başkasının değişikliğini stage etmez, amend/rebase ile ortak geçmişi yeniden yazmaz ve çatışmayı kapsam dışına taşarak çözmez.
- Import/symbol tablosu kısmi ekleme ile eski satırları sessizce ezmez; tam baseline ve encoding kaydedilir.
- Başarısız deneme silinmez; KZ kaydında sonuç ve neden korunur.
- Revert önceki kaydı yok etmez; yeni KZ kimliğiyle ilişkilendirilir.
- İnsan compile/FAT kanıtı ayrı `evidence:` commit'i olabilir.
- Açık kullanıcı yetkisi olmadan push, remote değişikliği veya history rewrite yapılmaz.

## 5. Universal derse terfi

Bir vaka ancak şu koşullarda `knowledge/CULTURE_TRANSFER.md` içine terfi eder:

- Proje ve vendor ayrıntılarından arındırılmıştır.
- Kuralın uygulanabilirlik sınırı yazılmıştır.
- Tek bir ajan iddiasından fazlasına dayanır.
- Kaynak, test veya insan kanıtıyla desteklenir.
- Başka hedefe kör kopyalanması engellenir.

Terfi formatı:

`Sorun → Kök neden → Universal kural → Gereken kanıt → Yanlış genelleme sınırı`

## 6. Cerrahi değişiklik akışı

1. Aktif kaynak ve baseline hashini doğrula.
2. Tek hipotez seç.
3. Etki/sahiplik haritasını çıkar.
4. En küçük davranış dilimini değiştir.
5. İlişkili symbol/HMI/alarm/test/doküman sözleşmesini kontrol et.
6. Negatif ve fault senaryosu çalıştır.
7. Değişiklik dışındaki davranışın korunmasını doğrula.
8. İnsan kapılarını `PENDING` bırak.
9. Sonucu ve rollback'i kaydet.

## 7. Öğrenme yoğunluğu ve yaşam döngüsü

- Envanterlemek, hash almak veya bir ajan özetini okumak `tam okundu` demek değildir; kayıt hangi kaynağın tam, kısmi, türetilmiş veya erişilemez olduğunu belirtir.
- Aynı ders üçüncü kez görülse bile otomatik olarak çekirdek politika yapılmaz. Önce ortak önkoşul, karşı örnek ve yanlış genelleme sınırı aranır.
- Güncel kullanıcı düzeltmesi, eski rapor veya ajan sentezinden üstündür; yine de fiziksel gerçek için kaynak/test/insan kanıtı gerekir.
- Davranışsız küçük düzeltmede geniş kurul kurulmaz. Riskli motion, safety, restart, sahiplik veya migration değişikliğinde 10. Adam izi korunur.
- Eski belge `ACTIVE`, `SUPERSEDED`, `ARCHIVE` veya `CLAIM` yaşam döngüsü taşır; aktifmiş gibi bırakılmaz.
