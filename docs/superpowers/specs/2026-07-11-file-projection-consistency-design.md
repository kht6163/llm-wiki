# 파일 투영·외부 재색인 정합성 설계

## 배경

문서 쓰기는 SQLite revision을 먼저 commit하고 Markdown 파일을 뒤에 원자 교체한다. 이
순서는 DB를 정본으로 유지하고 중단 후 `recover_pending()`으로 파일을 복구하게 해 주지만,
현재 후처리는 DB에서 commit한 당시의 `path/body`를 그대로 파일에 쓰고 문서 `id`만으로
`file_state='clean'`을 설정한다. 같은 문서의 두 쓰기가 역순으로 파일 후처리를 끝내면 최신
DB version 위에 오래된 본문이 게시된 뒤 `clean`으로 표시될 수 있다.

`move()`는 새 경로만 DB에 남기므로 commit 뒤 중단되면 이전 경로를 복구 시점에 알 수 없다.
연속 이동은 여러 이전 경로를 남길 수 있고, 늦은 정리가 이미 다른 문서에 재사용된 경로를
삭제할 수도 있다. 삭제·복원·purge도 같은 무조건 후처리 때문에 오래된 생명주기의 파일을
부활시키거나 새 세대의 파일을 제거할 수 있다.

외부 편집을 가져오는 `reindex_all()`은 파일 본문과 mtime을 서로 다른 시점에 읽고, 그 뒤
문서 `id`만으로 최신 DB를 덮어쓴다. 파일을 읽는 동안 atomic replace가 일어나거나 읽은 뒤
웹/MCP 편집이 commit되면 서로 다른 파일 세대가 섞이거나 최신 관리 쓰기가 외부 파일의
이전 본문으로 되돌아간다. `pending` 문서도 디스크가 정본인 것처럼 가져오며 rename source와
target을 같은 세대로 검증하지 않는다.

## 목표

- 관리 쓰기의 파일 후처리는 항상 **현재 DB version/path/body**로 수렴한다.
- `file_state='clean'` 전환 시점에는 현재 document tuple과 정확히 일치하는 revision이 현재
  경로에 게시되어 있고, 그 문서가 남긴 모든 이전 경로 정리가 완료 또는 새 소유자에 의해
  명시적으로 대체되어 있다. 그 뒤 SQLite fence를 따르지 않는 외부 편집은 다음 reindex가
  탐지할 때까지 disk를 다시 발산시킬 수 있다.
- 오래된 worker는 최신 파일을 덮어쓰거나 최신 문서를 `clean`으로 만들 수 없다.
- 이동 전 경로는 process 중단과 연속 이동을 넘어 내구적으로 복구되고, 재사용된 경로의 새
  파일은 삭제하지 않는다.
- 삭제 상태는 revision의 canonical body를 휴지통에 투영하고 live 경로를 제거한다. 복원은
  같은 canonical body를 live 경로에 투영하고 이전 휴지통 사본을 제거한다.
- `recover_pending()`은 오래된 body/path 목록을 보관하지 않고 문서별 최신 snapshot을 다시
  읽어 bounded sweep으로 복구한다.
- 외부 파일은 한 세대의 안정된 byte snapshot일 때만, 그리고 target/source DB tuple이
  그대로일 때만 짧은 writer transaction에서 반영한다.
- 경쟁은 파일별 최대 세 번 재시도하고, 소진된 항목은 고정 reason code와 함께 반환한다.
- 파일 본문 쓰기·읽기·Markdown 파싱은 SQLite writer lock 밖에서 수행한다.

## 범위 밖

- 외부 편집기는 SQLite writer fence를 따르지 않으므로 POSIX 경로 lookup과 unlink 사이의
  극히 짧은 TOCTOU를 완전히 선형화할 수 없다. 안정 읽기, signature 검증, commit 후 재검증,
  다음 sweep 재시도로 이를 탐지·수렴시키되 kernel 수준 lease는 도입하지 않는다.
- 동일 문서의 두 API 응답이나 WebSocket event가 commit 순서와 다르게 전달되는 문제는 별도
  event sequencing 작업으로 남긴다. 이번 변경은 DB와 파일의 최종 정합성을 보장한다.
- vault 전체를 잠그거나 외부 편집을 중단시키는 운영 잠금은 도입하지 않는다.

