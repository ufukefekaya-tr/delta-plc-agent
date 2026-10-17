# Control Contract

## Modül grafiği

| Modül | Tek sorumluluk | Okudukları | Yazdıkları | Task/sıra | Sahibi |
|---|---|---|---|---|---|

## Bağımlılık yönü

| Kaynak modül | Hedef arayüz | İzin nedeni | Ters bağımlılık yasak mı | Feedback/status yolu |
|---|---|---|---|---|

- [ ] Modül grafiğinde gizli veya doğrudan mutable-state döngüsü yok.
- [ ] Feedback bir sahipli status arayüzü üzerinden dönüyor.

## Fiziksel output sahipliği

| Output kimliği | Fiziksel işlev | Tek yazar | İstek kaynakları | Öncelik | Safe-state |
|---|---|---|---|---|---|

## Kritik state sahipliği

| State/parametre | Tek yazar | Okuyucular | Retentive | Reset/migration sahibi |
|---|---|---|---|---|

## Task sırası ve veri bağımlılığı

```text
INPUT SNAPSHOT
→ karar/sekans
→ istek arbitrajı
→ fiziksel output owner
→ feedback/fault
→ HMI/diagnostic projection
```

Gerçek sıra ve gerekçesi:

| Sıra | POU/task | Önceki veriye bağımlılık | Yanlış sıranın etkisi |
|---:|---|---|---|

## Asenkron handshake'ler

| İşlev | Request | Accepted/rejected | Active/busy | Done | Fault | Abort/ack |
|---|---|---|---|---|---|---|

## Durum makinesi

| State | Amaç | Giriş koşulu | Çıkış koşulu | Timeout | Abort | Yazılan istek/state |
|---|---|---|---|---|---|---|

## Stop/reset/power matrisi

| Olay | Fiziksel çıkış | Aktif komut | State | Sayaç/metraj | Param snapshot | Restart şartı |
|---|---|---|---|---|---|---|
| Normal stop |  |  |  |  |  |  |
| Fault stop |  |  |  |  |  |  |
| Emergency stop |  |  |  |  |  |  |
| Reset/ack |  |  |  |  |  |  |
| Mode change |  |  |  |  |  |  |
| Power return |  |  |  |  |  |  |

## Invariantlar

- [ ] Her fiziksel output tek yazarlı.
- [ ] Her wait state timeout ve abort taşıyor.
- [ ] Command/state/ack ayrık.
- [ ] Parametre snapshot zamanı açık.
- [ ] Task sırası belgeli.
- [ ] Otomatik restart açık sözleşmeye bağlı.
- [ ] Bağımlılık yönü açık ve döngüsüz.
