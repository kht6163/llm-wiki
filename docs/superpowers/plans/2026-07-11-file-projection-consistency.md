# 파일 투영·외부 재색인 정합성 구현 계획

> 설계 정본: `docs/superpowers/specs/2026-07-11-file-projection-consistency-design.md`

**목표:** 관리 쓰기의 DB→Markdown 투영을 최신 version에 대해 선형화하고, 이동 전 경로를
내구적으로 복구하며, 외부 reindex가 안정 파일 snapshot과 exact DB CAS를 모두 통과한
세대만 가져오게 한다.

**구조:** 새 `file_projection` 모듈이 파일 signature, fsync된 staged write, 안정 읽기와
구조화된 결과를 제공한다. `DocumentService`는 exact revision snapshot과 cleanup intent를
SQLite writer fence 안에서 검증하는 공통 projector를 사용한다. `reindex_all()`은 파일별
최대 3회 retry loop와 existing/new/rename CAS를 사용하고 CLI에 부분 실패를 노출한다.

**기술:** Python 3.12, SQLite WAL, POSIX atomic replace/fsync, pytest, ruff, mypy

---

## Task 1: 파일 세대 primitive와 cleanup schema

**파일**

- 생성: `src/llm_wiki/file_projection.py`
- 수정: `src/llm_wiki/db.py`
- 생성: `tests/test_file_projection.py`
- 수정: `tests/test_db_migrations.py`

### 1. 실패 테스트 작성

다음을 먼저 추가한다.

- fresh DB와 이미 stamp된 기존 DB 모두 `file_projection_cleanup` 및
  `document_purge_intents` table과 FK cascade를 가진다. cleanup에는 signature CHECK와
  `path_norm` lookup index가 있다.
- `FileSignature`가 dev/inode/size/mtime_ns/ctime_ns를 비교하고 같은 길이 rewrite 및 atomic
  replace를 다른 세대로 판정한다.
- `stage_text()`가 vault 전용 `.tmp`에 UTF-8 body를 만들고 파일 fsync를 호출하며 target
  parent와 같은 `st_dev`가 아니면 명시적으로 실패한다.
- `install_staged()`가 target을 atomic replace하고 parent directory를 fsync하며 temp를
  소비한다.
- stage 뒤 temp가 교체/변조되면 stored full signature 불일치로 install을 거부한다.
- nested target parent 생성은 모든 기존 component symlink를 거부하고 새 ancestor와 그 parent를
  fsync한다.
- `vault/.tmp` 자체가 symlink/non-directory이면 stage를 거부해 canonical body를 vault 밖에
  쓰지 않는다.
- 실패 전후 `cleanup_staged()`가 남은 temp만 idempotently 제거한다.

실행:

```bash
uv run pytest tests/test_file_projection.py tests/test_db_migrations.py -q
```

예상: 새 module/table이 없어 실패.

### 2. 최소 구현

`file_projection.py`에 immutable `FileSignature`, `StagedText`, `ProjectionResult`와 다음 작은
함수를 구현한다.

```python
file_signature(path, *, missing_ok=False)
managed_path(vault, rel, *, namespace="live")
stage_text(vault, target, body)
install_staged(staged, target)
unlink_regular(path, expected=None)
fsync_directory(path)
cleanup_staged(staged)
```

- signature는 `lstat`/regular-file 검증을 명시하고 정수 nanosecond 필드를 쓴다.
- managed path는 모든 lexical parent component의 symlink/non-directory를 거부하고 target을
  따라가는 기존 `safe_join().resolve()`를 filesystem mutation에 사용하지 않는다.
- temp는 snapshot/reindex에서 이미 제외되는 vault `.tmp`에 만들고 target parent와 `st_dev`를
  검증한다. cross-device target은 atomicity를 낮추지 않고 실패한다.
- write→flush→`os.fsync(fd)` 뒤에만 staged 값을 반환한다.
- install 직전 stored full signature를 다시 확인해 변조/교체된 temp를 게시하지 않는다.
- unlink/replace 뒤에는 영향받은 parent directory를 fsync한다.