## 검토한 접근

### 파일 쓰기 전체를 전역 writer lock에 포함

이해하기 쉽고 관리 쓰기를 직렬화하지만 큰 문서의 UTF-8 인코딩과 임시 파일 쓰기 시간이
SQLite writer lock에 포함된다. 문서 크기에 따라 다른 모든 쓰기가 멈추므로 사용하지 않는다.

### 파일 게시 뒤 조건부 clean만 수행

`UPDATE ... WHERE version=?`만 추가하면 오래된 worker가 `clean`을 설정하는 것은 막지만,
조건 검증 전에 오래된 파일을 이미 게시할 수 있다. 그 직후 process가 종료되면 최신 DB와
오래된 파일이 남으므로 불충분하다.

### 문서별 application lock

한 process 안의 경쟁은 줄이지만 여러 worker process와 CLI는 lock을 공유하지 않는다.
중단 후 남은 이동 경로도 표현하지 못한다.

### 선택: staged file + SQLite writer fence + durable cleanup intent

본문을 `.tmp`에 먼저 쓰고 fsync한 다음, 짧은 `BEGIN IMMEDIATE` 안에서 snapshot을 다시
검증하고 `os.replace`와 정리를 수행한다. 모든 관리 writer process가 같은 SQLite fence를
통과하므로 검증 뒤 더 최신 DB commit이 끼어들 수 없다. 이동 전 경로는 별도 정규화 table에
파일 세대 signature와 함께 기록해 process 중단과 다단계 이동을 복구한다.

외부 재색인은 같은 writer fence를 사용하되 파일을 정본으로 채택하기 전에 안정된 byte
snapshot을 만들고, 기존 문서·신규 target·rename source를 정확한 tuple CAS로 검증한다.

## 투영 snapshot과 파일 세대

### `ProjectionSnapshot`

한 reader SELECT에서 다음 값을 읽는다.

```text
(id, path, path_norm, version, content_hash, is_deleted, file_state,
 revision.version, revision.content_hash, revision.body)
```

revision은 `revisions.doc_id=documents.id AND revisions.version=documents.version`으로 정확히
join한다. revision이 없거나 두 hash 또는 `sha256(body)`가 다르면 손상으로 보고 파일을
건드리지 않은 채 `pending`을 유지한다. writer 안의 재검증은 큰 body를 다시 hash하지 않고
document tuple과 immutable revision의 version/content_hash를 비교한다. application write는
revision을 수정하지 않으며 같은 SQLite writer fence가 revision 교체를 막는다. snapshot
equality는 `id/path/path_norm/version/content_hash/is_deleted/file_state` 전체를 비교한다.

### `FileSignature`

파일 세대 식별자는 다음 정수 tuple이다.

```text
(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)
```

초 단위 mtime이나 content hash만으로는 동일 길이 in-place rewrite와 inode 교체를 충분히
구분하지 못한다. regular file 여부도 함께 검사한다.

## staged file publication

`_project_current(doc_id, max_attempts=3)`은 다음 순서를 따른다.

1. reader에서 현재 pending `ProjectionSnapshot`을 읽는다. 문서가 없거나 이미 clean이면
   idempotent 완료로 반환한다. `document_purge_intents`가 있으면 일반 snapshot을 만들지 않고
   `purge_pending`을 반환해 전용 finisher로 route한다.
2. canonical revision body를 vault의 전용 `.tmp`에 UTF-8로 쓰고 flush/fsync한다. target
   parent의 symlink component를 거부하고 새 directory entry도 fsync한다. `.tmp` 자체도
   `lstat`으로 실제 vault root 아래 directory인지 확인해 symlink/non-directory를 거부한다.
   `.tmp`와 target parent의 `st_dev`가 다르면 cross-device atomicity를 제공할 수 없으므로
   명시적으로 실패한다. 이 단계에는 DB writer lock이 없다.
3. writer transaction을 시작해 같은 snapshot을 다시 읽고 정확히 일치하는지 확인한다.
   `NOT EXISTS document_purge_intents(doc_id)`도 CAS 조건이다. 달라졌거나 purge intent가 생겼으면
   filesystem을 바꾸지 않고 transaction을 끝낸 뒤 최신 operation으로 재시도/route한다.
4. snapshot이 live이면 temp를 현재 live path에 `os.replace`하고 대상 directory를 fsync한다.
   같은 path의 오래된 `.trash` 사본을 제거하고 해당 directory도 fsync한다.
5. snapshot이 deleted이면 temp를 `.trash/<path>`에 `os.replace`하고 directory를 fsync한 뒤
   live path를 제거하고 그 directory를 fsync한다. 디스크의 live 본문을 휴지통으로 옮기지
   않으므로 오래된 projection이 canonical revision을 오염시키지 않는다.
6. 이 문서의 cleanup intent를 `path_norm` keyset 순서로 transaction당 최대 64개 처리한다.
   다른 document row가 그 path를 소유하면 tombstone 여부와 무관하게 namespace가 예약된
   것이므로 파일을 건드리지 않고 intent를 완료한다. 소유자가 없고 파일이 없으면 완료한다.
   파일이 intent의 expected signature와 같을 때만 unlink하고 directory를 fsync한다.
   signature가 다르면 외부/새 세대 파일로 보존하고 그 intent를 conflict로 남기되 같은 batch와
   뒤 keyset batch를 계속 처리한다.
7. 설치 직전 temp를 다시 `lstat`해 stage 때 저장한 full signature와 같은 regular file인지
   검증한다. 설치 직후 target도 `lstat`해 staged inode/dev/size와 같은지 검증하고 이
   설치 세대 signature의 `mtime_ns / 1_000_000_000`을 REAL mtime으로 사용한다. 현재 snapshot이
   여전히 같은 조건이고 cleanup
   intent가 없을 때만 `file_state='clean'`과 live `file_mtime`을 갱신한다. deleted 문서의
   `file_mtime`은 `NULL`이다. intent가 64개보다 많으면 완료 batch만 commit하고 writer lock을
   놓은 뒤 같은 snapshot의 다음 batch를 처리한다. 최종 clean 전에 canonical target을 다시
   게시·검증한다.
8. commit 뒤 남은 temp를 정리한다. 파일 작업이나 commit이 실패하면 DB는 pending이며 다음
   호출이 같은 canonical body로 idempotently 반복한다.

`os.replace`와 unlink는 writer transaction 안에 있지만 UTF-8 인코딩·temp 쓰기는 밖에 있다.
managed live/trash/cleanup 경로는 lexical parent component를 검사해 symlink/non-directory를
거부하고 target을 따라가는 기존 `safe_join().resolve()`를 filesystem mutation에 사용하지
않는다. lock 보유 시간은 짧은
metadata 검증과 최대 64개의 rename/unlink/fsync에 한정된다.

snapshot mismatch는 정상 경쟁이며 `max_attempts=3`은 이 document-state 경쟁에만 적용한다.
cleanup의 finite 64-row keyset batch loop는 retry 예산과 별개로 한 번의 stable snapshot에서
끝까지 방문한다. batch 안 signature conflict/I/O를 수집하되 뒤 row와 뒤 batch를 계속
처리하고, 성공 row만 commit하며 conflict row는 남긴다. 세 번 모두 snapshot이 바뀌면 pending을
남기고 구조화된 `target_changed` 결과를 반환한다. OSError와 revision 손상은 호출자에게
명시적으로 보고하며 startup recovery는 파일별로 기록한 뒤 다음 문서를 계속한다.

purge가 같은 version/path/hash의 clean tombstone을 다시 pending으로 만드는 ABA가 있어도 과거
projector는 intent 부재 CAS에 실패한다. `_project_current()`를 직접 부르는 reindex와 managed
callsite도 intent가 있으면 live/trash를 만지지 않는다.

## 내구적 이전 경로 정리

`SCHEMA_SQL`에 다음 의미의 두 table을 추가한다. 새 IF-NOT-EXISTS table이므로 기존 정책대로
numbered migration이나 schema version 변경은 필요하지 않다.

```text
file_projection_cleanup
  doc_id              FK documents ON DELETE CASCADE
  path
  path_norm
  expected_exists     0 | 1
  expected_dev        nullable integer
  expected_ino        nullable integer
  expected_size       nullable integer
  expected_mtime_ns   nullable integer
  expected_ctime_ns   nullable integer
  queued_version
  created_at
  PRIMARY KEY (doc_id, path_norm)

document_purge_intents
  doc_id              PRIMARY KEY, FK documents ON DELETE CASCADE
  path
  path_norm
  version
  actor
  via
  created_at
```

cleanup table은 `path_norm` lookup index를 두고, `expected_exists=0`이면 signature가 모두
NULL이며 1이면 모두 NOT NULL인 CHECK를 둔다. 손상되거나 부분적인 signature를 삭제 근거로
사용하지 않는다.

`move()`의 첫 DB transaction 안에서, 문서 path를 바꾸기 전에 이전 live path의 signature를
캡처하고 cleanup intent를 upsert한다. SQLite writer fence 안에서 캡처하므로 앞선 관리
projection이 중간에 파일 세대를 바꿀 수 없다. A→B→C는 A와 B intent를 모두 보존한다.
새 target과 같은 이 문서의 과거 intent는 삭제해 B→A 같은 되돌리기가 현재 파일을 지우지
않게 한다.

경로가 다른 DB 문서에 재사용되면 soft tombstone을 포함한 그 row가 namespace 정본
소유자다. cleanup은 intent만 지우고 현재 파일을 절대 unlink하지 않는다. reindex가 외부
파일을 새 문서로 채택할 때도 같은 `path_norm`의 과거 intent를 transaction 안에서 제거하며,
과거 문서는 후속 recovery에서 clean으로 수렴한다.

## 관리 쓰기 통합

`create`, tombstone revive, `update`, targeted edit, properties/task update, import, daily note,
`move`, `delete`, `restore`는 DB commit 뒤 로컬 body/path로 직접 `_write_file` 또는
`_trash_file`을 호출하지 않는다. 모두 `doc_id`만 `_project_current()`에 넘긴다. targeted
edit와 import처럼 이미 create/update에 위임하는 경로는 자동으로 같은 계약을 얻는다.

후처리를 기다리는 사이 더 최신 관리 쓰기가 commit되면 먼저 호출된 worker도 최신
snapshot으로 재시도해 파일을 최신 상태로 수렴시킨다. 임베딩은 기존처럼 DB revision/chunk를
정본으로 하므로 projection 성공 뒤 해당 `doc_id`를 처리하되, 문서 경쟁 시 기존 embedding
CAS가 stale 결과를 거부한다.

## delete, restore, purge

- delete DB commit은 tombstone revision과 `pending`을 먼저 남긴다. projector는 revision
  body를 휴지통에 새로 게시하고 live path를 제거한 뒤에만 clean으로 만든다.
- restore/revive DB commit은 live revision과 `pending`을 남긴다. projector는 live path를
  게시하고 같은 path의 이전 trash 사본을 제거한 뒤 clean으로 만든다.
- purge는 전용 two-phase intent를 쓴다. 처음 읽은 tombstone이 pending이면 intent 전에 일반
  deleted projector가 canonical trash 게시와 live 경로 제거까지 성공해야 한다. cleanup
  conflict가 남아 전체 result가 pending이어도 current deleted target 설치가 성공했고 첫
  writer의 exact tombstone/live path ENOENT 검증을 통과하면 intent 단계로 진행한다. 처음부터
  clean tombstone이면 그
  뒤 나타난 live 파일은 외부 세대로 간주해 이 부재 조건을 요구하지 않는다. 첫 writer가
  `document_purge_intents`를 commit하고 문서를 pending으로 만든 뒤 finisher는 live 경로를 전혀
  건드리지 않는다.
- actor/via를 intent에 보존해 중단 뒤 완료된 purge도 원 요청자 audit를 남긴다. 기존 intent가
  있으면 API retry는 이를 overwrite하거나 새 세대를 캡처하지 않고 immutable request를
  resume한다. 동시에 실행된 finisher 중 첫 transaction만 row 삭제와 audit를 commit하고,
  다음 finisher는 row/intent 부재를 성공 no-op으로 처리한다.
- purge finisher도 cleanup intent를 `path_norm` keyset으로 writer transaction당 64개씩 끝까지
  처리한다. owner/missing/exact signature는 intent를 완료하고, signature mismatch는 파일을
  보존한 채 intent를 완료하며, I/O 실패 row만 남긴다. 한 conflict가 뒤 cleanup을 굶기지
  않는다. cleanup이 모두 해소된 마지막 writer transaction만 관리 trash를 제거·fsync하고
  document row/history/intent 삭제와 audit를 commit한다.
