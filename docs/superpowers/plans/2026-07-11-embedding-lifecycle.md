# 임베딩 수명주기 안정화 구현 계획

> 설계 정본: `docs/superpowers/specs/2026-07-11-embedding-lifecycle-design.md`

**목표:** 임베딩 재바인딩을 중단 복구 가능하게 만들고, process epoch fencing과 문서 단위
bounded sweep으로 stale/부분 벡터 publication 및 corpus 크기 비례 writer lock을 제거한다.

**구조:** 새 `embedding_contract` 모듈이 immutable binding과 검증 오류를 정의한다.
`Database`는 initialize/rebind 때 받은 binding을 process-local expected token으로 고정한다.
publisher는 이 token과 문서 passage-input snapshot을 writer transaction에서 검증하며,
검색은 query encode 뒤 binding 검증과 KNN을 하나의 read snapshot에 묶는다.

**기술:** Python 3.12, SQLite WAL/sqlite-vec, pytest, ruff, mypy

---

## Task 1: embedding binding 계약과 기동 검증

**파일**

- 생성: `src/llm_wiki/embedding_contract.py`
- 수정: `src/llm_wiki/db.py`
- 수정: `src/llm_wiki/embedding.py`
- 수정: `src/llm_wiki/runtime.py`
- 생성: `tests/test_embedding_binding.py`

### 1. 실패 테스트 작성

다음을 `tests/test_embedding_binding.py`에 추가한다.

- fresh initialize가 `EmbeddingBinding(model, dim, passage-input-v1, 1)`을 반환하고 DB
  instance의 expected token으로 보관한다.
- model/dim meta만 있는 legacy DB는 pipeline+epoch를 함께 bootstrap하고 기존 vector를
  보존한다.
- pipeline/epoch 중 하나만 없거나 epoch/dim이 숫자가 아니면 손상된 binding으로 거부한다.
- 같은 model 이름의 dimension 변경과 pipeline 변경을 `reindex --reembed` 안내와 함께
  거부한다.
- sqlite master의 실제 vector dimension과 meta가 다르면 거부한다.
- vector table만 없으면 원자적으로 다시 만들고 모든 활성 문서를 dirty로 만든다.

실행:

```bash
uv run pytest tests/test_embedding_binding.py -q
```

예상: 새 계약/API가 없어 실패.

### 2. 최소 구현

`embedding_contract.py`에 다음을 정의한다.

```python
EMBEDDING_PIPELINE = "passage-input-v1"

@dataclass(frozen=True)
class EmbeddingBinding:
    model: str
    dim: int
    pipeline: str
    epoch: int

class EmbeddingBindingChanged(RuntimeError):
    pass
```

`Database.initialize(model, dim, pipeline)`은 meta 네 키와 `sqlite_master.sql`의
`embedding float[N]`을 검증하고 binding을 반환·보관한다. legacy bootstrap은
pipeline/epoch가 둘 다 없을 때만 허용한다. table이 없으면 같은 writer transaction에서
생성하고 활성 문서를 dirty로 만든다. `runtime.build_context(full=True)`는 현재 pipeline을
명시적으로 전달한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_embedding_binding.py tests/test_reindex.py -q
uv run ruff check src/llm_wiki/embedding_contract.py src/llm_wiki/db.py src/llm_wiki/embedding.py src/llm_wiki/runtime.py tests/test_embedding_binding.py
git add src/llm_wiki/embedding_contract.py src/llm_wiki/db.py src/llm_wiki/embedding.py src/llm_wiki/runtime.py tests/test_embedding_binding.py
git commit -m "임베딩 바인딩 기동 검증 강화"
```

## Task 2: 원자적 rebind와 epoch fencing 기반

**파일**

- 수정: `src/llm_wiki/db.py`
- 수정: `src/llm_wiki/_cli_impl.py`
- 수정: `tests/test_embedding_binding.py`
- 수정: `tests/test_reindex.py`

### 1. 실패 테스트 작성

- 같은 tuple로 rebind해도 epoch가 1 증가한다.
- rebind 직후 vector table은 비고 활성 문서는 모두 dirty, 삭제 문서는 clean이다.
- dirty update를 실패시키는 SQLite trigger를 설치하면 vector table/meta/dirty가 모두 기존
  상태로 rollback된다.
- 별도 `Database` instance A가 epoch 1을 기대하는 동안 B가 같은 dimension으로 rebind하면
  A의 expected token은 epoch 1에 고정되고 B만 epoch 2를 보관한다.
- 기존 rebind/reindex 테스트가 pipeline 인자를 포함한 계약에서도 유지된다.

실행:

```bash
uv run pytest tests/test_embedding_binding.py tests/test_reindex.py -q
```

### 2. 최소 구현

`rebind_model(model, dim, pipeline)`을 한 writer transaction으로 바꾼다.

```text
read/validate old epoch
DROP + CREATE chunk_vectors
UPDATE documents SET vector_dirty = CASE WHEN is_deleted=0 THEN 1 ELSE 0 END
write model/dim/pipeline/epoch+1
```

commit 후 새 immutable token을 instance에 보관하고 반환한다. CLI `--reembed`는 현재 pipeline을
전달한다. `Database.expected_embedding_binding()`과 transaction 안에서 meta 전체를 비교하는
`verify_embedding_binding(conn, expected)`을 추가한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_embedding_binding.py tests/test_reindex.py -q
uv run ruff check src/llm_wiki/db.py src/llm_wiki/_cli_impl.py tests/test_embedding_binding.py tests/test_reindex.py
git add src/llm_wiki/db.py src/llm_wiki/_cli_impl.py tests/test_embedding_binding.py tests/test_reindex.py
git commit -m "재바인딩 원자성과 세대 경계 보장"
```

