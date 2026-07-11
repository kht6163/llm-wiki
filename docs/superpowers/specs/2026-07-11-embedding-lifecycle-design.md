# 임베딩 수명주기 안정화 설계

## 배경

현재 `reindex --reembed`는 벡터 테이블을 비운 뒤 실제로 디스크에서 다시 읽은
문서만 `vector_dirty=1`로 만든다. 재바인딩 직후 프로세스가 종료되거나 vault에
파일이 없는 활성 DB 문서가 있으면 `vector_dirty=0`인데 벡터는 없는 상태가 남고,
다음 기동의 복구 스윕도 이 문서를 찾지 못한다.

또한 `embed_pending()`은 모든 dirty 문서와 chunk, 입력 문자열, 결과 벡터를 한꺼번에
메모리에 적재한 뒤 하나의 writer transaction에서 저장한다. 저장소 크기가 커질수록
메모리 사용량과 SQLite writer lock 시간이 함께 증가한다. 현재 경쟁 조건 검사는
`(chunk_id, text)`만 비교하므로 실제 입력에 포함되는 `heading_path`만 바뀌거나, 같은
차원의 다른 모델로 재바인딩되는 중 이전 worker가 끝나는 경우도 구분하지 못한다.

## 목표

- 재바인딩 commit 직후부터 모든 활성 문서가 자동 복구 가능한 dirty 상태가 된다.
- process가 기동 때 고정한 model, dimension, 입력 pipeline, epoch가 모두 같은 경우에만
  벡터를 쓰거나 읽을 수 있다.
- 재바인딩 epoch가 바뀌면 이전 encode 결과는 publish하거나 dirty를 지울 수 없다.
- dirty sweep의 메모리와 writer transaction 범위를 전체 corpus가 아닌 문서 하나로
  제한하고, 모델 forward 입력은 설정한 chunk batch를 넘지 않는다.
- `vector_dirty=0`은 현재 ordered passage input과 현재 embedding binding의 모든 chunk
  벡터가 한 transaction에서 publish되었음을 뜻한다. version은 publish 경쟁을 막는 CAS
  token이지만, 입력을 바꾸지 않는 metadata 편집은 기존 완성 벡터를 그대로 쓸 수 있다.
- 한 문서에서 경쟁 조건이 발생해도 다른 dirty 문서는 같은 sweep에서 계속 처리한다.

## 검토한 접근

### 전체 유지보수 잠금

재바인딩과 전체 재색인 동안 모든 웹/MCP 쓰기를 막으면 이해하기 쉽지만, 큰 vault에서
서비스 중단 시간이 모델 계산 시간에 비례한다. 외부 편집기도 이 잠금을 따르지 않으며,
일반 임베딩 backlog 처리까지 같은 방식으로 직렬화하기 어렵다. 이번 변경에는 사용하지
않는다.

### 전체 corpus 단일 transaction

현재 구현과 가장 가깝지만 메모리와 writer lock이 corpus 크기에 비례하고, 중간 성공을
내구성 있게 남기지 못한다. 유지하지 않는다.

### chunk 단위 즉시 publish

메모리를 엄격한 chunk batch로 제한할 수 있지만 한 문서의 일부 새 벡터가 검색에 먼저
노출된다. 이를 안전하게 하려면 staging generation이나 vector 검색의 완성 generation
필터가 추가로 필요하다. 이번 단계에서는 복잡도 대비 이득이 작다.

### 선택: 문서 단위 원자 publish + keyset sweep

한 문서의 입력을 chunk batch로 encode한 뒤, 그 문서의 벡터만 짧은 transaction에서
검증·교체한다. 메모리는 가장 큰 문서 하나, writer lock은 한 문서의 벡터 교체로
제한된다. 문서 단위 transaction 때문에 검색에는 이전 완성본 또는 새 완성본만 보인다.
dirty 문서 ID는 keyset pagination으로 제한된 수만 읽으며 한 sweep에서 ID마다 최대 한
번만 시도한다.

## embedding binding

현재 meta의 `embedding_model`, `embedding_dim`에 다음 두 키를 추가한다.

- `embedding_pipeline`: 코드가 만드는 passage 입력 의미의 버전. 현재 값은
  `passage-input-v1`이다. 기존 chunk row를 passage/query vector로 변환하는 `_embed_text`,
  E5 prefix, normalization 또는 distance metric 의미가 달라지면 이 값을 올린다.
  chunker 자체의 의미 변경은 기존 DB chunk만으로 복구할 수 없으므로 별도 index schema와
  vault reconciliation 변경으로 다룬다.
- `embedding_epoch`: 재바인딩 세대의 양의 정수. `rebind_model()` 호출마다 model이나
  dimension이 같아도 1 증가한다.

별도 schema migration은 필요하지 않다. 기존 DB에서 model/dimension이 현재 설정과 맞고
pipeline과 epoch가 **둘 다** 없는 기존 형태일 때만 `initialize()`가 현재 pipeline과
epoch 1을 기록한다. 둘 중 하나만 없으면 부분 손상으로 간주해 거부한다. 이는 현재 코드로
만든 기존 DB를 중단 없이 받아들이되 모호한 binding을 받아들이지 않기 위한 bootstrap이다.
기존 벡터가 현재 pipeline으로 만들어졌다는 프로젝트 업그레이드 전제를 택하는 명시적인
하위 호환 절충이며, 임의의 더 오래된 pipeline을 자동 판별할 수 있다는 뜻은 아니다.

`initialize(model, dim, pipeline)`은 다음을 검증한다.

- `dim > 0`
- 저장된 model이 요청 model과 같음
- 저장된 dimension이 유효한 정수이고 요청 dimension과 같음
- 저장된 pipeline이 요청 pipeline과 같음
- 이미 존재하는 `chunk_vectors` 선언 dimension이 meta/request와 같음

vector table이 없으면 binding 기록과 같은 transaction에서 생성하고 모든 활성 문서를
dirty로 만든다. 기존 meta는 있지만 table만 유실된 상태도 다음 sweep으로 복구되어야 한다.

model, dimension, pipeline 중 하나라도 다르면 serve를 거부하고
`llm-wiki reindex --reembed`를 안내한다. pipeline/epoch 단독 누락이나 잘못된 숫자 값은
손상된 binding으로 취급해 거부한다.

`rebind_model(model, dim, pipeline)`은 하나의 `BEGIN IMMEDIATE` transaction에서 다음을
수행한다.

1. 기존 epoch를 읽고 다음 epoch를 계산한다.
2. `chunk_vectors`를 drop/recreate한다.
3. 모든 활성 문서를 `vector_dirty=1`, 삭제 문서를 `vector_dirty=0`으로 만든다.
4. model, dimension, pipeline, 새 epoch를 함께 기록한다.

어느 단계에서든 실패하면 기존 벡터 테이블, meta, dirty flag가 함께 rollback된다.
commit 후 vault 파일 유무나 후속 reindex 성공 여부와 관계없이 다음 startup/worker sweep이
DB의 기존 chunk로 모든 활성 문서를 복구할 수 있다.

`initialize()`와 `rebind_model()`은 immutable `EmbeddingBinding(model, dim, pipeline,
epoch)`을 반환하고 `Database` 인스턴스에도 process expected token으로 보관한다. publisher가
매번 DB의 최신 epoch를 새로 채택해서는 안 된다. encode 시작 때 이 local token을 캡처하고
publish transaction에서 DB meta와 비교해야, 다른 process의 rebind 이후 구 process가 새
epoch를 뒤늦게 받아 stale 결과를 쓸 수 없다.

`EmbeddingBindingChanged`는 문서 CAS 실패와 구분되는 전역 오류다. 새 코드의 모든
publisher는 model/dimension/pipeline/epoch 불일치에서 이 오류를 발생시켜 현재 sweep을 즉시
중단한다. 문서 하나의 version/input 경쟁만 `False`로 처리한다. 재바인딩은 구 버전
process를 종료한 상태에서 수행하는 운영 명령으로 유지하며, current-code process끼리는 이
fencing으로 이미 encode 중인 이전 결과도 새 세대에 publish할 수 없다.

## vector read fencing

같은 차원의 다른 모델로 다른 process가 rebind하면 실행 중인 서버의 query embedder도
즉시 stale해진다. write fencing만으로는 새 passage vector를 구 query vector로 검색하는
조용한 오염을 막을 수 없으므로 vector read에도 process expected token을 적용한다.

- hybrid/vector search는 local expected token을 먼저 캡처하고 query vector를 계산한다.
- 그 뒤 SQLite read transaction을 시작해 DB binding 전체가 expected token과 같은지
  검증하고, KNN과 결과 해석을 같은 read snapshot 안에서 끝낸다.
- query encode 중 rebind가 끝났으면 snapshot 검증이 실패한다. snapshot 시작 뒤 rebind가
  일어나면 현재 요청은 이전 table/binding의 일관된 WAL snapshot을 사용하고 다음 요청부터
  실패한다.
- 저장 vector만 사용하는 related-documents도 source vector 조회와 모든 KNN을 한 binding
  검증 read snapshot에 묶는다.
- `/readyz`는 local expected token과 DB meta가 다르면 503과 binding 불일치 상태를 반환한다.

BM25 전용 검색은 vector 의미에 의존하지 않으므로 계속 수행할 수 있지만, readiness는 stale
process를 트래픽에서 제거하도록 503을 유지한다.

## 문서 임베딩 publish

`embed_doc()`은 성공 여부를 `bool`로 반환한다. 기존 `None` 반환과
`embed_pending(doc_id=...)`의 무조건 1 반환을 실제 publish 결과로 바로잡는 의도적인 내부
API 변경이며, 저장소 밖의 공개 API에는 노출되지 않는다.

1. 한 SELECT snapshot에서 활성/dirty 상태, document version과 ordered chunk
   `(id, passage_input)`을 읽는다.
2. process local expected binding이 현재 `Embedder`의 model/dimension과 코드 pipeline에
   맞는지 확인하고 immutable token으로 캡처한다.
3. chunk를 `batch_size` 이하로 나눠 transaction 밖에서 encode한다.
4. 각 batch의 출력 개수와 모든 벡터 dimension을 검증한다. 불일치는 오류로 보고하고
   DB를 변경하지 않는다. 모든 vector serialization도 writer transaction 전에 끝낸다.
5. writer transaction에서 DB meta가 캡처한 네 binding 값과 같은지 먼저 검증하고,
   document version, 활성/dirty 상태, 현재의
   ordered `(id, passage_input)` 전체가 snapshot과 같은지 다시 확인한다.
6. 모두 같을 때만 그 문서의 기존 벡터를 교체하고
   `WHERE id=? AND version=? AND vector_dirty=1 AND is_deleted=0` 조건으로 dirty를 지운다.

문서 version/input 검증 실패는 경쟁 조건을 뜻하므로 `False`를 반환하고 dirty를 유지한다.
존재하지 않거나 이미 clean/deleted인 문서도 `False`다. binding 불일치는
`EmbeddingBindingChanged`, embedder 출력 계약 위반은 명시적 오류를 발생시켜 운영상 전역
실패가 보이게 한다. chunk가 없는 활성 dirty 문서는 같은 CAS로 정상 clean 처리한다.

`passage_input` 전체를 비교하므로 rowid가 재사용되고 본문 `text`가 같더라도
`heading_path`가 달라진 stale 결과는 publish되지 않는다.

## bounded dirty sweep

`embed_pending()`은 다음과 같이 동작한다.

- `doc_id`가 지정되면 동일한 `embed_doc()` 경로를 사용하고 실제 clean 성공 시에만 1을
  반환한다.
- 전체 sweep 시작 시 대상의 최대 document ID와 진행률용 dirty document/chunk count를
  scalar로 snapshot한다.
- `WHERE id > ? AND id <= max_id ORDER BY id LIMIT doc_batch_size` keyset query로 dirty 활성
  문서 ID를 읽는다.
- 각 ID를 `embed_doc()`으로 독립 처리하며 실제 CAS 성공만 합산한다.
- 경쟁으로 실패한 문서는 다음 sweep에 남기고 뒤의 ID 처리를 계속한다.
- sweep 중 이미 지나간 ID가 다시 dirty가 되거나 새 ID가 생기면 다음 worker wake/idle
  sweep에서 처리한다. 하나의 hot document가 뒤 문서를 굶기지 않는다.

진행률 callback은 encode batch마다 누적 chunk 수와 시작 시점의 전체 chunk 수를 받는다.
경쟁 중 chunk 수가 변할 수 있으므로 전체 값은 진행 표시용 snapshot이며, 정상 종료 때
완료 callback을 한 번 보장한다.

문서 하나가 매우 큰 경우 그 문서의 결과 벡터는 publish 전까지 메모리에 남는다. 이는
부분 세대 노출 없이 얻는 의도적인 상한이다. 향후 실제 사용 데이터에서 단일 문서 크기가
문제가 되면 staging generation을 별도 설계한다.

## CLI와 복구

`build_context(full=True)`와 `reindex --reembed` 모두 현재 pipeline 값을 DB API에 전달하고
반환된 process expected token을 보관한다.
CLI는 먼저 원자적 rebind를 수행한다. 이후 vault reconciliation이나 embedding이 중단되어도
모든 활성 문서는 이미 dirty이므로 다음 정상 기동의 `docs.embed_pending()`만으로 복구된다.
vault에 파일이 없는 활성 문서도 DB chunk를 사용하므로 빠지지 않는다.

`reindex_all(reembed=True)`가 unchanged 파일의 revision/version을 갱신하는 기존 동작은 이번
변경에서 유지한다. 파일 reconciliation의 version-aware CAS와 투영 복구는 다음 독립
작업에서 다룬다.

## 테스트 전략

- 재바인딩 직후 vector table이 비고 모든 활성 문서만 dirty인지 검증한다.
- 재바인딩 중 dirty update를 강제로 실패시켜 table/meta/flags가 모두 rollback되는지
  검증한다.
- 같은 model의 dimension 변경과 pipeline 변경을 startup이 거부하는지 검증한다.
- 기존 DB의 누락 pipeline/epoch bootstrap을 검증한다.
- pipeline/epoch 중 하나만 누락된 부분 binding은 거부하는지 검증한다.
- 재바인딩 직후 중단을 모사하고 startup sweep만으로, 파일이 없는 활성 문서까지 벡터가
  복구되는지 검증한다.
- 여러 document page를 통과해 backlog가 모두 drain되는지 검증한다.
- encoder 호출 입력 수가 `batch_size`를 넘지 않는지 검증한다.
- 첫 문서 publish 후 다음 문서 encode가 실패하면 앞 문서는 clean, 뒤 문서는 dirty로
  남는지 검증한다.
- encode 중 heading path 또는 version이 바뀌면 해당 문서만 dirty로 남는지 검증한다.
- encode 중 epoch가 바뀌면 이전 벡터가 publish되지 않는지 검증한다.
- 두 `Database` 인스턴스로 같은 차원 rebind를 실행한 뒤 구 process publisher가 최신 epoch를
  새로 채택하지 않고 중단하는지 검증한다.
- 구 process의 hybrid/vector 검색과 readiness는 binding 변경을 감지하고, related 검색은
  한 read snapshot에서만 vector generation을 사용하는지 검증한다.
- 출력 개수/dimension 불일치 시 DB가 바뀌지 않는지 검증한다.
- `doc_id` 단건 경로가 실제 CAS 실패 시 0을 반환하는지 검증한다.

## 수용 조건

- rebind 반환 시점의 상태는 `빈 vector table + 새 binding/epoch + 모든 활성 문서 dirty`로
  원자적이다.
- rebind 이후 어느 지점에서 프로세스가 종료되어도 다음 정상 startup sweep으로 모든 활성
  DB chunk가 복구된다.
- `vector_dirty=0`인 활성 문서에는 현재 ordered input/binding의 모든 chunk 벡터가 있다.
- 이전 epoch/model/pipeline의 encode 결과는 새 세대에 publish되거나 dirty를 지우지 못한다.
- stale process는 새 generation을 vector 검색에 사용하지 않고 readiness가 503이 된다.
- 모델 forward 입력은 `batch_size`, ID query는 `doc_batch_size`, writer transaction은 문서
  하나를 넘지 않는다.
- 한 문서의 경쟁 실패가 같은 sweep의 나머지 문서 처리를 막지 않는다.
- 기존 검색·쓰기·worker·reindex 테스트와 전체 품질 게이트가 통과한다.
