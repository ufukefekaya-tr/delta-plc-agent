# AXQ-FILE/2

## Amaç

`AXQ-FILE/2`, stdio MCP host ile DIADesigner-AX içindeki tek bounded worker arasında mutation, build, artifact ve izinli lab işlerini taşıyan fail-closed dosya protokolüdür. [AXQ-FILE/1](AXQ-FILE-1.md) replay/read-only sözleşmesi değişmeden kalır; v1 kaydı v2 mutation'a yükseltilmez.

## Dizin ve tek worker lease'i

Her task-local scratch root altında `inbox/`, `running/`, `results/`, `archive/`, `evidence/`, `workspaces/` ve `lease/` dizinleri bulunur. Host yalnız `.partial` dosyasından aynı volume üzerinde atomik rename ile `inbox/<correlation>.request.json` yayınlar. Worker exact tek lease sahibidir ve her işi şu sırayla taşır:

`inbox -> running -> results -> archive`

Lease kaydı worker kimliği, PID, process-start fingerprint'i, environment digest, başlangıç/heartbeat zamanı ve sequence taşır. İkinci watcher canlı lease'i devralamaz. PID tek başına adoption kanıtı değildir; process-start ve environment digest de eşleşir. Heartbeat stale ise yeni worker önce eski PID/state reconciliation yapar; belirsiz canlılıkta ikinci worker başlatılmaz.

## Request ve result

Request ve result exact Draft 2020-12 şemaları [contracts/v2](../contracts/v2/) altında tutulur. Ortak kurallar:

- UTF-8, BOM'suz, tek JSON object, duplicate key/trailing content/NaN/Infinity reddi.
- Exact `AXQ-FILE/2`, lowercase canonical UUID, UTC seconds timestamp ve `sha256:<64 lowercase hex>`.
- Artifact yolları yalnız scratch-root-relative `/` segmentleridir; absolute/drive/UNC/device/backslash/colon/NUL/percent/dot-segment/reparse escape reddedilir.
- Correlation, job, operation, environment, workspace, request ve before/after project digest zinciri exact eşleşir.
- Workspace mutation, güncel `expected_revision` eşleşmeden çalışmaz; başarılı mutation revision'ı tam bir artırır.
- Final dosya overwrite edilmez. Aynı correlation/job veya aynı final digest replay olarak reddedilir.

Operation girdisi request içindeki digestli artifact ile taşınır ve operation'a özel strict şemayla doğrulanır. Worker generic Python/ScriptEngine çağrısı, method adı, kod string'i veya raw IPC payload kabul etmez.

## Job durumları ve recovery

Durumlar `queued|running|succeeded|blocked|failed|cancelled|unknown_outcome` olur. `cancelled`, yalnız henüz side effect başlamamış veya handler'ın güvenli cancellation point'ine ulaşmış işte terminaldir. Worker/IDE crash durumunda:

1. `running` request ve evidence logu okunur.
2. Side effect başlamamışsa aynı request digest'iyle bir kez resume edilir.
3. Save/build sonucu digestlerle reconcile edilebiliyorsa terminal result üretilir.
4. Download/start gibi fiziksel side effect sonucu kesin değilse `unknown_outcome` verilir; otomatik retry yapılmaz.

Corrupt, partial, stale, deadline aşmış veya digest mismatch kayıt `blocked` olur. Host aynı hata fingerprint'i için iki düzeltmeden, compiler için toplam on build'den sonra tekrar üretmez.

## Workspace ve artifact

`workspace.create`, yalnız allowlist baseline digest'inden yeni task-local kopya üretir. Canonical proje vendor IDE'de açılmaz. Workspace handle baseline/current project digest, environment digest, monotonic revision, dirty ve frozen state taşır.

`artifact.freeze` yalnız save/reopen/rebuild geçmiş, sıfır error ve kabul edilen warning politikasına uyan revision için immutable candidate, native export, source mirror ve manifest üretir. Artifact içindeki her dosya digestlidir; candidate sonradan değişirse deployment prepare reddedilir.

## Operation politikası

Kanonik liste [autonomous operation allowlist](../qualification/autonomous/operation-allowlist.json) kaydıdır. Public araç isimleri operation ID ile aynıdır. Default deny uygulanır; özellikle eval/`_eval`, shell, arbitrary filesystem/path, generic import/write, force, arbitrary online write, reset-origin, firmware/library update, safety bypass ve raw IPC yoktur.

Target operasyonları yalnız yeni bir görev zarfında exact target fingerprint allowlist'e alındığında çalışabilir. `deployment.execute` ayrıca artifact+target+rollback+observation digestlerine bağlı, exact güncel `yükle` komutundan türetilmiş, en fazla 600 saniyelik ve tek kullanımlık authority kaydı ister.

## Deployment state machine

`prepared -> authorized -> executing -> observing -> succeeded`

Alternatif terminal yollar `blocked|failed|rolled_back|unknown_outcome|expired|consumed` olur. Download öncesi artifact/build digestleri, AX-308 model/device, runtime/firmware, gateway route, application identity, forced-value yokluğu ve last-known-good rollback digest'i tekrar doğrulanır. Build öncesi üretilip GVL'ye yazılan tekil runtime release digest'i plan ve authority zincirine bağlanır; download sonrasında aynı marker ve application state yeniden okunmadan start başarı sayılmaz. Fault invariant tetiklenirse stop + last-known-good download + state verification uygulanır. Bağlantı kaybı rollback kanıtı değildir ve `unknown_outcome` üretir.

`unknown_outcome` sonrasında `deployment.reconcile` yalnız aynı job, target, candidate ve rollback digest zincirini okur. Download/start/stop/rollback tekrar etmez. Adayın veya last-known-good artifact'in RUN durumda olduğu runtime marker ile doğrulanırsa immutable `DeploymentReconciliation` kaydı üretir ve yeni deployment kapısını açar; diğer bütün sonuçlarda kapı kapalı kalır.

Crash sınırları da protokol durumudur. Queue order, operation input ve inbox request aynı deterministic job/correlation kimliğiyle idempotent tamamlanır. Cancel intent request hareketinden önce kalıcılaştırılır. Target worker launch sonucu kanıtlanamazsa side-effect marker'dan önce request/target/digest bağlı `launch_uncertainty` provenance yazılır. Download intentinden önce yazılmış doğrulanmış journal olayları varsa bunların tam zincir digest'i launch kaydıyla composite `pre_intent_journal` başlığına bağlanır. Reconciliation kaydı provenance başlığının türünü `journal|launch_uncertainty|pre_intent_journal` olarak ayrıca bağlar; kaynaklar birbirinin yerine sessizce kullanılamaz. Restart aynı durable işi adopt eder, yeni bir deployment veya authority üretmez.

`project.build` öncesi scratch project byte snapshotı ve transaction intenti yazılır. Exact başarılı result/build-pass/current-digest zinciri yoksa recovery snapshotı geri yükler ve orphan payload/pass kayıtlarını arşivler; compiler error veya yeni warning de deploy edilebilir artifact üretmez.

## Observation sınırı

Observation profile yalnız exact symbol allowlist'i, `100..10000 ms` interval ve `1..300 s` duration taşır. Varsayılan `500 ms / 60 s` olur. Okuma dışı expression, method call, assignment veya online write reddedilir. JSONL/CSV trace zaman, target fingerprint, application state ve sample sequence ile digestlenir.

Standalone observation `stop_and_rollback` semantiğini kabul etmez; bu aksiyon yalnız candidate ve last-known-good artifactleri authority ile bağlayan `deployment.execute` zincirinde kullanılabilir.
