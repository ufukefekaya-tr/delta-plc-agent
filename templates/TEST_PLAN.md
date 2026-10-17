# Test Planı

## Kapsam ve baseline

- Requirement/KZ kimliği:
- Kaynak hashleri:
- Hedef profil revizyonu:
- Test edilecek davranış:
- Test edilmeyecek davranış:

## Ajan testleri

| Kimlik | Tür | Ön koşul/girdi | Beklenen state/output/trace | Sonuç | Kanıt |
|---|---|---|---|---|---|

Zorunlu sınıflar:

- Static yapı ve tek-yazar
- Boundary/min/max
- Fixed-point/overflow/division
- Scan/task order
- Sensor bounce/stuck/delay
- Timeout/abort
- Normal stop/fault stop/emergency stop
- Reset/mode change/power return
- Communication loss/recovery
- Regression/golden trace

## Test sonucu sınıflandırması

- `PRODUCT_DEFECT`
- `TEST_DRIFT`
- `CONTRACT_CHANGE_PENDING`
- `PASS`
- `SKIP`
- `ERROR`

`SKIP` ve `ERROR`, PASS sayılmaz.

## İnsan IDE/bench/FAT planı

| Sıra | İnsan faaliyeti | Risk azaltma | Beklenen kanıt | Sonuç/imza |
|---:|---|---|---|---|
| 1 | Kaynak/import karşılaştırması |  |  |  |
| 2 | IDE build/compile |  |  |  |
| 3 | Enerjisiz I/O gözlemi |  |  |  |
| 4 | Düşük güç/hız |  |  |  |
| 5 | Fault/stop testleri |  |  |  |
| 6 | Kademeli FAT |  |  |  |

## Rollback

- Yedek/baseline:
- Geri dönüş adımları:
- Geri dönüş kabul ölçütü:

