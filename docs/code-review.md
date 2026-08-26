Standards
Hard violations: None; no documented coding-standard source exists.
Judgement calls:
- Duplicated Code: workspace.py:149-226 repeats creation rollback/recovery handling.
- Primitive Obsession / Repeated Switches: string states recur in models.py, workspace.py, and serialization.py; small enums/constrained types could centralize transitions.
- Middle Man: workspace.py:1330-1367 intent/outcome wrappers only delegate.
- Divergent Change: workspace.py (1,766 lines) owns creation, claim, removal, context, status, locking, and rollback.
- Duplicated Code: test run_ws/Git helpers repeat across integration test files.
Spec
Missing or partial:
- No README, despite documentation requirements (doc/workspace-plan.md:1989-1993).
- Human status omits default selector, context phase, and removal progress (:1013-1028).
- Mandatory concurrent-process and real crash coverage is incomplete (:1813-1823, :1884-1903).
Scope creep:
- Claim/context/finalize acquire the lifecycle lock intended for create/remove (:1180-1194, :2381-2384).
Implemented but apparently wrong:
- Remote default disagreement compares only branch-name suffixes (:179-185).
- Context state is workspace-global rather than per repository (:950-954).
- Tombstone recovery is not durable across SIGKILL/power loss (:723-727).
Summary: Standards: 5 judgement-call findings; worst is divergent responsibilities in workspace.py. Spec: 7 findings; worst is non-durable tombstone recovery.
