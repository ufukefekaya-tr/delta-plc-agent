# İnsan IDE ve Saha Handoff

## Paket kimliği

- Proje/revizyon:
- Çalışma modu:
- Artifact durumu:
- Kaynak hashleri:
- Target profile revizyonu:
- KZ/ADR:

## Teslim edilenler

- [ ] ST taslağı veya Ladder ağ reçetesi
- [ ] I/O ve variable tablosu
- [ ] Control contract ve task sırası
- [ ] Test planı ve ajan sonuçları
- [ ] Monitor/trace listesi
- [ ] Rollback paketi

## Açık TBD ve varsayımlar

| ID | Konu | Etki | İnsan kararı/kanıtı |
|---|---|---|---|

## Atlanan ve geçen ajan kontrolleri

| Kontrol | PASS/FAIL/SKIP/ERROR/ATLANDI | Kanıt/not |
|---|---|---|

## İnsan IDE adımları

1. Mevcut proprietary proje ve PLC yedeğini al.
2. Kaynak/symbol/import encoding ve sürümünü doğrula.
3. Dar değişikliği kontrollü import/uygula.
4. Task sırası ve tek-yazar ağlarını gözle doğrula.
5. Full build/compile sonucunu kaydet.
6. Hata/uyarı listesini ve program belleği/step sonucunu ekle.
7. Download kararını yetkili insan verir.

## Enerjisiz/bench/FAT sırası

1. Enerjisiz continuity ve polarity.
2. PLC online olmadan output güvenli beklentisi.
3. Enerjisiz/düşük riskli input monitor.
4. Düşük güç/hız tek aktüatör.
5. Stop/reset/fault/emergency senaryoları.
6. Kademeli proses/FAT.

## İnsan kanıtı ve imza

| Seviye | Sonuç | Tarih | Yetkili kişi | Kanıt yolu/hash |
|---|---|---|---|---|
| IDE compiled |  |  |  |  |
| Bench verified |  |  |  |  |
| FAT accepted |  |  |  |  |

## Rollback

- Tetikleme koşulu:
- Yedek kimliği:
- Geri dönüş adımları:
- Geri dönüş doğrulaması:

