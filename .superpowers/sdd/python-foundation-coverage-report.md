# Python 기반 모듈 커버리지 보고서

## RED 기준선

재현 명령은 기존 순수 단위 테스트 24개를 대상으로 아래 8개 모듈을 `--cov-branch`로 측정했다.

```bash
uv run pytest tests/test_config.py tests/test_config_resilience.py tests/test_events.py \
  tests/test_request_id.py::test_filter_defaults_to_dash_then_reflects_binding \
  tests/test_request_id.py::test_new_request_id_is_unique_and_short \
  tests/test_request_id.py::test_route_label_bounds_unmatched_paths \
  tests/test_request_id.py::test_route_label_uses_matched_route_template \
  tests/test_request_body_limit.py::test_content_length_rejected_before_inner_app \
  tests/test_request_body_limit.py::test_streamed_body_rejected_at_actual_limit \
  tests/test_request_body_limit.py::test_body_at_limit_is_allowed \
  tests/test_request_body_limit.py::test_non_http_scope_passes_through_unchanged \
  tests/test_request_body_limit.py::test_stream_overflow_after_response_start_does_not_send_second_start \
  tests/test_request_body_limit.py::test_default_request_limit_exceeds_attachment_limit \
  --cov=llm_wiki.config --cov=llm_wiki.util --cov=llm_wiki.events \
  --cov=llm_wiki.logconf --cov=llm_wiki.ratelimit --cov=llm_wiki.metrics \
  --cov=llm_wiki.services.errors --cov=llm_wiki.web.security \
  --cov-branch --cov-report=term-missing
```

결과: `24 passed`, 대상 합계 519 statements 중 198 miss, 104 branches 중 5 partial, 총 57%.

| 모듈 | Statements | Miss | Branches | Partial | 기준선 |
|---|---:|---:|---:|---:|---:|
| `config.py` | 95 | 7 | 16 | 2 | 92% |
| `util.py` | 85 | 63 | 32 | 0 | 19% |
| `events.py` | 51 | 14 | 6 | 1 | 67% |
| `logconf.py` | 32 | 1 | 2 | 0 | 97% |
| `ratelimit.py` | 29 | 19 | 2 | 0 | 32% |
| `metrics.py` | 56 | 23 | 2 | 0 | 57% |
| `services/errors.py` | 43 | 8 | 2 | 0 | 78% |
| `web/security.py` | 128 | 63 | 42 | 2 | 46% |

## 추가 테스트

`tests/test_foundation_coverage.py`에 다음 관찰 가능한 계약을 추가했다.

- 설정값 경계와 디렉터리 생성 성공/OS 오류 변환을 검증했다.
- 시간·해시·단어수·IP·문서/폴더 경로·파일명 헤더·vault 이탈 방지를 검증했다.
- 이벤트 fan-out, 닫힌 루프 무시, 큐 포화 드롭 메트릭과 경고 throttling을 검증했다.
- 로깅 기본값, sliding-window rate limit의 임계/만료/reset 상태를 검증했다.
- 인덱스 gauge의 DB 반영, Prometheus 렌더와 성공/예외 HTTP 계측을 검증했다.
- 구조화 오류의 기본/복구 힌트 직렬화 및 인스턴스 override를 검증했다.
- CSP/CSRF의 안전·면제·same-origin·헤더·폼·거부 경로를 검증했다.
- body limit의 잘못된 Content-Length, disconnect, 예외 전파/억제와 request-id의 ASGI 전달·로그·context 정리를 검증했다.
- 보안 헤더의 기본값, 기존 값 보존, 선택적 HSTS를 검증했다.

OS·라이브러리 경계 fault injection은 `Path.mkdir`, monotonic clock, event loop scheduling,
DB reader/get_meta, ASGI receive/send를 monkeypatch 또는 작은 fake로 대체했으며, 반환값·상태·로그·
메트릭·예외 및 context 정리를 각각 assertion했다. production 코드는 변경하지 않았다.

## 최종 커버리지

집중 검증 결과: `56 passed`, 519/519 statements, 104/104 branches, partial 0, 총 100%.

| 모듈 | Statements | Miss | Branches | Partial | 최종 |
|---|---:|---:|---:|---:|---:|
| `config.py` | 95 | 0 | 16 | 0 | 100% |
| `util.py` | 85 | 0 | 32 | 0 | 100% |
| `events.py` | 51 | 0 | 6 | 0 | 100% |
| `logconf.py` | 32 | 0 | 2 | 0 | 100% |
| `ratelimit.py` | 29 | 0 | 2 | 0 | 100% |
| `metrics.py` | 56 | 0 | 2 | 0 | 100% |
| `services/errors.py` | 43 | 0 | 2 | 0 | 100% |
| `web/security.py` | 128 | 0 | 42 | 0 | 100% |

## 검증

- 위 집중 coverage 명령: `56 passed`, 대상 statement+branch 100%.
- `uv run pytest tests/test_foundation_coverage.py tests/test_config.py tests/test_config_resilience.py tests/test_events.py tests/test_request_id.py tests/test_request_body_limit.py tests/test_quickwins.py tests/test_shortlist.py`: `78 passed`, 경고 1건.
- `uv run pytest`: `854 passed`, 경고 1건.
- `uv run ruff check .`: `All checks passed!`
- `uv run mypy src/llm_wiki`: `Success: no issues found in 30 source files`
- `git diff --check`: 통과.

## 커밋

- 메시지: `기반 모듈 커버리지 완성`
- 이 보고서와 집중 테스트를 함께 포함하는 단일 커밋이다.

## 우려

- 기존 테스트의 `starlette.testclient` import에서 `httpx2` 전환을 권고하는 deprecation 경고 1건이 남아 있다. 이번 범위의 실패는 아니다.
