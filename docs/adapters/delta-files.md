# Delta dosya adaptörleri

17 Ekim 2026: Bu belge yalnız dosya adaptörünün kapsamını anlatır. Güncel DVP seri/M/D/watch uygulaması [ayrı adaptördedir](dvp-controls.md); WPLSoft ajan akışı [runbook içindedir](../DVP_AGENT_RUNBOOK.md).

11 Ekim temelinden gelen `DeltaFileAdapter` gerçek dosya dönüşümleri sunar. Sonuçlar **FILE_TRANSFORM** kanıtıdır. Vendor derleme, simülatör, donanıma bağlanma veya canlı kabul anlamına gelmez.

26 Ekim 2026: AS v3010 explicit source-pointer codec ve nötr testler upstream çekirdeğe alındı. [Güncel ISPSoft çalışma sınırı ve ayrı Windows kaynak katmanı](ispsoft-lab.md).

## Giriş ve taşınabilirlik

```python
from universal_plc.delta_files import DeltaFileAdapter, FileTransformError

adapter = DeltaFileAdapter("ispsoft")
capabilities = adapter.capabilities()
result = adapter.invoke("isp.inspect", {"input_path": "task/input.isp"})
```

Python standart kütüphanesi yeterlidir. Dönüştürücüler ve MIT bildirimleri Python paketinin içindedir; çalışma sırasında internetten kod alınmaz, eski proje/checkout aranmaz. `wplsoft` ve `diadesigner` profilleri aynı sınıftan sorgulanabilir; burada yerel işlemler için `not_implemented` döndürür. Gerçek IDE adaptörü ayrıca bağlanmalıdır.

Çağıran Universal MCP, açık dosya argümanlarını görev çalışma alanına sınırlar. Dönüştürücü; arşiv/manifest içindeki yolları ayrıca doğrular. Mevcut dosya veya dizinin üzerine yazılmaz; çıktı üst dizini önceden var olmalıdır. Kaynak proje korunur. Sonuçlarda girdi/çıktı SHA-256, `vendor_compile: false` ve `live_execution: false` bulunur. Geçersiz işlemler `FileTransformError` üretir.

## İşlem sözleşmeleri

| İşlem | Gerekli argümanlar | İsteğe bağlı argümanlar |
|---|---|---|
| `isp.inspect` | `input_path` | — |
| `isp.unpack` | `input_path`, `output_dir` | — |
| `isp.pack` | `input_dir`, `output_path` | — |
| `isp.patch_st` | `input_path`, `output_path`, `pou_name`; kod veya değişken dosyasından en az biri | `code_path`, `variables_path` |
| `isp.roundtrip` | `input_path`, `output_path` | — |
| `isp.package_unpack` | `input_path`, `output_dir` | — |
| `isp.package_pack` | `input_dir`, `output_path` | — |
| `dvp.il_to_mpu` | `input_path`, `output_path` | `pou_name="Program0"`, `encoding="cp1254"` |
| `dvp.mpu_to_il` | `input_path`, `output_path` | `encoding="cp1254"` |
| `dvp.generate_isp` | `template_path`, `spec_path`, `output_path` | `api_library_path`, `special_devices_path`, `knowledge_path` |

`capabilities()` her işlem için `operation`, `implementation`, `qualification`, `description` verir. Dosya işlemlerinin `software` seviyesi aşağıdaki nötr regresyonları belirtir; bütün model/firmware dosyaları için uyumluluk taahhüdü değildir.

## ISP projesi ve kütüphane dosyaları

- Kaynak akışı `0xAA` veya `0xAC` konumunda olan, korumasız RAW-DEFLATE proje varyantları desteklenir. CRC, kaynak boyutu, kapsayıcı boyutu, kod sayfası, sürüm alanları ve akış sonu kontrol edilir. Diğer varyantlar açıklamalı hata verir; dosyanın içinde kör biçimde akış aranmaz.
- `isp.unpack` yeni dizine `original.isp`, `project-source.txt` ve `manifest.json` bırakır. Bütün kaynak metni UTF-8 olarak düzenlenebilir. Orijinal kapsayıcı hash'i denetlenir. Hiçbir düzenleme yapılmadıysa `isp.pack` kaynak byte'larını tam korur; dosyanın sıkıştırılmış byte'ları farklı olabilir.
- `isp.patch_st` yalnız adı tekil olan mevcut POU'nun `IL_ST_CODE` ve/veya `LOCAL_VAR` içeriğini değiştirir. UTF-8 BOM kabul edilir; proje kod sayfasına çevrilemeyen karakter reddedilir. Kalan kaynak alanları korunur. Yeni POU oluşturma ve ST sözdizimi derleme işlemi değildir.
- `isp.package_unpack/pack`, `0x98/0xA4/0xA6` önekli, ZIP Stored/Deflate içerikli FBU/MPU/FCU/CLU kapsayıcılarını işler. İçerideki vendor sabit ZipCrypto biçimi kullanılır; kullanıcı tarafından korunan kaynakların kilidi açılmaz. Önek ve dosya üye kümesi korunur. Paket oluşturma, bu profilde ASCII üye adları gerektirir.
- Girdi en fazla 32 MiB, açılmış kaynak veya paket üyelerinin toplamı en fazla 16 MiB, üye sayısı en fazla 512 olur. Mutlak yol, `..`, Windows aygıt adı, sembolik bağ, çakışan üye, bozuk CRC ve fazla açılım reddedilir. Tek dosya çıktıları tamamlandıktan sonra atomik ve üzerine yazmadan yayımlanır.

