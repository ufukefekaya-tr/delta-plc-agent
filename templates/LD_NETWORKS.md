# Ladder Network Reçetesi

Bu belge grafik Ladder'ın vendor IDE'de geçerli kapsamda kurulması için açıklamalı ağ sözleşmesidir. Çizimin kendisi veya derleme kanıtı değildir.

## POU kontratı

- POU adı:
- Tek sorumluluk:
- Task/sıra:
- Fiziksel output sahipliği:
- Stop/reset/power-return özeti:
- İlgili `IO_AND_VARIABLES.md` revizyonu:

---

## Network N001 — Başlık

### Amaç

Fiziksel/mantıksal davranış ve bu ağın neden var olduğu.

### Girişler ve ara değişkenler

| Sembol | Aktif anlamı | Polarite/birim | Owner | Neden gerekli |
|---|---|---|---|---|

### Mantık reçetesi

1. Kontak/koşul sırası:
2. Paralel/seri dallar:
3. Edge/timer/FB davranışı:
4. Interlock ve öncelik:

### Tek ana write

- Yazılan output/state:
- Başka yazar var mı: `HAYIR`
- Safe-state:

### Timeout, fault ve reset

- Timeout:
- Fault üretimi:
- Abort:
- Ack/reset:
- Power-return:

### Neden yorumu

Sözdizimini tekrar etmeyen, tasarım kararını açıklayan kısa yorum.

### İnsan IDE doğrulaması

- [ ] Sembol tipleri ve binding
- [ ] Network sırası
- [ ] Step/bellek sonucu
- [ ] Compile uyarıları
- [ ] Online trace/FAT maddesi
