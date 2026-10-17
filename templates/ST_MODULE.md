# ST Module Şablonu

## POU kontratı

- POU adı:
- Tek sorumluluk:
- Task/sıra:
- Sahibi olduğu state/output:
- Girişler:
- Çıkışlar:
- In-out/veri paylaşımı:
- Birimler/fixed-point:
- Kalıcı/geçici state:
- Scan/çağrı varsayımı:
- Stop/reset/power-return:
- Timeout/fault:

## Modülerlik değerlendirmesi

- Mantıksal satır tahmini:
- Karar iç içeliği:
- State sayısı:
- Tekrar eden mantık:
- Eşik aşımı varsa ADR:

## Kod taslağı

```iecst
(*
  Bu alan onaylı Control Contract'tan türetilir.
  Yorumlar neden, birim ve invariantları açıklar.
*)
```

## Negatif ve boundary kontrolleri

- Bölme sıfır:
- Overflow/cast:
- Min/max parametre:
- Stale done/fault:
- Sensor stuck/noisy:
- Stop/reset/power return:

## İnsan IDE doğrulaması

- [ ] Syntax/build
- [ ] Step/program belleği
- [ ] Task sırası
- [ ] Import/symbol uyumu
- [ ] Monitor trace