`SCHEMA_SQL`에 설계의 `file_projection_cleanup`, `document_purge_intents` table과 cleanup
path lookup index를 추가한다.
IF-NOT-EXISTS table 정책이므로 `SCHEMA_VERSION`은 바꾸지 않는다. 이미 최신 stamp가 찍힌
DB에서도 `ensure_schema()`가 table을 만드는 회귀 테스트를 포함한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_file_projection.py tests/test_db_migrations.py -q
uv run ruff check src/llm_wiki/file_projection.py src/llm_wiki/db.py tests/test_file_projection.py tests/test_db_migrations.py
uv run mypy src/llm_wiki
git add src/llm_wiki/file_projection.py src/llm_wiki/db.py tests/test_file_projection.py tests/test_db_migrations.py
git commit -m "파일 투영 세대 기반 추가"
```

## Task 2: 최신 revision 공통 projector와 bounded recovery

**파일**

- 수정: `src/llm_wiki/file_projection.py`
- 수정: `src/llm_wiki/services/documents.py`
- 수정: `tests/test_file_projection.py`
- 수정: `tests/test_reindex.py`
- 수정: `tests/test_delete_recovery.py`

### 1. 실패 테스트 작성

- exact document version의 revision만 snapshot하며 body/hash가 어긋나면 파일을 건드리지 않고
  pending을 유지한다.
- update v2/v3의 staged publication을 역순으로 재개해도 v3 body만 파일에 남고 clean이다.
- 다른 `Database` instance의 commit이 첫 snapshot 뒤 일어나면 stale temp를 설치하지 않고
  최신 snapshot으로 retry한다.
- replace 또는 directory fsync 실패 뒤 DB는 pending이고 다음 호출이 canonical body로
  복구한다.
- `recover_pending()`이 작은 keyset page를 여러 번 넘고 첫 문서가 매번 바뀌거나 I/O 실패해도
  뒤 문서를 계속 처리한다.
- recovery 시작 뒤 update/delete/purge된 ID에 오래된 path/body 작업을 하지 않는다.
- stale deleted projector가 stage된 사이 clean→purge-intent→pending ABA가 일어나도 intent 부재
  CAS에 실패해 external live/trash를 건드리지 않는다.

실행:

```bash
uv run pytest tests/test_file_projection.py tests/test_reindex.py -q
```

예상: 기존 id-only clean과 stale body projection 때문에 실패.

### 2. 최소 구현

`DocumentService`에 다음 내부 경계를 추가한다.

```python
_projection_snapshot(conn, doc_id) -> ProjectionSnapshot | None
_project_current(doc_id, *, max_attempts=3) -> ProjectionResult
_recover_pending_report(*, page_size=64) -> RecoveryReport
recover_pending() -> int
```

- snapshot은 `documents.version = revisions.version` exact join과 두 hash/body hash를 검증한다.
- reader snapshot과 최종 writer CAS 모두 `document_purge_intents` 부재를 요구한다. intent가
  있으면 일반 projector는 filesystem을 만지지 않고 `purge_pending`으로 전용 finisher에
  route한다.
- `_project_current`는 temp stage를 writer 밖에서 하고 writer 안에서 전체 tuple을 다시 비교한
  뒤 live/deleted target을 설치한다.
- 이 task에서 cleanup intent가 없는 canonical live/deleted install, stale trash/live 제거와
  recovery를 모두 완성한다. `delete()`/`restore()` 일반 callsite 전환과 purge는 Task 4다.
- clean UPDATE의 WHERE에 id/path/path_norm/version/hash/is_deleted/pending을 모두 둔다.
- 설치 직후 staged inode/dev/size를 다시 확인하고 그 세대
  `mtime_ns / 1_000_000_000`만 REAL `file_mtime`에 쓴다.
- managed live/trash path의 symlink/non-regular component는 파일을 건드리지 않고 실패한다.
- temp 정리는 모든 return/exception 경로에서 보장한다.
- recovery는 max ID + keyset page만 보관하고 문서별 오류를 report/log한 뒤 계속한다.

### 3. create/update callsite 전환

`create()`와 `update()`의 `_write_file()` 및 id-only clean을 `_project_current(doc_id)`로
교체한다. targeted edit/properties/task/import는 update 위임을 통해 자동 적용된다. 기존
return, embedding, audit, metric 동작은 유지한다.

### 4. 검증 및 커밋

```bash
uv run pytest tests/test_file_projection.py tests/test_reindex.py tests/test_delete_recovery.py tests/test_service_extra.py -q
uv run ruff check src/llm_wiki/file_projection.py src/llm_wiki/services/documents.py tests/test_file_projection.py tests/test_reindex.py
uv run mypy src/llm_wiki
git add src/llm_wiki/file_projection.py src/llm_wiki/services/documents.py tests/test_file_projection.py tests/test_reindex.py tests/test_delete_recovery.py
git commit -m "문서 파일 투영을 최신 버전으로 직렬화"
```

## Task 3: 다단계 move cleanup intent

**파일**

- 수정: `src/llm_wiki/services/documents.py`
- 수정: `tests/test_file_projection.py`
- 수정: `tests/test_service_extra.py`

### 1. 실패 테스트 작성

- move DB commit 뒤 파일 처리 전 중단하고 recovery하면 이전 path가 제거되고 새 path가
  canonical body인지 검증한다.
- A→B→C를 모두 commit한 뒤 역순 projector를 실행해 A/B가 사라지고 C만 clean인지 검증한다.
- B→A 되돌리기는 A cleanup intent를 취소해 현재 파일을 보존한다.
- A→B 뒤 다른 문서가 A를 재사용하면 늦은 cleanup이 새 문서 파일을 삭제하지 않는다.
- old path 파일 signature가 intent 뒤 바뀌면 보존되고 원 문서는 pending/conflict다.
- cleanup 일부 unlink/fsync 실패 뒤 완료된 intent만 commit되고 나머지가 다음 recovery에서
  처리된다.
- 65개 이상의 intent도 transaction당 64개 이하로 처리하고 writer lock을 사이에 놓는지
  검증한다.
- 193개 intent와 첫 batch의 영구 signature conflict에서도 모든 뒤 batch를 한 번씩 방문하고
  성공 row만 제거하는지 검증한다.

실행:

```bash
uv run pytest tests/test_file_projection.py tests/test_service_extra.py -q
```

예상: old path가 DB에 내구적으로 남지 않아 실패.

### 2. 최소 구현

- `move()`의 첫 writer transaction 안에서 이전 path signature를 읽고 cleanup intent를
  upsert한다.
- 새 target과 같은 과거 intent는 삭제하고 다른 intent는 보존한다.
- `_project_current()`가 intent를 `path_norm` keyset batch로 읽어 다른 document row(tombstone
  포함), missing, exact signature 순서로 안전하게 완료한다.
- signature가 다르거나 non-regular/I/O 실패이면 파일과 해당 intent를 보존하되 같은 batch와
  뒤 batch 처리를 계속하고 최종 pending/conflict로 보고한다.
- transaction당 최대 64개만 처리하고 남은 intent가 있으면 pending을 유지한 채 다음 짧은
  transaction으로 계속한다. 이 finite batch loop는 snapshot mismatch 최대 3회 retry와 별도며,
  최종 clean 전에 target을 다시 게시·검증한다.
- 기존 `_trash_file(rel)`과 로컬 body 새 경로 쓰기를 제거하고 공통 projector만 호출한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_file_projection.py tests/test_service_extra.py tests/test_rename_references.py -q
uv run ruff check src/llm_wiki/services/documents.py tests/test_file_projection.py
uv run mypy src/llm_wiki
git add src/llm_wiki/services/documents.py tests/test_file_projection.py tests/test_service_extra.py
git commit -m "이동 경로 정리를 내구적으로 복구"
```

## Task 4: delete·restore·purge 생명주기 통합

**파일**

- 수정: `src/llm_wiki/services/documents.py`
- 수정: `tests/test_delete_recovery.py`
- 수정: `tests/test_shortlist.py`
- 수정: `tests/test_file_projection.py`

### 1. 실패 테스트 작성

- delete/update, delete/restore, restore/delete의 파일 후처리를 역순 실행해도 최신
  `is_deleted`에 맞는 live 또는 trash만 남는다.
- delete projector는 stale live 파일을 이동하지 않고 exact revision body를 trash에 쓴다.
- restore와 tombstone revive는 live 파일을 게시하고 stale trash 사본을 제거한다.
- purge의 첫 transaction 뒤 중단하면 durable intent/actor/via가 남고 startup recovery가 같은
  영구 삭제와 audit를 완료한다.
- purge finisher의 trash unlink/fsync 또는 DB commit 실패 시 row/history/intent가 남고 다음
  recovery가 완료한다.
- clean tombstone 뒤 새로 나타난 live 외부 파일 및 signature가 달라진 old path를 purge가
  보존한다.
- purge intent 뒤 restore/create revive가 영구 삭제를 추월하지 못한다.
- 두 `Database` instance의 finisher가 경쟁해도 한 번만 audit되고 다른 호출은 성공 no-op이며,
  기존 intent를 API retry가 overwrite하지 않는다.
- pending tombstone은 intent 전에 canonical deleted projection과 live 부재를 검증하고, clean
  tombstone 뒤 나타난 external live 파일은 purge가 보존한다.
- canonical trash/live 처리는 성공했지만 cleanup conflict로 문서가 pending인 경우에도 첫
  writer의 live ENOENT를 gate로 intent/finisher cleanup 단계에 진입한다.
- purge cleanup 193개와 앞쪽 I/O/conflict에서도 transaction당 64개, 뒤 row 계속 처리,
  실패 row만 durable retry인지 검증한다.
- stage된 과거 deleted projector와 purge의 clean→pending ABA를 두 DB/Event로 재현해 purge
  intent가 stale projector를 fence하고 external live를 보존하는지 검증한다.

실행:

```bash
uv run pytest tests/test_delete_recovery.py tests/test_shortlist.py tests/test_file_projection.py -q
```

예상: 현재 id-only clean, disk-body trash, best-effort purge 때문에 실패.

### 2. 최소 구현

- Task 2의 canonical deleted/live projector를 `delete()`와 `restore()` callsite에 연결하고 직접
  file operation/id-only clean을 제거한다.
- `purge()`는 기존 immutable intent가 있으면 그대로 resume한다. 처음 읽은 tombstone이
  pending이면 일반 deleted projector와 첫 writer의 live ENOENT 검증을 통과한 뒤에만
  `document_purge_intents`에 version/actor/via를 기록하고 문서를 pending으로 commit한다. 처음
  clean이면 live 외부 파일은 보존한다.
- 전용 `_finish_purge()`는 cleanup을 별도 writer transaction에서 keyset 64개씩 전부 방문한다.
  owner/missing/match/mismatch 완료 row를 제거하고 I/O 실패만 남긴다. cleanup이 0인 마지막
  writer만 trash 삭제·fsync, row cascade와 audit를 commit한다.
- 동시 finisher가 이미 row/intent를 삭제했으면 성공 no-op이며 audit를 추가하지 않는다.
- `recover_pending()`은 purge intent를 일반 deleted projector보다 먼저 finish한다. restore와
  create revive는 intent가 있으면 conflict로 거부한다.
