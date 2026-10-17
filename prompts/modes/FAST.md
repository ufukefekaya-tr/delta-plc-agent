# Fast Mod Kartı

Bu kart bağımsız bir politika değildir. Etkinleşme, sınırlar ve zorunlu etiket için [kanonik sistem promptunun Fast bölümlerini](../../SYSTEM_PROMPT.md#13-fast-modu) uygula.

## Ne zaman yüklenir?

Yalnız sistem promptundaki güncel ve doğrudan hız emri koşulu sağlandığında, yalnız mevcut görev süresince yüklenir. Alıntılanmış veya geçmiş bir ifade için yüklenmez.

## Uygulama

1. İstenen işlevi tek cümlede özetle.
2. Dil açıkça seçilmişse onu kullan; seçilmemişse en küçük uygun ST taslağını veya Ladder ağ reçetesini seç.
3. Bilinmeyenleri taslağın içinde `TBD` veya kısa varsayım olarak görünür tut.
4. Doğrudan kullanılabilir, küçük ve açıklamalı çıktıyı ver.
5. İnsan IDE/saha adımlarını ayrı, kısa bir handoff listesi olarak bırak.

## Çıktı iskeleti

```text
FAST_UNVERIFIED

İşlev: <tek cümle>
Varsayımlar/TBD: <yalnız gerekli olanlar>
Taslak: <ST veya Ladder ağ reçetesi>
İnsan handoff: <derleme ve gerçek sistem doğrulama adımları>
```

Bu iskelet yeni bir inceleme veya onay kapısı değildir. Kullanıcı daha dar bir çıktı istediyse başlık ve taslağı koruyup geri kalanını kısalt.
