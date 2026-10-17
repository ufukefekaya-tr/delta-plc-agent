# KZ-20261017-001 — DVP teslimi ve gözetimli M/D yetkisi

- Durum: `STATIC_CHECKED`
- Tarih: 2026-10-17
- Baseline/hash: `f5f43bd69a5c43fb2ec47a205319f1ca87764a1f`
- Requirement/ADR: Onaylı G-DVP-FINAL; mevcut API, WPLSoft GUI üretim/derleme/yükleme, MCP-only watch.

## Gözlem

Saha ajanı enerjili gözetimli M/D için ayrı runtime kullanmıştı; ana repo yalnız izolasyon kabul ediyordu. Ana repo kurulum yardımcısı DVP işinde de AX kuruyordu. Eski V7/V10 belgeleri güncel sanılabiliyordu.

## Beklenen / gerçek

Doğru fiziksel beyanla mevcut profil komutları, DVP-only kurulum ve kanıtı ayrı GUI/MCP aşamaları. Eski saha kayıtları gerçek M/D kanıtı taşıyor fakat yeni paket veya başka revizyon kabulü değil.

## Kök neden hipotezi

Host yetki profili, ürün paketi ve makine revizyonunun birbirine karıştırılması; yerel deneyin main'e kontrollü alınmaması.

## 10. Adam karşı görüşü

- 10. Adam karşı görüşü: Attended boolean geniş yetkiye dönüşebilir; yeni flag RUN/STOP/program aktarımını açmamalı. Bir portun listelenmesi PLC kimliği değildir. Kurulum başarı metni vendor/native kabul yerine geçmemeli.

## En küçük değişiklik

Yalnız M/D strict gate'e açık attended+cutoff alternatifi. Mevcut context/source/lease/expiry ve kalıcı belirsizlik sınırı korunur. DVP install seçimi ve port açmayan prerequisite helper. WPLSoft GUI ile MCP watch arasında tek port sahibi ve ayrı kanıt tarif edilir; yeni compiler/transfer API eklenmez.

## Doğrulama

[Güncel test/kanıt](../docs/public/VALIDATION.md) ve [teslim](../docs/public/VALIDATION.md). Gözetimli izin negatifleri, legacy profil, geç yazma iptali, kurulum seçimi ve temiz wheel SDK roundtrip sınanır. Yerel seri aygıt yokluğu ve vendor UI erişim engeli canlı başarı sayılmaz.

## Rollback

Attended flag'leri false tutup eski isolated yolunu kullan; önceki paket sürümüne ayrı yerel revert ile dön. Bilinmeyen işlem journal/fence'lerini silme. Paket değişimi canlı yetkiyi yenilemez.

## Sonuç ve genellenebilirlik sınırı

Genel host gate proje algoritması taşımaz. Gözetimli çalışma gözetimsiz saha veya fiziksel FAT değildir. Kurulum ve GUI yolu yalnız doğrulanan host/araç koşullarında çalışır; eksik donanım yazılımla giderilemez.

- Yanlış genelleme sınırı: tarihsel attended runtime saha kanıtı yeni wheel, yeni CPU veya yeni kaynak revizyonu için canlı kabul değildir.

## Son inceleme düzeltmesi

İzleme yardımcısının her çalışmada yeni workspace oluşturması mevcut canlı revizyon kanıtını geçersiz bırakıyordu. Mevcut workspace açık kimlikle yeniden kullanılır; hedef/kaynak/revizyon eşleşmesi okumadan önce denetlenir. Doğrulanmamış örnek başarı sayılmaz. Altı odaklı yardımcı testi ve kurulu paketle gerçek MCP üzerinden portsuz workspace yeniden kullanımı doğrulandı. Bu testler canlı PLC kabulü değildir.