- restore/create revive, delete, purge의 기존 권한·audit·graph/index semantics를 유지한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_delete_recovery.py tests/test_shortlist.py tests/test_file_projection.py tests/test_service_extra.py -q
uv run ruff check src/llm_wiki/services/documents.py tests/test_delete_recovery.py tests/test_file_projection.py
uv run mypy src/llm_wiki
git add src/llm_wiki/services/documents.py tests/test_delete_recovery.py tests/test_shortlist.py tests/test_file_projection.py
git commit -m "삭제와 복원 파일 생명주기 동기화"
```

## Task 5: 안정 파일 읽기와 existing/new reconcile CAS

**파일**

- 수정: `src/llm_wiki/file_projection.py`
- 수정: `src/llm_wiki/indexing.py`
- 수정: `src/llm_wiki/services/documents.py`
- 생성: `tests/test_reindex_concurrency.py`
- 수정: `tests/test_reindex.py`
- 수정: `tests/test_indexing.py`

### 1. 실패 테스트 작성

- `lstat→no-follow open/fstat→read→fstat→lstat` 중 atomic replace가 일어나면
  `file_changed`다.
- 같은 inode/길이의 in-place rewrite도 mtime_ns/ctime_ns 변화로 거부한다.
- symlink/non-regular/사라짐/permission 오류를 안정 본문으로 채택하지 않고 adopted
  signature의 `mtime_ns / 1_000_000_000`만 REAL DB mtime에 기록한다.
- final entry뿐 아니라 `vault/linkdir -> outside` 같은 parent-directory symlink도 Task 1의
  lexical validator로 open 전후 거부한다.
- stable read 뒤 real directory를 rename하고 같은 이름 symlink로 바꿔도 writer CAS 직전과
  commit 후 parent 재검증이 update/INSERT/rename을 거부한다.
- 안정 읽기 직후 관리 update가 commit되면 existing reconcile CAS가 실패·retry하고 최신 관리
  본문을 되돌리지 않는다.
- unchanged mtime UPDATE도 tuple race에 rowcount 0이면 retry한다.
- target이 snapshot 뒤 생성/삭제/restore되면 new INSERT 또는 tombstone skip을 stale
  판단으로 commit하지 않는다.
- pending target은 disk를 import하지 않고 공통 projector를 먼저 복구하며 끝까지 pending이면
  `pending_projection`으로 skip한다.
- clean tombstone skip은 exact tuple/signature transaction에서 audit와 함께 commit되고
  restore/delete 경합은 retry한다.
- pending target의 정상 delete/purge가 scanned 파일을 제거하면 superseded no-op으로 끝내고
  `file_disappeared`/skip audit로 오분류하지 않는다.
- `A.md`/`a.md` norm 충돌은 둘 다 `path_collision`이며 case-only 단일 rename은 실제 DB path와
  rename revision을 갱신한다.
- target absent의 exact cleanup generation은 rename 탐색 전에 writer 밖 owner projection으로
  제거하며 외부 새 generation 채택은 intent owner를 같은 실행에서 clean으로 만든다.
- 세 번 연속 file/target 변경은 stale revision/index/audit를 남기지 않고 reason-coded
  `skipped_conflicts`를 반환한다.

실행:

```bash
uv run pytest tests/test_reindex_concurrency.py tests/test_reindex.py tests/test_indexing.py -q
```

예상: 현재 단일 read와 id-only update 때문에 실패.

### 2. 안정 읽기 구현

`file_projection.py`에 `StableMarkdown`과 다음을 추가한다.

```python
read_stable_markdown(vault, path) -> StableMarkdown
StableFileError(reason)
```

lstat, no-follow fd before/after fstat, after lstat, byte count, regular file, lexical vault
상대 경로와 모든 parent component를 open 전후 검증하고 성공 뒤에만 UTF-8 decode한다.

### 3. existing/new retry loop 구현

- `reindex_all()` 시작과 cleanup owner 처리 뒤 종료 시 `_recover_pending_report()`를 호출한다.
  최종 pending만 `pending_projection` conflict로 합친다.
- scan 전 lexical path를 norm으로 그룹화하고 중복 norm은 처리하지 않는다.
- 발견 path마다 최대 3회 새 DB/file snapshot을 얻는다.
- existing/unchanged UPDATE는 전체 target tuple WHERE + rowcount 1 CAS를 사용한다.
- 모든 writer signature CAS 직전과 commit 후 file signature 확인 때 lexical parent validator를
  다시 실행한다.
- new INSERT는 reader와 writer에서 target 부재를 모두 검증한다.
- target absent에서는 cleanup을 rename보다 먼저 판정한다. exact old projection signature면
  reconcile writer 밖에서 owner를 처리하고 target을 retry한다. 외부 새 파일을 채택하면 같은
  transaction에서 path intent를 제거하고 commit 뒤 owner를 project한다.
- revision/tags/FTS/chunks/links/audit는 CAS와 같은 transaction에 둔다.
- `indexing.py`에 prepared FTS body/chunk/link 입력과 이를 저장하는 helper를 분리한다. parsing은
  stable read 뒤 writer 밖에서 하고 writer 안에서는 prepared row만 교체/resolve한다. 기존
  관리 쓰기 wrapper는 호환 유지한다.
- commit 뒤 file signature를 재검증하고 달라졌으면 다음 세대를 retry한다.
- 기존 counter는 path당 최종 분류 한 번, `retried`는 실제 추가 시도 수로 계산한다.

### 4. 검증 및 커밋

```bash
uv run pytest tests/test_reindex_concurrency.py tests/test_reindex.py tests/test_indexing.py -q
uv run ruff check src/llm_wiki/file_projection.py src/llm_wiki/indexing.py src/llm_wiki/services/documents.py tests/test_reindex_concurrency.py tests/test_reindex.py tests/test_indexing.py
uv run mypy src/llm_wiki
git add src/llm_wiki/file_projection.py src/llm_wiki/indexing.py src/llm_wiki/services/documents.py tests/test_reindex_concurrency.py tests/test_reindex.py tests/test_indexing.py
git commit -m "외부 파일 반영에 안정 스냅샷 적용"
```

## Task 6: rename CAS·부분 실패 report·CLI 종료 코드

**파일**

- 수정: `src/llm_wiki/services/documents.py`
- 수정: `src/llm_wiki/_cli_impl.py`
- 수정: `tests/test_reindex_concurrency.py`
- 수정: `tests/test_reindex.py`

### 1. 실패 테스트 작성

- rename stable read 뒤 source edit/move/delete가 일어나면 source exact CAS가 전체 rollback되고
  retry한다.
- target이 동시에 생성되면 rename이 덮어쓰지 않는다.
- source old path가 writer 검증 전에 다시 나타나면 `rename_source_reappeared`로 retry/skip한다.
- 같은 hash의 pending source는 rename/new create를 막고 `pending_projection`으로 보고한다.
- 초기 map 뒤 다른 connection이 새 missing same-hash source를 만들면 `data_version` global
  rebuild가 이를 발견하며, 세 번 계속 바뀌면 duplicate INSERT 없이 `rename_source_changed`다.
- DB commit 없이 외부에서 같은-hash source 파일 하나를 추가로 제거해도 target-absent writer가
  모든 same-hash live row의 lexical 존재를 재검사해 unique/ambiguous를 다시 판정한다.
- ambiguous clean missing source는 기존처럼 new create로 fallback한다.
- 성공 commit 뒤에만 renamed counter/renames 결과가 추가된다.
- 결과에 `recovered_pending`, `retried`, `skipped_conflicts`가 항상 존재한다.
- reconcile 종료 뒤 최신 exact DB path가 ENOENT인 clean live 문서만 `missing_files`인지
  검증한다. symlink/non-regular/EACCES/기타 I/O는 `file_unreadable` conflict다.
- CLI가 상세 conflict를 출력하고 unresolved conflict에 1, missing/skipped_deleted만 있으면 0을
  반환한다.

실행:

```bash
uv run pytest tests/test_reindex_concurrency.py tests/test_reindex.py -q
```

예상: rename source id-only update와 CLI 무조건 0 때문에 실패.

### 2. 최소 구현

- global round 시작 시 O(D) 한 번으로 clean/pending missing-by-hash map과 같은 reader
  connection의 `PRAGMA data_version`을 캡처한다. target-absent writer 안에서 generation이
  같을 때만 rename/INSERT하고, 다른 connection commit을 감지하면 전체 map/disk round를 다시
  만든다. 최대 3 round 뒤에도 변하면 `rename_source_changed`이며 INSERT하지 않는다. 자체
  commit은 in-memory map에 반영해 전체 복잡도를 `O(3(F+D))`로 제한한다.
- writer transaction에서 target 부재, source 전체 tuple, source path 부재, target file
  signature를 함께 검증한다.
- `data_version`이 같아도 rename/INSERT 직전에 initial map의 같은-hash 모든 live row lexical
  source 존재를 다시 검사하고 commit 후에도 재검증한다. filesystem-only 후보 변화는 최신
  unique/ambiguous 판정이나 후속 work item으로 처리한다.
- source path는 UPDATE 직전과 commit 직후에도 확인한다. commit 뒤 재등장하면 old path를 bounded
  work queue에 넣어 새 generation으로 reconcile하고 끝까지 불안정할 때만 고정 reason을 남긴다.
- source UPDATE는 exact WHERE/rowcount 1 CAS로 하고 revision/index/graph/audit를 같은
  transaction에 둔다.
- 성공 rename transaction도 target path의 모든 cleanup intent를 제거하고 commit 뒤 affected
  owner를 writer 밖에서 project한다.
- 선행 `claimed` mutation을 제거하고 commit 성공 뒤에만 결과를 집계한다.
- 고정 reason code와 attempts를 path당 최종 한 번 기록한다.
- path 분류 우선순위를 `renamed > created > updated > unchanged`로 고정한다.
- `_reindex()`가 recovered/retried/skipped와 상세 항목을 출력하고 conflict 존재 시 1을
  반환한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_reindex_concurrency.py tests/test_reindex.py -q
uv run ruff check src/llm_wiki/services/documents.py src/llm_wiki/_cli_impl.py tests/test_reindex_concurrency.py tests/test_reindex.py
uv run mypy src/llm_wiki
git add src/llm_wiki/services/documents.py src/llm_wiki/_cli_impl.py tests/test_reindex_concurrency.py tests/test_reindex.py
git commit -m "재색인 경쟁 충돌을 감지해 보고"
```