## Task 3: 문서 단위 snapshot publisher

**파일**

- 수정: `src/llm_wiki/indexing.py`
- 수정: `tests/test_indexing.py`
- 수정: `tests/test_embedding_binding.py`

### 1. 실패 테스트 작성

- `embed_doc()`은 실제 clean CAS 성공만 `True`로 반환한다.
- embed 도중 document version 또는 `heading_path`만 바뀌면 vector를 쓰지 않고 `False`,
  dirty=1을 유지한다.
- 별도 DB instance가 같은 dimension으로 rebind하면 구 instance publisher가
  `EmbeddingBindingChanged`를 내고 새 table에 insert/clean하지 않는다.
- rebind가 publisher commit 뒤 실행되면 앞서 쓴 vector를 drop하고 문서를 다시 dirty로
  만든다.
- encoder 출력 개수 또는 각 vector dimension이 입력 계약과 다르면 writer 변경 없이
  명시적 오류를 낸다.
- 하나의 큰 문서는 `batch_size` 이하의 모델 호출로 나뉜다.
- chunk가 없는 활성 dirty 문서도 CAS 성공 시 clean 처리한다.

실행:

```bash
uv run pytest tests/test_indexing.py tests/test_embedding_binding.py -q
```

### 2. 최소 구현

`embed_doc(db, embedder, doc_id, batch_size=64, on_batch=None) -> bool`로 변경한다.

- 시작 시 DB instance의 expected token을 한 번 캡처하고 embedder identity를 검증한다.
- 한 read statement에서 document version/state와 ordered `(id, _embed_text(row))`을 읽는다.
- batch별 encode, 출력 count/dimension 검사, serialization을 모두 writer 밖에서 수행한다.
- writer에서 먼저 expected binding을 검증한 뒤 document state/version/ordered input 전체를
  재검증한다.
- 완전 일치할 때만 한 document의 vector를 교체하고 조건부 dirty clear를 수행한다.
- 문서 CAS 실패는 `False`, binding 실패는 전역 예외로 분리한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_indexing.py tests/test_reindex.py tests/test_embed_worker.py -q
uv run ruff check src/llm_wiki/indexing.py tests/test_indexing.py tests/test_embedding_binding.py
git add src/llm_wiki/indexing.py tests/test_indexing.py tests/test_embedding_binding.py
git commit -m "문서 임베딩 게시를 스냅샷 CAS로 보호"
```

## Task 4: keyset 기반 bounded dirty sweep

**파일**

- 수정: `src/llm_wiki/indexing.py`
- 수정: `tests/test_indexing.py`
- 수정: `tests/test_embed_worker.py`

### 1. 실패 테스트 작성

- 5개 이상의 dirty 문서를 `doc_batch_size=2`로 처리해 모두 drain한다.
- sweep 중 한 문서의 input 경쟁은 그 문서만 dirty로 남기고 뒤 문서는 계속 clean 처리한다.
- 두 번째 문서 encode가 예외를 내면 첫 문서 commit은 유지되고 이후 문서는 dirty다.
- `embed_pending(doc_id=...)`은 실제 CAS 실패 시 0, 성공 시 1을 반환한다.
- progress callback은 모델 batch마다 호출되고 정상 종료 때 완료 상태를 받는다.
- worker가 여러 ID page의 backlog를 정상적으로 drain한다.

실행:

```bash
uv run pytest tests/test_indexing.py tests/test_embed_worker.py -q
```

### 2. 최소 구현

전체 dirty 목록/입력/vector materialization과 corpus-wide writer transaction을 제거한다.
시작 시 `max_id`, dirty document/chunk count만 scalar로 읽고 다음 keyset query를 반복한다.

```sql
SELECT id
FROM documents
WHERE vector_dirty=1 AND is_deleted=0 AND id>? AND id<=?
ORDER BY id
LIMIT ?
```

각 ID를 Task 3 publisher로 독립 처리하고 성공만 합산한다. 한 sweep에서 지나간/새 ID는 다음
wake 또는 idle sweep이 처리한다. 시작 시점 chunk total을 진행 표시 snapshot으로 사용하고
정상 종료 callback을 보장한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_indexing.py tests/test_embed_worker.py tests/test_reindex.py -q
uv run ruff check src/llm_wiki/indexing.py tests/test_indexing.py tests/test_embed_worker.py
git add src/llm_wiki/indexing.py tests/test_indexing.py tests/test_embed_worker.py
git commit -m "임베딩 대기열을 문서 단위로 분할 처리"
```

## Task 5: vector read fencing과 readiness

**파일**

- 수정: `src/llm_wiki/db.py`
- 수정: `src/llm_wiki/search.py`
- 수정: `src/llm_wiki/services/documents.py`
- 수정: `src/llm_wiki/web/app.py`
- 수정: `tests/test_search_auth.py`
- 수정: `tests/test_web.py`
- 수정: `tests/test_embedding_binding.py`

### 1. 실패 테스트 작성

- 다른 DB instance가 같은 dimension의 다른 binding으로 rebind한 뒤 구 instance의
  hybrid/vector 검색은 `EmbeddingBindingChanged`로 fail closed한다.
- BM25-only 검색은 binding 변경 뒤에도 동작한다.
- query encode 중 rebind가 일어나면 KNN 전 snapshot 검증에서 실패한다.
- related-documents의 source vector 조회와 반복 KNN이 하나의 read transaction 안에서
  실행된다.
- local expected token과 meta가 다르면 `/readyz`가 503, `binding_current=false`를 반환한다.

실행:

```bash
uv run pytest tests/test_search_auth.py tests/test_web.py tests/test_embedding_binding.py -q
```

### 2. 최소 구현

`Database.embedding_read_snapshot(expected)` context manager를 추가한다. 이미 transaction이
없으면 `BEGIN`하고 expected binding을 검증한 뒤 yield하며 종료 시 rollback해 snapshot을
닫는다.

hybrid/vector search와 RAG context는 query vector를 transaction 밖에서 계산하되 local
expected token을 먼저 캡처하고, 이후 rank/KNN/result resolution 전체를 이 snapshot 안에서
수행한다. related-documents도 같은 context를 사용한다. BM25-only 경로는 일반 reader를
유지한다. readiness는 model loaded와 `embedding_binding_is_current()`가 모두 참일 때만
200을 반환한다.

### 3. 검증 및 커밋

```bash
uv run pytest tests/test_search_auth.py tests/test_search_tuning.py tests/test_web.py tests/test_embedding_binding.py -q
uv run ruff check src/llm_wiki/db.py src/llm_wiki/search.py src/llm_wiki/services/documents.py src/llm_wiki/web/app.py tests/test_search_auth.py tests/test_web.py
git add src/llm_wiki/db.py src/llm_wiki/search.py src/llm_wiki/services/documents.py src/llm_wiki/web/app.py tests/test_search_auth.py tests/test_web.py tests/test_embedding_binding.py
git commit -m "벡터 조회에 임베딩 세대 검증 적용"
```

## Task 6: 중단 복구 통합 검증과 문서 정리

**파일**

- 수정: `tests/test_reindex.py`
- 수정: `tests/test_embed_worker.py`
- 수정: 필요 시 `README.md`

### 1. 실패 테스트 작성

- 활성 문서를 만든 뒤 vault 파일을 제거하고 rebind 직후 중단을 모사한다.
- `reindex_all()` 없이 다음 startup과 동일한 `embed_pending()`만 호출해 파일 없는 문서의
  모든 vector가 복구되고 dirty가 0이 되는지 검증한다.
- rebind 후 일부 문서 embed에서 실패해도 이미 처리한 문서는 clean, 나머지는 다음 sweep으로
  복구되는지 검증한다.

### 2. 최소 구현/문서 갱신

통합 테스트가 드러낸 연결 누락만 수정한다. 운영 문서가 재바인딩 중 서비스 process 종료
전제를 설명하지 않으면 `README.md`의 모델 변경 절차에 이를 짧게 추가한다.

### 3. 전체 검증

```bash
uv run pytest -q
uv run pytest --cov=llm_wiki --cov-report=term-missing
uv run ruff check .
uv run mypy --check-untyped-defs src/llm_wiki
uv lock --check
uv build --out-dir /tmp/llm-wiki-embedding-lifecycle-dist
```

실패가 있으면 원인을 고치고 전체 명령을 다시 실행한다.

### 4. 최종 커밋과 리뷰

```bash
git add tests/test_reindex.py tests/test_embed_worker.py README.md
git commit -m "재바인딩 중단 복구 시나리오 검증"
git status --short
```

독립 reviewer에게 설계 수용 조건, 경쟁 interleaving, 테스트 누락, 회귀 위험을 검토받고
Critical/Important 지적을 모두 수정한 뒤 전체 품질 게이트를 다시 실행한다.
