# deficit-loop

**측정 가능한 목표의 "결핍"을 공급 에이전트가 하나씩 메우고, 공급과 분리된 재측정이 채택 여부를 판정하는 자율 루프 커널.**

> **English summary.** `deficit-loop` drains a goal that is defined as a *measurable list of deficits*. An oracle command prints the deficits as JSON; a pluggable supplier (a deterministic script, or any agent CLI via `DEFICIT_LOOP_AGENT_CMD`) tries to remove one deficit inside an isolated git worktree; an independent judge then **re-runs the oracle** on the integration branch with the supply merged in and adopts it only if the assigned deficit is gone, no new deficit appeared, the measured count did not shrink, and the oracle tooling was not touched. The supplier's own "I fixed it" is recorded but never trusted. The loop ends only with a recorded reason: `ACHIEVED`, `STALLED` (k attempts without progress), `BUDGET_EXHAUSTED`, `BLOCKED_EXTERNAL` (rate limit / network — not a defect), `HALTED_UNSAFE` (git state makes ownership undecidable) or `HALTED_ERROR` (the oracle or git itself failed); the three halts are pauses that `resume` reopens. State is crash-safe (atomic `state.json`, append-only `events.jsonl`, `flock`) and `resume` continues after a kill. Python 3.12, standard library only.