## Task 7: 운영 문서·통합 검증·독립 리뷰

**파일**

- 수정: `README.md`
- 수정: 필요 시 위 task의 source/test 파일

### 1. 운영 문서 갱신

README의 reindex/recovery 설명에 다음을 반영한다.

- pending 관리 projection이 외부 파일보다 우선한다.
- reindex는 경합 파일을 최대 세 번 재시도하고 일부 미수렴 시 exit 1이다.
- missing file과 tombstone skip은 경고지만 exit 0이다.
- cleanup signature conflict는 외부 파일을 보존하고 다음 reindex에서 별도 문서로 채택할 수
  있다.

### 2. 집중 회귀

```bash
uv run pytest tests/test_file_projection.py tests/test_delete_recovery.py tests/test_reindex_concurrency.py tests/test_reindex.py tests/test_service_extra.py tests/test_rename_references.py tests/test_snapshot_restore.py -q
```

### 3. 전체 품질 gate

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src/llm_wiki
uv lock --check
uv build --out-dir /tmp/llm-wiki-projection-consistency-dist
```

### 4. 독립 리뷰

spec 준수 reviewer와 code-quality reviewer를 각각 실행한다. Critical/Important 지적은 테스트를
먼저 추가해 재현한 뒤 수정하고 전체 gate를 다시 실행한다.

### 5. 최종 커밋

```bash
git add README.md
git commit -m "파일 재색인 복구 절차 문서화"
git status --short --branch
git log --oneline --decorate -12
```

빈 문서 변경이면 마지막 커밋은 생략한다. release/push는 이 계획 범위에 포함하지 않는다.