- unlink/fsync/DB commit 실패 또는 process 중단 시 purge intent와 tombstone이 남는다.
  `recover_pending()`은 purge intent를 일반 projection보다 먼저 찾아 같은 전용 finisher로
  완료한다. restore와 tombstone revive는 purge intent가 있으면 거부하므로 영구 삭제와 복원이
  서로 추월하지 않는다.
- signature가 달라진 cleanup 경로와 intent commit 뒤 나타난 live 경로는 purge가 삭제하지
  않는다. DB tombstone이 사라진 다음 reindex가 별도 외부 문서로 가져올 수 있다.

## bounded pending recovery

`recover_pending()`은 `(id,path,body)` 전체를 미리 읽지 않는다.

- 시작 시 pending 최대 document ID를 scalar로 snapshot한다.
- `WHERE id>? AND id<=max_id AND file_state='pending' ORDER BY id LIMIT ?` keyset query로 작은
  ID page만 읽는다.
- 각 ID는 독립적으로 처리한다. purge intent가 있으면 전용 purge finisher를 먼저 호출하고,
  아니면 `_project_current()`를 호출한다. 호출 안에서 최신 tuple/body를 다시 읽으므로 page를
  얻은 뒤 update/delete/move/purge가 일어나도 stale 파일 작업을 하지 않는다.
- 성공한 실제 pending→clean 전환과 durable purge 완료만 기존 정수 반환값에 합산한다. 이미
  다른 worker가 끝낸 문서는 idempotent no-op이다.
- 한 문서의 경쟁·I/O·손상이 뒤 문서를 막지 않는다. 내부 recovery report는 reason-coded
  conflict를 보존하고 기존 `recover_pending() -> int` API는 성공 수를 반환하며 실패를 log한다.
- sweep 중 이미 지난 ID가 다시 pending이 되거나 새 ID가 생기면 다음 startup/호출에서
  처리한다. hot document가 뒤 문서를 굶기지 않는다.

## 안정적인 외부 파일 읽기

`_read_stable_markdown(path)`은 lexical symlink를 따라가지 않도록 `lstat`과 지원되는
플랫폼의 `O_NOFOLLOW`를 사용해 최대 한 attempt에서 다음을 수행한다.

```text
lstat(path, before)
open(path, "rb", no-follow)
fstat(fd, before-read)
read all bytes
fstat(fd, after-read)
lstat(path, after)
```

네 signature가 모두 같고 lexical entry와 열린 fd가 regular file이며 읽은 byte 길이가
`st_size`와 같아야 한다. managed mutation과 같은 lexical parent validator를 open 전후에
재사용해 중간 directory symlink도 거부하고, 경로가 계속 vault 아래의 같은 상대 path일 때만
bytes를 채택한다. reconcile writer의 signature CAS 직전과 commit 후 재검증에도 parent
validator를 다시 실행한다. directory를 rename한 뒤 같은 이름의 symlink로 바꿔 inode가
유지되는 alias 경합도 통과하지 못한다. 그 뒤
UTF-8을 기존 호환 동작인 `errors='replace'`로 decode한다. 외부 invalid UTF-8 파일에 대한
DB/file 동일성은 원시 byte가 아니라 이 decoded text 의미를 기준으로 한다. `file_mtime`도
별도 stat이 아니라 채택한 `mtime_ns / 1_000_000_000`에서 만든다. 불일치는 DB를 쓰지 않고
`file_changed`, 사라짐은 `file_disappeared`, I/O·symlink·non-regular 실패는
`file_unreadable`이다.

## 외부 reconcile CAS

scan은 먼저 lexical relative path를 `path_norm`으로 그룹화한다. case-sensitive filesystem에
`A.md`와 `a.md`가 함께 있는 것처럼 한 norm에 둘 이상이면 어느 것도 가져오지 않고 각 path를
`path_collision`으로 보고한다. symlink는 scan 대상 document가 아니다.

scan 전에 `_recover_pending_report()`를 실행해 missing live projection과 pending delete/purge도
먼저 수렴시키며, 실제 pending→clean 또는 purge 완료 수를 `recovered_pending`에 합산한다.
중간 conflict는 즉시 최종 skip으로 고정하지 않고 scan/후속 cleanup 뒤 남은 pending 상태를
기준으로 `pending_projection`을 결정한다.

