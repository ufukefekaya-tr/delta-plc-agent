# I/O ve Değişken Sözleşmesi

## Fiziksel I/O

| Kimlik | Açıklayıcı ad | Tür | Fiziksel işlev | Aktif seviye/polarite | Normal durum | Safe-state | Birim/ölçek | Owner | Target binding | Kanıt |
|---|---|---|---|---|---|---|---|---|---|---|

## İç değişkenler

| Sembol | Veri tipi | Açıklama | Birim/ölçek | Başlangıç | Retentive | Tek yazar | Okuyucular | HMI rolü |
|---|---|---|---|---|---|---|---|---|

## Command/state/ack ayrımı

| İşlev | Operator command | Accepted/rejected | Internal state | Result/ack | Fault/detail |
|---|---|---|---|---|---|

## Parametreler

| Parametre | Tip | Birim | Min | Max | Fabrika varsayımı | Snapshot zamanı | Persistence | Operator/service |
|---|---|---|---|---|---|---|---|---|

## Kontrol listesi

- [ ] Her Ladder input/değişken/output açıklanmış.
- [ ] Aynı fiziksel binding iki sinyale verilmemiş.
- [ ] Word/DINT overlap kontrol edilmiş.
- [ ] Birim ve fixed-point ölçek açık.
- [ ] Owner/yazar listesi tekil.
- [ ] HMI writable alanlar sınırlı ve tanımlı.

