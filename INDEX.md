# Universal PLC Agents — İndeks

Bu dosya insan ve ajan için tek giriş haritasıdır. Her kuralın kanonik yeri yalnız bir dosyadır; diğer belgeler aynı kuralı kopyalamak yerine ona bağlanır.

## Hızlı başlangıç

1. [README](README.md)
2. [Kanonik sistem politikası](SYSTEM_PROMPT.md)
3. Repo içi ajan yönlendirmesi: [AGENTS.md](AGENTS.md)
4. [Normal çalışma akışı](docs/WORKFLOW.md)
5. [İnsan ve kanıt sınırı](docs/EVIDENCE_AND_HUMAN_BOUNDARY.md)

## Kanonik belgeler

- [Çalışma akışı](docs/WORKFLOW.md)
- [Mühendislik kuralları](docs/ENGINEERING_RULES.md)
- [Kanıt seviyeleri ve insan sınırı](docs/EVIDENCE_AND_HUMAN_BOUNDARY.md)
- [Target Profile rehberi](docs/TARGET_PROFILE_GUIDE.md)
- [Kaizen protokolü](docs/KAIZEN_PROTOCOL.md)
- [Anonim kültür aktarımı](knowledge/CULTURE_TRANSFER.md)
- [Kaizen kayıtları](kaizen/README.md)

## Mod ve rol kartları

- [Fast mod](prompts/modes/FAST.md)
- [Analist](prompts/roles/ANALYST.md)
- [Mimar](prompts/roles/ARCHITECT.md)
- [Programcı](prompts/roles/PROGRAMMER.md)
- [Bağımsız Doğrulayıcı](prompts/roles/VERIFIER.md)
- [10. Adam](prompts/roles/TENTH_MAN.md)
- [Kaizen Küratörü](prompts/roles/KAIZEN_CURATOR.md)

## Proje şablonları

- [Project Brief](templates/PROJECT_BRIEF.md)
- [Target Profile](templates/TARGET_PROFILE.md)
- [Control Contract](templates/CONTROL_CONTRACT.md)
- [I/O ve değişken sözleşmesi](templates/IO_AND_VARIABLES.md)
- [LD/ST dil kararı](templates/LANGUAGE_DECISION.md)
- [Ladder network reçetesi](templates/LD_NETWORKS.md)
- [ST modül şablonu](templates/ST_MODULE.md)
- [Görev zarfı](templates/TASK_ENVELOPE.md)
- [Makinece okunan görev zarfı](templates/TASK_ENVELOPE.json)
- [Cerrahi değişiklik](templates/SURGICAL_CHANGE.md)
- [Session checkpoint](templates/SESSION_CHECKPOINT.md)
- [Makinece okunan session checkpoint](templates/SESSION_CHECKPOINT.json)
- [Test planı](templates/TEST_PLAN.md)
- [Bağımsız review](templates/REVIEW.md)
- [İnsan handoff](templates/HUMAN_HANDOFF.md)
- [ADR](templates/ADR.md)
- [Kaizen kaydı](templates/KAIZEN.md)

## Doğrulama

- [Prompt kabul senaryoları](tests/PROMPT_SCENARIOS.md)
- [Repo denetleyicisi](tools/check_repo.py)
- [Session guard](tools/session_guard.py)
- [Denetleyici birim testleri](tests/test_check_repo.py)
- [Session guard testleri](tests/test_session_guard.py)

`tools/check_repo.py` Git-tracked framework kapsamındaki yapı, metin, bağlantı, rol, prompt ve Kaizen sözleşmesini denetler; ignored/vendor çalışma alanlarını taramaz. `--verify-bootstrap` tracked/committed control plane'in `HEAD` ile byte-eşitliğini sınar. `tools/session_guard.py` envelope/checkpoint/scope uyuşmazlığında fail-closed sonuç verir; ajanın metni kavradığını veya gerçek IDE/saha uygunluğunu kanıtlamaz.

## Universal uygulama

- [Taşınabilir kurulum](docs/PORTABILITY.md)
- [Tek MCP, sözleşmeler ve API](docs/UNIVERSAL_RUNTIME.md)
- [Model kataloğu](catalog/README.md)
- [Goals ve kabul açıkları](goals/README.md)
- [W1–W7 workflows](workflows/README.md)
- [AX backend ve 19 operasyon](backends/ax/README.md)
- [ISPSoft/DVP dosya adaptörü](docs/adapters/delta-files.md)
- [Uygulama raporu](docs/public/VALIDATION.md)
- [Üçüncü taraf lisansları](THIRD_PARTY_NOTICES.md)

## Güncel DVP / WPLSoft başlangıcı — 17 Ekim 2026

DVP işi alan ajan [0.3.1 iş teslim rehberini](docs/DVP_AGENT_RUNBOOK.md), [güncel goal’i](goals/dvp-final.json) ve [workflow’u](workflows/dvp-final.json) okur. WPLSoft üretim/derleme ve talep edilen yükleme Computer Use ile; online okuma, profil bağlı M/D kontrolü ve watch Universal MCP içindeki `delta.dvpserial` adaptörüyle yürür. `delta.wplsoft` katalog kaydı tek başına çalışan bir MCP derleyicisi değildir. Seri model kapsamını [katalog](catalog/README.md) belirler.

- [DVP seri bağlantı ve sınırlar](docs/adapters/dvp-serial.md)
- [Genel M/D proje profili](docs/adapters/dvp-controls.md)
- [Deneysel SS2 aktarımı ve ayrı kabulü](docs/adapters/dvp-transfer.md)
- [DVP kurulum gereksinimleri](packaging/dvp-prerequisites.json)
- [Makineden bağımsız iş talebi şablonu](examples/dvp-job.json)
- [0.3.1 teslimi, yazılım testleri ve açık native/canlı kabul](docs/public/VALIDATION.md)

AX ve ISPSoft kendi girişlerini korur. Tarihsel makine profilleri veya eski saha yetkileri yeni görevin çalışma profili seçilmez.
