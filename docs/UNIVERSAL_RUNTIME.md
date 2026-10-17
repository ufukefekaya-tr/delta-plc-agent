# Universal çalışma zamanı

## Akış

`TargetProfile → Registry → Adapter → ExecutionContext → EvidenceRecord`.

TargetProfile vendor/family/model gerektirir; firmware, toolchain, ide_version, project_format bilindiğinde eklenir. Katalog uyumluluk adayını seçer; firmware/IDE/native işlem doğrulamasını uydurmaz. Proje metadatası kullanıcı modeliyle çelişirse işlem reddedilir. Okunamayan proprietary proje kimliği kullanıcı beyanı olarak etiketlenir; metadata okunmuş gibi sunulmaz.

## MCP araçları

`environment.probe`, `target.resolve`, `capabilities.list`, `workspace.create`, `workspace.status`, `workspace.import`, `adapter.invoke`, `evidence.read`, `workflow.start`, `workflow.run`, `workflow.status`.

`adapter.invoke(workspace_id, operation, arguments, expected_context_digest)` yalnız adaptörün kayıtlı typed operasyonlarını çağırır. AX'in 19 operasyon adı ve eski CLI girişleri AX paketinde korunur. Universal `workspace.create` bir dosya çalışma alanı; AX `workspace.create` hash kilitli native baseline klonudur. İkisi de `project.create` native sıfırdan oluşturma kabulünün yerine geçmez.

## Dosya sınırı

İçeri alma açık read-root + SHA-256 gerektirir. Dönüştürücülerin `*_path` ve `*_dir` girdileri workspace `files` dizinine göre çözülür. Mutlak yol, traversal ve symlink kaçışı reddedilir. Kaynak baseline değiştirilemez; yeni çıktılar yeni isimle yazılır. Native AX kendi profil, scratch root ve exact hedef kontrollerini ayrıca uygular.

## Kanıt ve devam

İşlem başlamadan kalıcı intent; dönüşte ayrı terminal kayıt yazılır. Son kaydı eksik intent `unknown_outcome` olarak okunur. Workflow için kalıcı tek-admission dosyası ikinci sürecin aynı işi tekrar yürütmesini engeller. Backend işi kuyruğa aldıysa bağımlı adım otomatik başlamaz. Terminal araç dönüşü, native kabul veya hedefte başarı değildir.

Workspace manifesti `task_id`, `revision`, hedef, adapter, baseline hash ve authority sınırını digest'e bağlar. Bunlar belirtilmezse workspace kimliği/`initial` kullanılır; canlı yetki üretilmez. Evidence kaydı hedefi, paket sürümünü, adaptör modül hash'ini ve backend'in girdi/çıktı kanıtını taşır. Aynı geçerli kullanıcı revizyonundan üretilen yeni candidate yetkisi backend'de bağlanır; workflow kaydı tek başına yetki değildir.

## Yeni üretici

Katalogda vendor/model kuralı ve adaptör kimliği ekleyin; Python entry point grubu `universal_plc.adapters` altında adaptörü kaydedin. Adaptör `capabilities()` ve `invoke(operation, arguments)` sağlar. Katalog modeli ve çalışma kanıtı ayrı tutulur. İkinci sentetik üretici testi çekirdeğin Delta'ya kilitlenmediğini sınar; gerçek donanım desteği ilan etmez.

## DVP / WPLSoft iş devri

Ajan [DVP runbook](DVP_AGENT_RUNBOOK.md) ve [nihai workflow tarifini](../workflows/dvp-final.json) izler. WPLSoft oluşturma/düzenleme/derleme ve talep edilen yükleme Computer Use aşamalarıdır; bunlar `workflow.run` tarafından çalıştırılan sahte MCP çağrılarına çevrilmez. Online watch yalnız MCP kullanır.

`delta.wplsoft` `.dvp` proje/IDE eşlemesidir; bu dosya adaptörünün native MCP işlemleri hâlâ `not_implemented` olur. Mevcut online uygulama `delta.dvpserial` kimliğindedir. Seri `TargetProfile` için `toolchain=dvpserial` ve desteklenen tam model gerekir; `.dvp` proje biçimi seri hedefe eklenmez. Kaynak dosya ve SHA, ayrı kaynak/iş bağlamında tutulur.

Seri adaptörün temel işlemleri `serial.ports`, `target.connect/status/read/identify/disconnect`; profil işlemleri `machine.profile/command/parameter.write/read/status/observe/reconcile` olur. Komut ve parametre adresleri dış proje profilinden gelir. `machine.profile` port açmadan incelenebilir. RUN/STOP ve deneysel native program aktarımı ayrı yetki ve doğrulama gerektirir.

GUI’den MCP’ye geçişte WPLSoft portu bırakır. MCP, güncel kaynak/revizyon ve hedefe bağlı mevcut doğrulanmış workspace’i kullanır; yeni bir workspace oluşturarak canlı yetki kazanılmaz. Watch kaydındaki her örnek güncel revizyon kanıtını taşımalıdır. [Profil sözleşmesi](adapters/dvp-controls.md), [seri sınırlar](adapters/dvp-serial.md), [0.3.1 kabul durumu](public/VALIDATION.md).
