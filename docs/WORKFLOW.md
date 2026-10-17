# Çalışma Akışı

Bu belge, [kanonik sistem promptundaki](../SYSTEM_PROMPT.md) kuralları bir iş oturumunda uygulamak için yön haritasıdır. Politika çelişkisinde sistem promptu geçerlidir.

## Oturumu başlat

1. `AGENTS.md + SYSTEM_PROMPT.md + INDEX.md` control plane'inin temiz/committed olduğunu; `sha256-path-nul-bytes-nul-v1` algoritmasıyla `POLICY_EPOCH` kimliğini doğrula.
2. Yeni uzun/mutasyonlu görevse [Task Envelope](../templates/TASK_ENVELOPE.md) ile birlikte JSON zarfı oluşturup `BOOTSTRAP_ACK`; compaction/handoff/uyuşmazlık sonrasıysa JSON checkpoint'i `session_guard.py resume` ile doğrulayıp `RESUME_ACK` kaydet.
3. [İndeksten](../INDEX.md) yalnız atanmış rolü ve görevle ilgili belge/şablonları seç.
4. Güncel talebe bakarak modu belirle; geçmiş metinlerden mod çıkarma.
5. Dosya, sinyal ve fiziksel çıkış sahipliğini görünür çalışma listesine yaz.
6. Normal moddaysa kullanıcıyla doğal ilk hasbihali başlat; keşfedilebilir bilgiyi tekrar sorma.

Orkestratör [Session Checkpoint](../templates/SESSION_CHECKPOINT.md) ve kanonik JSON karşılığının tek yazarıdır; bootstrap, anlamlı karar, commit ve handoff sonrasında sıra numarasını artırır. Alt ajanlar checkpoint yazmaz. Her edit/commit öncesi epoch ve exact scope `session_guard.py action` ile yeniden kontrol edilir.

## Normal çalışma panosu

Her aşama, bir sonrakinin girdisini üretir:

| Aşama | Ana teslim | Sonraki aşamaya geçiş işareti |
|---|---|---|
| Kanıt okuma | Kaynak ve belirsizlik listesi | Güncel kaynaklar ayrıştırıldı |
| Hasbihal | Ortak proses anlatısı | Fiziksel amaç iki tarafça aynı anlaşılıyor |
| Intent Lock | Kısa davranış sözleşmesi | Kritik karar boşluğu kalmadı |
| Analiz | I/O anlamı ve normal/fault davranışı | Davranış gözlenebilir biçimde tarifli |
| Mimari | Modül, sahiplik, arayüz ve LD/ST kararı | Tek yazarlar ve sınırlar açık |
| 10. Adam | Karşı senaryo ve gereken kanıt | Kritik itiraz çözüldü veya açık risk olarak kabul edildi |
| Programlama | Dar ST modülü veya Ladder ağ reçetesi | Taslak dondurulmuş sözleşmeyle uyumlu |
| Doğrulama | Bağımsız bulgu ve senaryo sonucu | Hatalar kapandı; insan kanıtı gerekenler ayrıldı |
| Kaizen | Öğrenme ve rollback kaydı | Dersin geçerlilik sınırı açık |
| Handoff | İnsan IDE/FAT kontrol listesi | Sorumluluk ve beklenen kanıt devredildi |

10. Adam her görevde kurulmaz; tetik kararı sistem promptundaki risk listesine göre verilir.

## Çok ajanlı oturum

Orkestratör önce modül sınırlarını ve arayüzleri dondurur. Sonra yalnız çakışmayan analiz veya modülleri paralel verir. Her görev kartında şunlar bulunur:

- Okunacak kanıt
- Üretilecek tek teslim
- Sahip olunan dosya veya mantık alanı
- Değiştirilmeyecek alan
- Açık varsayım ve tamamlanma ölçütü
- Yerel commit politikası: `SCOPED_MANDATORY`
- Base, exact dosya allowlist'i, testler ve ayrık worktree ya da global commit lease
- Push/remote/history rewrite yetkisi: `yok` veya açık kaynak

Programcı ve Doğrulayıcı aynı teslim üzerinde ardışık çalışır; doğrulayıcı programcının sonucunu bağımsız inceler. Her yazıcı ajan başarıyla tamamladığı ayrık kapsamı ayrıca kullanıcı izni istemeden atomik yerel commit ile kapatır ve hash ile test sonucunu orkestratöre bildirir. Salt-okunur görev commit üretmez; başarısız kabul kontrolleri tamamlanmış iş gibi commitlenmez. Ayrık branch/worktree tercih edilir. Ortak worktree'de commit penceresi tek bir global lease sahibine verilir; lease boyunca başka ajan stage/commit yapmaz veya test edilen ağacı değiştirmez. Push, remote değişikliği ve history rewrite ayrı kullanıcı yetkisi gerektirir. Orkestratör çelişkileri çözer, nihai paketi birleştirir ve release geçmişini doğrular.

## Çalışma paketi kullanımı

İhtiyaca göre `templates/` altındaki belgeleri doldur:

- Proses ve hedef için `PROJECT_BRIEF.md` ile `TARGET_PROFILE.md`
- Davranış ve sahiplik için `CONTROL_CONTRACT.md`
- Ajan kapsamı/yetkisi için insan-okur `TASK_ENVELOPE.md` ve makine-okur `TASK_ENVELOPE.json`; maddi davranış/kapsam değişikliği sınırı için `SURGICAL_CHANGE.md`
- Sinyaller için `IO_AND_VARIABLES.md`
- LD/ST kararı için `LANGUAGE_DECISION.md`
- Program teslimi için `LD_NETWORKS.md` veya `ST_MODULE.md`
- İnceleme için `TEST_PLAN.md` ve `REVIEW.md`
- İnsan devri için `HUMAN_HANDOFF.md`
- Karar ve öğrenme için `ADR.md` ile `KAIZEN.md`

Yalnız işe yarayan belgeleri aç. Boş şablon üretmek ilerleme sayılmaz.
Davranışsız küçük yazım/yorum düzeltmesinde cerrahi alanların kısa hali Task Envelope içinde yeterlidir; riskli veya maddi değişiklikte ayrı Surgical Change artifact'i kullanılır.

## Kapanış kontrolü

Teslimden önce şu kısa turu yap:

- Amaç ile program davranışı hâlâ aynı mı?
- Her fiziksel çıkış ve kritik state tek yazarlı mı?
- Stop, fault, reset ve enerji dönüşü açık mı?
- Birimler, aktif seviyeler ve timeout'lar görünür mü?
- Koşturulan, koşturulmayan ve insan kanıtı gerektiren kontroller ayrıldı mı?
- IDE/lab kanıtı ve kalan fiziksel kabul, güncel yetki sınırlarıyla ayrıldı mı?
- Değişiklikten çıkan ders, yanlış genelleme sınırıyla kaydedildi mi?
- Checkpoint son baseline, karar, kanıt, sahiplik ve tek sonraki adımı taşıyor mu?
- Control plane değiştiyse eski ACK/checkpoint'ler geçersiz kılındı mı?
- Gerçek diff, önceden yazılan cerrahi sınır ve dokunulmayacak invariantlarla uyumlu mu?
- Tamamlanan mutasyon exact scope içindeki atomik yerel commit ile kapandı mı?

Fast çalışma için [Fast mod kartını](../prompts/modes/FAST.md) kullan.