각 발견 path는 최대 세 번 다음 과정을 반복한다.

1. target의 DB tuple
   `(id,path,path_norm,version,content_hash,is_deleted,file_state)`을 snapshot한다.
2. target이 pending이면 디스크를 DB로 가져오지 않고 `_project_current(id)`로 관리 projection을
   먼저 복구한다. 끝까지 pending이면 `pending_projection`으로 skip한다.
3. 안정된 파일 snapshot을 writer 밖에서 읽고 Markdown metadata/chunks/links 입력을 만든다.
4. 짧은 writer transaction에서 target tuple과 파일 signature를 다시 검증한다.
5. clean tombstone은 writer에서 전체 snapshot과 file signature를 다시 검증한 뒤 audit와
   `skipped_deleted`를 commit한다. 중간 restore/delete 경쟁은 target retry다. 관리 delete/purge가
   scanned 파일을 정상 제거해 retry의 lexical path가 ENOENT이면 superseded된 정상 종료이며
   `file_disappeared`나 tombstone skip으로 세지 않는다.
6. 기존 live 문서는 모든 snapshot column을 WHERE 조건에 둔 UPDATE rowcount가 1일 때만 새
   revision, tags, FTS, chunks, links, audit를 같은 transaction에 반영한다. unchanged 파일의
   mtime 갱신도 같은 CAS를 사용한다. DB path와 disk path가 대소문자/철자만 다르면 unchanged가
   아니라 exact-CAS rename revision으로 실제 path를 갱신한다.
7. target이 없으면 **rename 후보보다 먼저** 같은 path의 cleanup intent를 확인한다. 현재 file
   signature와 같은 intent가 있으면 reconcile writer 밖에서 owner `_project_current()`를
   실행하고 target을 처음부터 retry한다. projector가 의도대로 stale file을 지운 경우는
   `file_disappeared` conflict가 아니라 정상 cleanup이다.
8. 신규 문서는 snapshot과 transaction 모두 target이 없을 때만 INSERT한다. cleanup signature가
   다르면 외부 새 세대로 채택하고 같은 transaction에서 그 path의 모든 intent를 제거한다.
   commit 뒤 affected owner projector를 writer 밖에서 호출해 이번 reindex 안에 clean으로
   수렴시킨다.
9. commit 뒤 path signature를 다시 확인한다. 파일이 바뀌었으면 최신 DB/file snapshot으로
   재시도한다. 이미 반영한 외부 세대는 정상 revision으로 남고 다음 세대를 새 revision으로
   가져온다.

본문 읽기와 Markdown parsing 중에는 writer lock을 잡지 않는다. stable bytes에서 title/tags,
FTS body, chunk tuple, extracted link를 `PreparedMarkdown`으로 먼저 만든다. writer 안에서는
prepared row 치환과 현재 DB를 필요로 하는 link resolution만 수행한다. reembed가 요청되면
hash가 같은 파일도 기존 동작처럼 새 external-reconcile revision과 dirty flag를 만들되 같은
CAS를 따른다.

## 외부 rename CAS

target이 없을 때 같은 content hash를 가진 **clean live** 문서 중 현재 lexical source path가
`lstat` ENOENT로 실제 사라진 후보를 찾는다. broken symlink/permission 오류는 missing이 아니다.
유일한 후보만 rename source가 된다. 같은 hash의 pending **live/missing** source가 있으면 관리
move projection일 수 있으므로 create/rename을 하지 않고 `pending_projection`으로 재시도한다.
초기 후보는 disk norm set과 문서 한 번의 O(D) scan으로 map을 만들고 같은 reader connection의
`PRAGMA data_version`을 map generation으로 보관한다. target-absent writer 안에서 generation이
그대로일 때만 rename 또는 INSERT를 허용한다. 다른 connection의 commit으로 generation이
바뀌면 미처리 target을 다음 global round로 미루고 현재 disk/DB map을 다시 만든다. reindex
자체 commit은 in-memory map에 반영한다. global round는 최대 세 번이어서 정상/경쟁 복잡도는
`O(3(F + D))`이고, 세 번 모두 바뀐 target은 중복 identity를 만들지 않고
`rename_source_changed`로 남긴다.

`data_version`은 DB generation만 보호하므로 target-absent writer의 rename/INSERT 결정 직전에
초기 map이 보관한 **같은 hash의 모든 live DB row**에 대해 lexical source 존재를 다시
검사한다. 외부 파일 삭제로 새 missing candidate가 생겼으면 최신 집합으로 unique/ambiguous를
다시 판정한다. DB generation이 같으므로 candidate ID 집합은 완전하고, filesystem 상태는 이
commit point에서 새로 수집된다. commit 직후에도 선택 source와 같은-hash source 존재를
재검증해 이후 외부 변화는 다음 work item/충돌로 넘긴다.

rename writer transaction은 한 번에 다음을 모두 검증한다.

- target이 여전히 없음
- source의 `id/path/path_norm/version/content_hash/is_deleted/file_state`가 snapshot과 같음
- source가 clean/live이고 기존 source path가 여전히 없음
- target 파일 signature가 안정 읽기 snapshot과 같음

source UPDATE도 전체 tuple을 WHERE에 둔 rowcount 1 CAS를 사용한다. 하나라도 달라지면 revision,
index, graph, audit 전체를 rollback하고 다시 찾는다. 성공 commit 뒤에만 rename count와 결과를
기록한다. 별도 선행 `claimed` set은 필요하지 않으며, 첫 성공 뒤 source의 새 path가 디스크에
존재하므로 다음 target의 missing-source 후보에서 자연스럽게 제외된다. source absence는
transaction의 UPDATE 직전과 commit 직후 다시 확인한다. commit 뒤 source가 다시 나타나면 그
path를 bounded work queue에 넣어 새 외부 세대로 reconcile한다. 끝까지 불안정하면
`rename_source_reappeared`다.

성공한 rename도 target path의 모든 cleanup intent를 같은 transaction에서 제거하고 affected
owner ID를 반환한다. commit 뒤 writer 밖에서 각 owner를 project한다. rename source 자신이
과거 target intent owner인 경우도 현재 path를 지우거나 영구 pending으로 남지 않는다.

scan 종료 뒤 cleanup intent를 해소한 owner를 writer 밖에서 다시 project하고 bounded recovery를
한 번 더 실행한다. 최초 recovery에서 실패했어도 최종적으로 clean/purged된 ID는 conflict에서
제외한다. `missing_files`는 초기 disk snapshot을 재사용하지 않고 최신 DB exact path를 lexical
`lstat`해 ENOENT인 clean live 문서만 계산한다. regular file은 present, ENOENT는
`missing_files`, symlink/non-regular/EACCES/기타 I/O는 `file_unreadable` conflict로 분류한다.
최종 남은 pending/purge intent는 `pending_projection` conflict로 합친다.

## 결과와 CLI

기존 `created`, `updated`, `renamed`, `renames`, `unchanged`, `missing_files`,
`skipped_deleted`, `embedded`를 유지하고 다음을 추가한다.

```python
{
    "recovered_pending": 0,
    "retried": 0,
    "skipped_conflicts": [
        {"path": "notes/a.md", "reason": "target_changed", "attempts": 3}
    ],
}
```

reason code는 외부 계약으로 고정한다.

- `file_changed`
- `file_disappeared`
- `file_unreadable`
- `target_changed`
- `pending_projection`
- `rename_source_changed`
- `rename_source_reappeared`
- `path_collision`

내부 managed projection report는 `cleanup_changed`, `projection_corrupt`, `purge_failed`를
추가로 사용할 수 있으며 reindex 최종 미수렴은 공개 `pending_projection`으로 정규화한다.

`retried`는 첫 시도 이후 실제 추가 시도 수다. 한 path가 여러 안정 외부 세대를 성공 commit한
경우 최종 분류 counter는 path당 한 번만 올리고 audit/revision은 실제 반영 세대마다 남긴다.
분류 우선순위는 `renamed > created > updated > unchanged`이고 `renames`도 path당 한 번이다.
재시도 중 일부를 반영했지만 최종 snapshot이 끝까지 안정되지 않으면 분류 counter와
`skipped_conflicts`가 함께 존재할 수 있다.

CLI는 recovered/retried/skipped 수와 각 skipped path/reason/attempts를 출력한다.

- 완전한 reconcile, `missing_files`, tombstone의 의도적 skip: exit 0
- 재시도 소진으로 `skipped_conflicts`가 하나라도 있음: exit 1
- argparse/config 오류: 기존 exit 2

## 테스트 전략

### 관리 projection

- 같은 문서 update 두 개의 파일 게시 순서를 역전해도 최종 파일/DB가 최신 version인지 검증한다.
- snapshot 검증 뒤 다른 managed writer가 끼지 못하고 이전 staged temp가 최신 파일을 덮지
  못하는지 두 `Database` instance로 검증한다.
- A→B 중단과 A→B→C 연속 이동 뒤 recovery가 모든 이전 경로를 제거하는지 검증한다.
- B→A 되돌리기와 이전 A 경로를 새 문서가 재사용한 경우 새 파일을 보존하는지 검증한다.
- cleanup signature가 달라진 외부 파일은 보존되고 문서가 pending인지 검증한다.
- delete/update, delete/restore, restore/delete 후처리를 역전해도 canonical live/trash 한쪽만
  남는지 검증한다.
- projection의 replace/unlink/commit 실패 뒤 pending과 revision이 남고 다음 recovery가
  수렴하는지 검증한다.
- purge unlink 실패 rollback, 파일 삭제 뒤 DB commit 실패 복구, 외부 live 파일 보존을
  검증한다.
- 여러 keyset page, hot first document, 문서별 I/O 실패에서도 뒤 pending 문서가 복구되는지
  검증한다.

### 외부 reindex

- 읽는 동안 atomic replace와 같은 길이 in-place rewrite가 signature 불일치로 탐지되는지
  검증한다.
- 안정 읽기 직후 웹/MCP update가 commit되어도 최신 DB와 파일이 되돌아가지 않는지 검증한다.
- pending 관리 문서의 이전 disk 본문을 가져오지 않고 projection을 먼저 복구하는지 검증한다.
- 기존/unchanged/new target의 tuple CAS 실패가 revision/index/audit를 남기지 않고 재시도하는지
  검증한다.
- rename source의 동시 edit/move/delete와 target 동시 create를 전체 rollback하는지 검증한다.
- source path가 다시 나타난 rename을 거부하고, ambiguous hash는 기존처럼 신규 생성하는지
  검증한다.
- cleanup-before-rename, case-only rename, 한 norm의 대소문자 충돌, symlink 중복을 검증한다.
- tombstone skip과 restore/delete 경쟁이 exact 검증과 audit를 함께 rollback하는지 검증한다.
- 최초 recovery 실패가 scan 뒤에도 pending이면 CLI conflict로 전달되고, cleanup owner가 같은
  실행에서 수렴하면 오래된 conflict가 제거되는지 검증한다.
- 세 번 연속 파일/target 변경 시 DB를 stale snapshot으로 덮지 않고 구조화된 skip을
  반환하는지 검증한다.
- CLI가 부분 실패에 1, missing/tombstone 경고에 0을 반환하는지 검증한다.
- 기존 외부 create/update/rename, tombstone 보호, reembed, audit 동작을 유지한다.

## 성능과 수용 조건

- 관리 쓰기의 temp 본문 쓰기는 lock 밖이고 writer transaction은 한 문서의 검증과 filesystem
  rename/unlink/fsync를 넘지 않는다.
- recovery ID query는 설정한 page 크기를 넘지 않고 메모리는 문서 하나의 body와 pending ID
  page에 제한된다.
- reindex 정상 경로는 파일당 본문 한 번 읽기와 stat/fstat 검증만 추가되며 전체 복잡도는
  기존과 같은 `O(F + D)`다.
- 관리 쓰기 경쟁이 멈춘 뒤 한 번의 정상 recovery로 모든 pending 문서가 최신 DB revision의
  live/trash 파일과 clean 상태로 수렴한다.
- 동시 외부 writer가 없는 clean 전환 시점에 DB path/version/hash, exact revision body,
  projected file의 decoded text가 일치한다.
- 이전 이동 경로는 지워졌거나 현재 활성 소유자의 파일로 보존되며 stale worker가 새 소유자의
  파일을 삭제하지 않는다.
- 외부 reconcile은 안정 파일 snapshot과 정확한 DB CAS가 모두 성공한 세대만 revision으로
  commit한다.
- 기존 쓰기·검색·worker·snapshot·reindex 테스트와 전체 품질 gate가 통과한다.
