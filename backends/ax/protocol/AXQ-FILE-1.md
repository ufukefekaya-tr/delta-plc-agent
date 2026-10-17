# AXQ-FILE/1

## Amaç ve yetki sınırı

`AXQ-FILE/1`, görünür ve insan kontrollü bir süreç için sürümlü dosya alışveriş sözleşmesidir. F00'da yalnız recorded fixture conformance'ı vardır; runtime dispatcher, MCP kaydı, generic worker, vendor IDE veya gerçek hedef bağlantısı yoktur.

Bu metindeki `MUST`, fail-closed kabul koşuludur. Uymayan dosya çalıştırılmaz, başka bir operation'a çevrilmez ve başarı sayılmaz.

## Ortak kodlama

- Dosya içeriği BOM'suz UTF-8 ve tek bir JSON object MUST olmalıdır.
- `protocol` değeri exact `AXQ-FILE/1` MUST olmalıdır.
- Bilinmeyen alanlar reddedilir; gevşek version dönüşümü yapılmaz.
- UUID alanları canonical lowercase `8-4-4-4-12` biçimindedir.
- Digest alanları `sha256:` ve 64 lowercase hex karakterinden oluşur.
- Zamanlar UTC seconds precision `YYYY-MM-DDTHH:MM:SSZ` biçimindedir.
- JSON number, NaN/Infinity, duplicate key ve trailing content kabul edilmez.

## Request kaydı

Tamamlanmış request exact alanları taşır:

| Alan | Kural |
|---|---|
| `protocol` | Exact `AXQ-FILE/1` |
| `message_type` | Exact `request` |
| `correlation_id` | Lowercase UUID |
| `operation` | Allowlist'teki exact operation ID |
| `created_at` | UTC timestamp |
| `deadline_at` | `created_at` sonrasında UTC timestamp |
| `environment_digest` | `EnvironmentIdentity` kaydının SHA-256 digest'i |
| `artifacts` | Sıralı scratch-relative artifact descriptor listesi |

Bir artifact descriptor exact `path` ve `sha256` alanlarına sahiptir. Request dosyasının tamamlanmış byte SHA-256 değeri result içindeki `request_digest` ve evidence içindeki `input_digest` olur.

## Result kaydı

Tamamlanmış result exact alanları taşır:

| Alan | Kural |
|---|---|
| `protocol` | Exact `AXQ-FILE/1` |
| `message_type` | Exact `result` |
| `correlation_id` | Request ile exact eşleşir |
| `operation` | Request ile exact eşleşir |
| `created_at` | Deadline'ı aşmayan UTC timestamp |
| `status` | `verified`, `unverified` veya `blocked` |
| `environment_digest` | Request ile exact eşleşir |
| `request_digest` | Tamamlanmış request byte digest'i |
| `artifacts` | Sıralı scratch-relative artifact descriptor listesi |
| `diagnostics` | Sıralı string listesi |

Tamamlanmış result byte SHA-256 değeri EvidenceRecord içindeki `output_digest` olur. Result başarısı herhangi bir vendor veya fiziksel yeterlilik kanıtı değildir.

## Atomic publish

Üretici aynı spool dizininde önce `<correlation>.<request|result>.json.partial` dosyasını yazar. Tam içerik flush edildikten sonra aynı dosya sistemi içinde atomik rename ile `.partial` son eki kaldırılır. Tüketici:

- `.partial` dosyasını MUST ignore eder;
- tamamlanmış ad için tek create gözler;
- corrupt veya eksik JSON'u fail-closed reddeder;
- deadline geçmiş request/result'u stale sayar;
- aynı correlation için ikinci final dosyayı replay/ambiguity olarak reddeder.

Retry yeni lowercase UUID ile yeni request üretir. Var olan final dosya overwrite edilmez.

## Scratch path politikası

Artifact yolu yalnız atanmış scratch root'a göre relative, `/` ayraçlı ve en az bir normal segmentten oluşur. Her segment ASCII harf/rakam ile başlayabilir; devamında ASCII harf/rakam, `.`, `_` veya `-` kullanılabilir.

Aşağıdakiler fail-closed reddedilir:

- `/` veya `\\` ile başlayan absolute/UNC yol;
- drive-qualified `C:` benzeri yol;
- `\\?\\`, `\\.\\` veya başka device namespace;
- backslash, colon, NUL, boş segment;
- `.` veya `..` segmenti ve percent-encoded traversal;
- scratch root dışına resolve olan veya symlink/reparse-point ile kaçan yol.

Path doğrulaması önce lexical, sonra gerçek scratch root altında containment kontrolüyle yapılır. Arbitrary filesystem path kabul edilmez.

## Operation ve default-deny

Tek kanonik politika [operation-allowlist.json](../qualification/f00/operation-allowlist.json) dosyasıdır. Exact beş operation dışında bütün operation'lar reddedilir. Özellikle eval, `_eval`, shell, arbitrary path/filesystem, import/write, login, download, RUN/STOP/reset, PLC/online, target discovery ve dev-sentinel yolları yasaktır.

F00 durumunda beş izinli operation dahi `declared_not_executable` olur. Bu protokol bir operation'ı çalıştırma yetkisi vermez.

## Request → result → evidence zinciri

Conformance zinciri şu eşitliklerin tümünü gerektirir:

1. Request/result `correlation_id`, `operation` ve `environment_digest` exact eşleşir.
2. Result `request_digest`, tamamlanmış request dosyasının byte digest'idir.
3. Evidence `correlation_id` request ile eşleşir.
4. Evidence `environment_digest` request ile eşleşir.
5. Evidence `input_digest` request digest'ine, `output_digest` result digest'ine eşittir.

Her mismatch, timeout, stale/corrupt/partial dosya veya unsupported version `blocked` ve non-dispatch sonucudur. Bilinmeyen sonuç success'e yükseltilmez.

## F00 NOT_RUN listesi

Upstream kod çalıştırma, MCP kurulumu, vendor IDE açma, import, build/rebuild, export uygulaması, PLC bağlantısı, online/HIL ve fiziksel işlem F00'da `NOT_RUN` kalır.
