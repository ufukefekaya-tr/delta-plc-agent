# Session Checkpoint

Bu küçük şablon yerel/ignored `.agent-state/<task-id>/` alanına veya orkestratörün task kaydına kopyalanır. Canonical checkpoint'in tek yazarı orkestratördür; alt ajanlar benzersiz handoff üretir.

Uzun veya mutasyonlu görevde kanonik runtime kaydı [SESSION_CHECKPOINT.json](SESSION_CHECKPOINT.json) olur; bu Markdown yalnız insan görünümüdür. `session_guard.py snapshot` kimlik alanlarını üretir, fakat sıra/karar/kanıt güncellemesinin sahibi orkestratördür.

## Kimlik

- Task kimliği:
- Sıra numarası:
- Güncelleyen rol/ajan:
- Son güncelleme:
- Repo baseline/branch:
- `POLICY_EPOCH` — `sha256-path-nul-bytes-nul-v1` değeri:
- Envelope raw-byte SHA-256:
- Worktree status SHA-256:
- Mod: `NORMAL | FAST`

## Intent ve kapsam

- Kullanıcı amacı / Intent Lock:
- Kapsam içi:
- Kapsam dışı:
- İnsan IDE/saha sınırı kabulü:
- Immutable görev zarfı/authority kaynağı:

## Sahiplik

| Dosya/modül/output/state | Owner | Yetki: read/write/commit | Başka yazar var mı? |
|---|---|---|---|

## İlerleme

- Son tamamlanan kapı:
- Koşturulan kontroller ve kanıt seviyesi:
- Bekleyen kapılar/TBD:
- Açık varsayımlar ve karar sahibi:
- Sıradaki tek atomik adım:
- Alt ajan teslim/evidence bağlantıları:

## Git ve commit lease

- Yerel commit politikası: `SCOPED_MANDATORY`
- Exact dosya allowlist'i:
- Ayrık worktree/branch veya global lease sahibi:
- Index/worktree durumu:
- Tamamlanan mutasyonun commit hash'i:

## Yeniden yükleme

Compaction, yeni oturum/ajan veya handoff sonrasında:

1. `AGENTS.md`, `SYSTEM_PROMPT.md` ve `INDEX.md` control plane'ini yeniden oku.
2. `POLICY_EPOCH`, algoritma kimliği, envelope hash'i, baseline ve immutable scope'u bu kayıtla karşılaştır.
3. Güncel diff ve sahiplik tablosunu doğrula.
4. `python tools/session_guard.py ... resume` çalıştır; `RESUME_ACK` içinde uyuşan/uyuşmayan alanları ve sıradaki tek adımı yaz.
5. Uyuşmazlık çözülmeden edit veya commit yapma.

ACK yalnız öz-beyandır; okuma/anlama kanıtı veya yeni yetki kaynağı değildir. Compaction'ın runtime tarafından bildirilmesi garanti değilse her edit/commit oturumundan hemen önce epoch ve scope kontrol edilir.