## DVP IL ve JSON üretimi

IL dosyası UTF-8, her satır `KOMUT,operand,...` biçimindedir. Boş satır ve `#`, `;`, `//` ile başlayan yorumlar kabul edilir. Dönüştürücünün sabitlenen sürümünde:

- LD/LDI/LDP/LDF, AND/ANI/ANDP/ANDF, OR/ORI/ORP/ORF ve OUT/SET/RST;
- LD/AND ile `=`, `<=`, `>`, `<` karşılaştırmaları;
- MOV, DADD, DSUB, DMUL, INC, ZRST, DPLSY, TMR, STL ve RET vardır.

`END` ISPSoft tarafından eklendiği için üretilmez. Desteklenmeyen komut/operand sayısı reddedilir. OR'dan sonra seri koşul gelen, bu düz ağ gösteriminde anlamı değişebilecek şekiller reddedilir. Çıktıdan sonra ek koşul upstream yönteminde koşulu tekrar eden yeni bir ağa dönüşür; bu durum sonuç uyarısında belirtilir. Gerçek IDE kontrolü gerekir.

MPU geri okuması node yapısını yeniden üretip karşılaştırır. Tanınmayan veya atlanacak node, pasif ağ ve korumalı POU reddedilir. Çıktı bir **komut listesi**dir; yorum, yerel değişken, POU/task ve donanım metadatasını taşımadığı uyarıyla bildirilir. `.mpu` tam PLC projesi değildir.

`dvp.generate_isp`, açıkça seçilmiş **DVP-SE** şablonunu kullanır. JSON şemasının Program/Function Block, global değişken/FB instance, görev sırası ve Ladder seri/paralel/contact/coil/instruction/comparator özellikleri upstream serializer tarafından sağlanır. Tam JSON açıklaması: [sabit kaynak sürümündeki JSON_SPEC](https://github.com/elmasacom/DELTA-PLC/blob/b692eee5fb9240ec37e64d0350e6b2896910c248/docs/JSON_SPEC.md).

```json
{
  "plc": {"family": "DVP", "model": "DVP-SE"},
  "variables": [],
  "programs": [{
    "name": "Neutral",
    "networks": [{
      "kind": "coil",
      "logic": {"type": "contact", "name": "M0"},
      "coil": {"name": "M1"},
      "comment": "Nötr dosya testi"
    }]
  }]
}
```

Bu örnek dosya testidir; sahada çalıştırma programı değildir. Şablonun kod sayfası kullanılır. Gerekli otomatik M/D/T/C/S tahsis aralıkları şablonda bulunmalıdır. API numarası gerektiren komutlarda uygun `.api` dosyası ayrı argümanla verilir; JSON içindeki bir dosya yolu kendiliğinden okunmaz. İsteğe bağlı CPU katalogları da açık argümanlardır; başka bir bilgisayara projeyle birlikte taşınır. Repo gerçek makine şablonu veya vendor kurulum dosyası içermez.

## Kaynaklar ve doğrulama sınırı

Lisans, URL, sabit commit, orijinal ve yerel SHA-256 kayıtları [kaynak manifestinde](../../backends/delta_sources/manifest.json) bulunur. Üç kaynak kullanıldı:

| Kaynak | Sabit commit | Yerel değişiklik |
|---|---|---|
| delta-ispsoft-codec | `fd0679cca86624ace379a4c5f462ced3c93f4211` | Kaynak biçimi ve normalizasyon; dış girişler bounded wrapper üzerinden işlenir. |
| delta-dvp-ispsoft | `2dcd3bfce09d8df37333c4ab0b8b7cb6a9a6676e` | Paket içi göreli import; kapsam ve geri dönüş denetimleri wrapper'da. |
| DELTA-PLC | `b692eee5fb9240ec37e64d0350e6b2896910c248` | Sınırsız offset arama/açma yerine CRC/boyut kontrollü loader; serializer korunur. |

Vendored CLI dosyaları doğrudan çalıştırılacak public arayüz değildir. Adaptör dışında çağrılmaları wrapper kontrollerini atlar. Upstream test dosyasında çözümlenmemiş merge işaretleri bulunduğu için o test kopyalanmadı; bağımsız nötr regresyonlar kullanıldı. Gerçek upstream örnek projeleri de kopyalanmadı.

`python -B -m unittest discover -s tests/delta_files -v` nötr fixture'larla ISP/ST Türkçe–BOM, iki kapsayıcı varyantı, byte roundtrip, DVP komutları/MPU, Program/FB üretimi, yol/CRC/boyut hataları, pasif/bilinmeyen ağlar ve eşzamanlı çıktı çakışmasını sınar.

ZIP'teki eski iki Ladder örneğinin codec ile **0/2** sonucu tarihsel kayıttır. Buradaki yazılım testleri bu eski dosyaları yeniden doğrulamış sayılmaz. AS228, tüm DVP modelleri veya kurulu ISPSoft sürümünde native derleme/kaydet–yeniden açma henüz bu paketin kanıtı değildir. WPLSoft `.dvp` yazıcısı, IDE derleme/simülasyon ve canlı upload/download/RUN/STOP burada uygulanmış gösterilmez.
