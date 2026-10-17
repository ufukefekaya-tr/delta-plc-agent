# DVP seri MCP — FAST hedefleri

13 Ekim 2026. Başlangıç 10:14:19 UTC, teslim sınırı 11:14:19 UTC. Güncel yol COM6 HMI USB CDC → PLC RS485; COM5 kablosu kullanıcı tarafından geri çekildi. Makinece okunabilir kayıt: [dvpserial-fast.json](dvpserial-fast.json).

| Goal | Workflow | Sonuç | Kabul / açık iş |
|---|---|---|---|
| G-DVP-0 MCP hazır | W1 | PASS_SOFTWARE | Mevcut 11 araçlı MCP, ayrı delta.dvpserial adaptörü, exact SS2 seçimi, port envanteri. |
| G-DVP-1 gerçek online | W5 | PASS_LIVE_READ | Kullanıcı portu serbest bıraktıktan sonra 10:41:57 UTC'de HMI BRDY ve iki geçerli PLC durum okuması alındı. PLC STOP; D100–D111 ham değerleri 0, firmware 0x8840, kapasite 7920. Exact CPU modeli bağımsız doğrulanmadı. |
| G-DVP-2 program yükleme | W5 | PROGRAM_TRANSFER_UNAVAILABLE | İlk 10 dakika araştırmasında doğrulanmış SS2 aktarım sözleşmesi bulunamadı. Yedek/yükleme/geri karşılaştırma NOT_RUN; program değişmedi. |
| G-DVP-3 bağlantı hataları | W6 | PASS_SOFTWARE | Eksik/meşgul port, timeout, mutlak cevap süresi, LRC/istasyon/fonksiyon/uzunluk, süresi dolan yetki, değişen SHA ve yarım istek yönetimi; test donanımı yazılım fixture'ıdır. |
| G-DVP-4 teslim | W7 | PASS_DELIVERY | Çalıştırılabilir istemciler, kalıcı workflow/işlem kanıtı, kullanım/fizibilite belgeleri, 44 seri/MCP ve 25 mevcut runtime testi; scoped yerel commit kanıtı. |
| G-DVP-5 program aktarımını geliştirme | W5 | ACTIVE / ayrı NORMAL hedef | [Güncel hedef](dvp-transfer.json): en son onaylı kaynak, şu anda V7; V5 geçersiz. Native aktarım/geri okuma geliştirilir, canlı kabul bu eski FAST kaydından türetilmez. |

Online okuma kabulü: geçerli Modbus yanıtı, M1000 RUN/STOP ve D100–D111'in iki ardışık okuması. Köprünün BRDY/RUN yanıtı bu kabulü sağlamaz. Firmware/kapasite exact model kanıtı değildir. İlk salt okunur kabulün ardından kullanıcı ayrıca RUN → oku → STOP istedi: 10:52:34–10:52:40 UTC'de sabit M1072 FC05 komutları ve taze M1000 doğrulamaları geçti; iki okumada sıcaklıklar 24.6–26.2°C, son durum STOP. PLC programı değiştirilmedi. Bu tek kontrol döngüsünün izni kapatıldı; sonraki oturumda kendiliğinden RUN uygulanmaz.

- [Gerçek MCP COM6 kanıtı](../docs/public/VALIDATION.md)
- [Başarılı CDC online kanıtı](../docs/public/VALIDATION.md)
- [Başarılı RUN/okuma/STOP kanıtı](../docs/public/VALIDATION.md)
- [Teslim ve çalıştırma](../docs/public/VALIDATION.md)
- [Adaptör kullanımı](../docs/adapters/dvp-serial.md)
- [Aktarım engeli](../docs/public/VALIDATION.md)