같은 작성자의 [evidence-gated-e2e-loop](https://github.com/tmkim0512-max/evidence-gated-e2e-loop) 와는 다른 문제를 푼다 — 그쪽은 **"AI 가 쓴 테스트를 증거로만 승인한다"**(승인 게이트), 이쪽은 **"측정 가능한 목표의 결핍을 공급 에이전트가 메우고 독립 판정이 채택하는 자율 루프"**(실행 커널)다.

---

## 1. 무슨 문제를 푸나

AI 에이전트에게 "이 저장소의 docstring 다 채워줘" 같은 일을 맡기면 흔히 이렇게 끝난다.

- 다 했다고 보고하는데 실제로는 절반만 했다.
- 하나를 고치면서 다른 것을 깨뜨린다.
- 고치기 어려운 것은 **측정 대상에서 빼 버려서**(함수 삭제, 검사 스크립트 수정) 숫자만 0 으로 만든다. (이 커널은 측정량이 *줄어드는* 것과 검사 스크립트 수정을 막는다 — §7 한계 참조)
- 안 되는 일을 끝없이 다시 시도하고, 토큰만 태운다.
- 중간에 죽으면 어디까지 했는지 아무도 모른다.

이 커널은 그 다섯 가지를 **구조로** 막는다. 핵심 아이디어는 세 개다.

1. **목표 = 결핍 목록.** "docstring 을 채워라"가 아니라 "오라클(측정 명령)이 결핍 0 건을 출력한다"가 목표다. 사람 말이 아니라 측정값이다.
2. **공급과 판정을 분리한다.** 공급자는 격리된 worktree 에서 결핍 **1건**만 메운다. 판정자는 공급자가 뭐라 했는지 보지 않고 **오라클을 다시 돌려서** 채택한다.
3. **끝은 항상 사유가 있다.** 달성했거나, k 회 연속 진전이 없거나, 예산을 다 썼거나, 외부 장애이거나, git 상태가 위험해서 멈췄거나. "아마 됐을 것"으로 끝나는 경로는 없다.

## 2. 구조

```
                  ┌──────────────────────────── 저장소 (main 은 건드리지 않음) ────────────────────────────┐
                  │                                                                                     │
  deficit.json ─▶ │  통합 브랜치  deficit-loop/<goal>          ◀── 채택된 공급만 merge 로 누적            │
  (goal spec)     │        │                                                                            │
                  │        ├── 판정 worktree  .deficit-loop/<goal>/wt/judge   (통합 브랜치 checkout)      │
                  │        │        └─ 오라클 재측정 ─▶ 결핍 목록 (before / after)                       │
                  │        │                                                                            │
                  │        └── 공급 worktree  .deficit-loop/<goal>/wt/supply-N  (통합 HEAD 에서 분기)    │
                  │                 └─ 공급자 실행 (스크립트 | 외부 에이전트 CLI)  → 커밋                   │
                  └─────────────────────────────────────────────────────────────────────────────────────┘

  루프:  측정 ─▶ 결핍 0? ─ yes ─▶ ACHIEVED
          │ no
          ├─ 예산/정지 조건 확인 ─▶ STALLED | BUDGET_EXHAUSTED | BLOCKED_EXTERNAL
          ▼
        결핍 1건 선택 (거절 적은 것 → 가장 오래 안 건드린 것)
          ▼
        공급 worktree 생성 ─▶ 공급자 실행 ─▶ 커밋
          ▼
        판정 worktree 에 merge 시도 ─▶ 오라클 재측정 ─▶ 판정
          ├─ ACCEPT : merge 유지 (통합 브랜치 전진)
          └─ REJECT : 통합 브랜치를 merge 전으로 reset, 공급 폐기
          ▼
        공급 worktree/브랜치 정리 (커널이 만든 것만) ─▶ state.json / events.jsonl 기록 ─▶ 반복
```

| 부품 | 파일 | 역할 |
|---|---|---|
| Goal spec | `deficit_loop/spec.py` | `deficit.json` 파싱. 오라클 명령, 공급자 종류, 보호 경로, 예산. 결핍 동일성 키 = `(kind, target)` |
| Oracle | `deficit_loop/oracle.py` | worktree 안에서 측정 명령 실행 → `{"deficits":[{kind,target,detail}], "scope":N}` |
| Supplier | `deficit_loop/supplier.py` | `script`(결정론 데모) / `external`(`$DEFICIT_LOOP_AGENT_CMD`, 없으면 비활성). 외부 오류(429·네트워크·exit 75) 분류 |
| Judge | `deficit_loop/judge.py` | 재측정 결과만으로 채택 판정. 4개 가드 |
| Kernel | `deficit_loop/kernel.py` | 루프·정지 조건·복구·소유권 |
| State | `deficit_loop/state.py` | 원자적 `state.json`, append-only `events.jsonl`, `flock` 단일 실행 잠금 |
| Git | `deficit_loop/gitops.py` | worktree/브랜치/merge, shallow clone 거부 |
| CLI | `deficit_loop/cli.py` | `init` / `run` / `resume` / `status` / `report` |

## 3. 판정 원칙

판정자는 공급자의 stdout(자기 보고)을 **기록만 하고 믿지 않는다.** 통합 브랜치에 공급을 merge 한 뒤 오라클을 다시 돌리고, 아래가 **전부** 성립할 때만 채택한다.

| 가드 | 막는 것 | 거절 사유 예 |
|---|---|---|
| `target_removed` | 할당된 결핍이 그대로 남아 있음 (다른 걸 고치고 "했다"고 함) | `target still present: …` |
| `no_new_deficits` | 하나 고치면서 다른 결핍을 새로 만듦 | `new deficits introduced: …` |
| `scope_not_shrunk` | 측정 대상 자체를 지워서 숫자만 줄임 (`scope` < 기준선) | `scope shrank 8 -> 7` |
| `protected_intact` | 오라클 스크립트·spec 등 보호 경로를 고쳐 측정을 조작 | `protected path modified: tools/…` |

공급 기준과 판정 기준은 **같은 커밋**(통합 브랜치 HEAD)이다 — 공급 worktree 는 거기서 분기하고, 판정은 거기에 merge 해서 본다. 둘이 어긋나면 정상 공급도 merge 단계에서 튕기거나, 앞선 채택분이 "공급자가 지운 것"으로 오판된다. 그래서 커널이 두 기준을 같이 들고 간다.

공급자의 exit code·stderr 는 **진단 정보일 뿐 판정이 아니다.** exit 0 이어도 재측정으로만 채택되고, exit ≠ 0 이거나 외부 오류(429 등)를 내고 죽었어도 **변경이 있으면 똑같이 측정**해서 결핍이 사라졌으면 채택한다(사유에 `supplier exited N` / `external error` 를 덧붙인다). 변경이 아예 없을 때만 측정 없이 끝난다 — 외부 오류면 `EXTERNAL_ERROR`(책임 없음), 아니면 `commit` 단계 거절.

`scripts/red_check.py` 가 이 원칙이 말뿐이 아님을 증명한다 — 가드를 하나씩 끄면 해당 테스트가 **실제로 실패**한다 (§6).

## 4. 정지 조건

| 상태 | 언제 | exit code | `resume` |
|---|---|---|---|
| `ACHIEVED` | 통합 브랜치에서 오라클이 결핍 0 건 | 0 | 최종 |
| `STALLED` | `stall_k`(기본 3) 회 연속 결핍 집합이 변하지 않음, 또는 남은 결핍이 전부 `max_reject_per_key`(기본 2) 회 거절돼 더 시도할 것이 없음 | 1 | 최종 |
| `BUDGET_EXHAUSTED` | `max_attempts` 또는 `max_seconds`(재개 누적) 초과 | 1 | 최종 |
| `BLOCKED_EXTERNAL` | 공급자가 `external_k`(기본 2) 회 연속 **외부 사유**(rate limit·429·네트워크·timeout·exit 75)로 실패. 결함이 아니므로 거절·무진전 카운트에 넣지 않는다 | 4 | 다시 연다 |
| `HALTED_UNSAFE` | shallow clone, 통합 브랜치가 남의 것, 커널 디렉토리 아래에 기록에 없는 worktree, 판정 worktree 가 다른 브랜치로 옮겨져 있음 등 — 소유권을 판정할 수 없으면 **아무것도 건드리지 않고** 멈춤 | 5 | 사람이 고친 뒤 다시 연다(같은 검사를 다시 통과해야 진행) |
| `HALTED_ERROR` | 통합 브랜치에서 오라클 자체가 실패하거나 git 명령이 실패. 진행 중이던 시도는 안전하면 그 자리에서 되돌리고 멈춘다 | 6 | 다시 연다 |
| (BUSY) | 다른 실행이 잠금을 쥐고 있음 | 3 | — |

고집 센 결핍 하나가 루프를 독점하지 않도록, 다음 대상은 *거절 횟수가 적은 것 → 가장 오래 안 건드린 것* 순으로 고른다. 거절이 한도에 닿은 결핍은 큐에서 빠진다. 중간에 죽은 시도도 `max_attempts` 에 세고(`interrupted`), 죽기 전까지 흐른 시간도 `max_seconds` 에 합산한다. 시간 예산은 단조 시계 기준 마감(deadline)으로 관리한다 — 오라클·공급자 timeout 모두 그 시점의 남은 예산으로 잘리고, 매 반복의 경과 시간은 어떤 경로로 끝나든 기록된다. 마감이 지나면 공급자·오라클 호출을 **시작하지 않는다** — 준비 중에 마감이 지나면 그 시도는 시작 전에 취소되고, merge 뒤 판정 측정 직전에 지나면 `BUDGET_ABORT` 로 기록돼 거절·무진전·외부오류 어디에도 세지 않는다. 정리 단계에서 죽은 시도의 경과 시간은 판정과 함께 기록돼 있어 복구 때 정확히 한 번 복원된다.

### Crash-safe

- `state.json` 은 tmp → fsync → rename 으로만 쓴다. 반쯤 쓰인 상태 파일은 생기지 않는다. **복구의 정본은 `state.json` 이다.**
- `events.jsonl` 은 append-only 감사 로그다. 각 단계는 이벤트를 먼저 쓰고 그 결과를 반영한 상태를 저장한다 — 두 쓰기 사이에서 죽으면 *이벤트는 있는데 상태에 아직 반영 안 된* 경우가 생길 수 있고, 그 반대(상태만 바뀌고 이벤트 없음)는 없다. 찢어진 마지막 줄(멀티바이트 문자 중간에서 끊긴 것 포함)은 `corrupt_line` 으로 보고되고(숨기지 않음), 다음 이벤트가 그 줄에 들러붙지 않으며, 복구를 막지 않는다.
- 브랜치·worktree 는 **만들기 전에** 소유 기록에 넣고, 공급을 시작하기 **전에** `in_flight` 를 기록한다. 중간에 죽으면 다음 `run`/`resume` 이 그 공급 worktree·브랜치를 폐기하고(merge 시도 중이었다면 통합 브랜치를 merge 전으로 되돌리고) 이어서 간다. 되돌리기 전에 판정 worktree 가 정말 통합 브랜치에 있는지, 그리고 HEAD 가 *되돌릴 지점 그 자체*(merge 가 커밋 전에 끊김 — `MERGE_HEAD`·스테이징을 abort)이거나 *부모가 정확히 (되돌릴 지점, 공급 브랜치 끝)인 우리 merge 커밋*인지 확인한다. 그 사이에 사람이 커밋을 얹었으면 `HALTED_UNSAFE` 로 멈추고 손대지 않는다. 측정은 항상 **깨끗한 커밋 트리**에서만 한다(merge 진행 중이거나 추적 파일이 수정돼 있으면 측정하지 않고 멈춘다). 판정이 난 뒤에는 그 판정(채택/거절 + 결과 SHA)을 **정리 전에** 먼저 기록하므로, 정리 도중 죽으면 되돌리지 않고 *마저 끝낸다*. merge 시점의 공급 커밋 SHA 도 기록해 두어 공급 브랜치가 이미 지워졌어도 "우리 merge 커밋" 을 식별할 수 있다. `init` 도 같은 방식이라 중간에 죽으면 `init` 을 다시 실행해 마저 끝낸다 — 단 `git worktree add` 가 디렉토리를 만들고 등록하기 전에 죽은 극히 짧은 창은 예외다: 등록 안 된 디렉토리는 커널이 지우지 않으므로 `.deficit-loop/<goal>/wt/judge` 를 사람이 지운 뒤 `init` 을 다시 돌린다. 테스트는 실제로 `SIGKILL` 로 죽여서 확인한다.
- `flock(LOCK_EX|LOCK_NB)` — `init`·`run`·`resume` 모두 잠금을 잡고, 두 번째 실행은 즉시 `BUSY`(3) 로 끝난다. 프로세스가 죽으면 OS 가 잠금을 푼다.
- 공급자·오라클은 **자기 프로세스 그룹**에서 돈다. timeout 이면 그룹 전체를 `SIGKILL` 하고 거둔 뒤에 정리한다 — 손자 프로세스가 살아남아 정리 뒤에 파일을 쓰는 일이 없다.

### 소유권

커널은 **자기가 만들고 `state.json` 에 기록한** 브랜치(`deficit-loop/<goal>`, `deficit-loop/<goal>-supply-N`)와 worktree(`.deficit-loop/<goal>/wt/*`)만 지운다. 이름이 같아도 기록에 없으면 남의 것으로 보고 `HALTED_UNSAFE`. 사용자의 checkout(`main`)은 읽기만 한다 — 결과는 통합 브랜치에 쌓이고, 가져갈지는 사람이 정한다. 반대로 `.deficit-loop/<goal>/wt/` 아래는 **커널 소유**다: 거기 직접 둔 untracked 파일은 거절·복구 때 함께 치워진다. 거기서 작업하지 마라.

## 5. 실행법

```bash
# 요구: Python 3.12+, git. 외부 패키지 없음 (테스트만 pytest)
cd your-repo
cat > deficit.json <<'EOF'
{
  "name": "docstrings",
  "oracle":   ["python3", "tools/measure_docstrings.py", "src"],
  "supplier": {"kind": "script", "command": ["python3", "tools/fill_docstring.py"]},
  "protected_paths": ["tools/", "deficit.json"],
  "budget": {"max_attempts": 20, "max_seconds": 300, "stall_k": 3, "max_reject_per_key": 2}
}
EOF
python3 -m deficit_loop init     # 통합 브랜치 + 판정 worktree 생성, 기준선 측정
python3 -m deficit_loop run      # 종료 상태가 날 때까지 drain
python3 -m deficit_loop status   # 한 화면 요약
python3 -m deficit_loop report   # 시도별 판정·단계·사유 표
python3 -m deficit_loop resume   # 중단된 실행 이어서 (run 도 이어가지만 resume 은 상태가 없으면 실패)
```

**오라클 계약** — cwd 는 측정할 worktree. stdout 에 JSON 한 덩어리:
`{"deficits": [{"kind": "...", "target": "...", "detail": "..."}], "scope": 8}` (또는 `scope` 없이 결핍 리스트만). `scope` 는 "측정한 대상 수"로, 있으면 삭제로 숫자를 줄이는 공급을 막는 데 쓴다(없으면 그 가드는 비활성).

**공급자 계약** — cwd 는 공급 worktree(파일만 고치면 된다. 커밋은 커널이 한다). 환경변수 `DL_DEFICIT`(JSON) / `DL_KIND` / `DL_TARGET` / `DL_DETAIL` / `DL_GOAL` / `DL_WORKTREE`, stdin 에도 결핍 JSON. exit 75 = 일시적 외부 장애(결함으로 세지 않음). 그 외 exit code 는 기록만 되고 판정은 항상 재측정으로 한다.

**외부 에이전트 연결** — spec 을 `"supplier": {"kind": "external"}` 로 두고 환경변수로 명령을 준다. 환경변수가 없으면 공급은 비활성이고 `run` 은 설정 오류(2)로 끝난다.

```bash
export DEFICIT_LOOP_AGENT_CMD='my-agent --prompt "Remove exactly this deficit, nothing else: $DL_DEFICIT"'
python3 -m deficit_loop run
```

```bash
make test        # pytest (단위 + 통합: 임시 git 저장소에서 끝까지 drain, SIGKILL 복구, 잠금, shallow clone …)
make demo        # 아래 §6 출력을 다시 만든다
make red-check   # 가드를 하나씩 끄고 테스트가 실패하는지 확인
```

## 6. 데모 출력 (실제 실행 원문)

`examples/sample_project` 는 public 정의 8개 중 5개에 docstring 이 없다. `python3.12 scripts/demo.py` 가 이를 임시 git 저장소로 복사해 drain 한다. 아래는 2026-10-19 실행 출력에서 임시 경로와 인터프리터 경로만 치환한 것이다.

```
$ git init -q -b main
$ git -c user.name=demo -c user.email=demo@localhost add -A
$ git -c user.name=demo -c user.email=demo@localhost commit -q -m sample project
# repo: /tmp/deficit-loop-demo-XXXX/sample_project

$ python3.12 -m deficit_loop init
[init] docstrings: 5 deficits, scope=8, branch=deficit-loop/docstrings
$ python3.12 -m deficit_loop run
[  1] missing_docstring:src/shapes.py::circle_area -> ACCEPT (done: deficits 5 -> 4)
[  2] missing_docstring:src/shapes.py::Triangle -> ACCEPT (done: deficits 4 -> 3)
[  3] missing_docstring:src/shapes.py::perimeter -> ACCEPT (done: deficits 3 -> 2)
[  4] missing_docstring:src/text.py::slugify -> ACCEPT (done: deficits 2 -> 1)
[  5] missing_docstring:src/text.py::truncate -> ACCEPT (done: deficits 1 -> 0)
[finish] ACHIEVED: oracle reports 0 deficits
$ python3.12 -m deficit_loop report
goal:        docstrings
status:      ACHIEVED — oracle reports 0 deficits
deficits:    5 initial -> 0 remaining
attempts:    5 (accept 5 / reject 0 / external 0)
no_progress: 0 consecutive
elapsed:     2.5s
baseline:    scope=8

attempts:
#  deficit                                       verdict  stage  reason           sec
-  --------------------------------------------  -------  -----  ---------------  ---
1  missing_docstring:src/shapes.py::circle_area  ACCEPT   done   deficits 5 -> 4  0.3
2  missing_docstring:src/shapes.py::Triangle     ACCEPT   done   deficits 4 -> 3  0.3
3  missing_docstring:src/shapes.py::perimeter    ACCEPT   done   deficits 3 -> 2  0.3
4  missing_docstring:src/text.py::slugify        ACCEPT   done   deficits 2 -> 1  0.3
5  missing_docstring:src/text.py::truncate       ACCEPT   done   deficits 1 -> 0  0.3

$ git log --oneline --first-parent deficit-loop/docstrings
2cf65c7 deficit-loop adopt missing_docstring:src/text.py::truncate
3457465 deficit-loop adopt missing_docstring:src/text.py::slugify
d95c7d9 deficit-loop adopt missing_docstring:src/shapes.py::perimeter
3252080 deficit-loop adopt missing_docstring:src/shapes.py::Triangle
f1878b7 deficit-loop adopt missing_docstring:src/shapes.py::circle_area
822eb51 sample project
$ git diff --stat main..deficit-loop/docstrings
 src/shapes.py | 3 +++
 src/text.py   | 2 ++
 2 files changed, 5 insertions(+)

# main is untouched (the oracle still sees 5 deficits there); the result lives on branch deficit-loop/docstrings
$ python3.12 tools/measure_docstrings.py src
{"deficits": [{"kind": "missing_docstring", "target": "src/shapes.py::circle_area", "detail": "line 4"}, {"kind": "missing_docstring", "target": "src/shapes.py::Triangle", "detail": "line 13"}, {"kind": "missing_docstring", "target": "src/shapes.py::perimeter", "detail": "line 17"}, {"kind": "missing_docstring", "target": "src/text.py::slugify", "detail": "line 1"}, {"kind": "missing_docstring", "target": "src/text.py::truncate", "detail": "line 10"}], "scope": 8}
```

거절이 나는 경우는 테스트가 보여준다 — `tests/test_guards.py` 의 공급자들은 각각 거짓 보고, 다른 결핍 유발, 엉뚱한 대상 수정, 함수 삭제, 오라클 조작을 하고 모두 `REJECT` 된다. `tests/test_integration.py::test_stubborn_target_does_not_hog_the_loop` 은 못 고치는 결핍 1건이 있어도 나머지 4건이 채택되고 `STALLED` 로 정직하게 끝나는 것을 보인다.

### red check (실제 실행 원문)

```
guard                  test                                   guard on  guard off  verdict
---------------------  -------------------------------------  --------  ---------  -------
trust_supplier_report  test_liar_supplier_is_rejected         pass      fail       ok
no_new_deficits        test_new_deficit_is_rejected           pass      fail       ok
target_removed         test_target_still_present_is_rejected  pass      fail       ok
scope_not_shrunk       test_scope_shrink_is_rejected          pass      fail       ok
protected_intact       test_protected_path_is_rejected        pass      fail       ok
stall_detection        test_stall_detected                    pass      fail       ok

red check: ALL GUARDS PROTECTED
```

(각 행의 `# off: FAILED …` 꼬리는 생략. `make red-check` 로 재현.)

## 7. 한계

- **공급 1건 = 결핍 1건, 직렬.** 병렬 공급은 없다. 결핍이 수백 건이면 느리다. 병렬화하려면 공급 base 가 서로 갈라지는 문제(merge 충돌)를 다뤄야 해서 일부러 뺐다.
- **오라클이 전부다.** 오라클이 못 재는 것은 목표가 될 수 없고, 오라클이 틀리면 판정도 틀린다. `protected_paths` 는 저장소 *안의* 오라클 자산만 지킨다 — 공급자가 worktree 밖(환경, 전역 설정)을 건드리는 것은 막지 못한다.
- **외부 오류 분류는 휴리스틱이다.** exit 75 와 stderr 패턴(rate limit·429·network…)으로 가른다. 공급자가 다른 말로 실패하면 결함으로 세어진다. 오라클의 네트워크 장애는 분류하지 않고 `HALTED_ERROR` 로 멈춘다.
- **`scope` 가드는 개수만 본다.** 결핍이 있는 함수를 지우고 다른 함수를 하나 추가하면 개수가 같아 통과한다. 삭제를 완전히 막으려면 오라클이 측정 대상의 식별자를 내고 판정이 그 집합을 비교해야 한다 — 지금은 "측정량이 줄어드는" 가장 흔한 억제만 막는다.
- **무진전 판정은 결핍 집합 기준이다.** 공급이 거절되면 집합이 안 변하므로 사실상 "연속 거절 k 회"다. 결핍 A 가 사라지고 B 가 생기는 식의 제자리걸음은 진전으로 센다(새 결핍 가드가 먼저 거절하므로 실제로는 드물다).
- **같은 저장소에서 다른 프로세스가 git 을 흔드는 것**(shallow fetch, 통합 브랜치 수동 reset)은 감지해서 멈출 뿐, 복구하지 않는다.
- **복구는 `state.json` 만 본다.** `events.jsonl` 을 재생해 상태를 재구성하지는 않는다.
- **외부 오류 판별이 stderr 패턴에 기대는 만큼, 공급자가 결핍을 고쳐 놓고 429 를 찍으며 죽어도 채택은 된다** — 하지만 그 반대로 고치지 못한 채 네트워크 오류 문구만 낸 결함 공급자는 외부 오류로 분류돼 거절 횟수에 안 잡힌다. `external_k` 가 그 상한이다.
- 외부 에이전트 CLI 연결은 인터페이스만 있다. 이 저장소의 테스트·데모는 전부 결정론 공급자로 돈다 — 실제 LLM 에이전트로 돌린 결과는 여기 없다.
- macOS/Linux 전용(`fcntl.flock`).

## 라이선스

MIT — Kim Taemin. `LICENSE` 참조.
