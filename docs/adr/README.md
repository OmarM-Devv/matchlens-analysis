# Architecture decision records

Short records of the main design decisions in MatchLens: the context, the
options considered, what was chosen and what it costs. They follow the fields
in the UK government's
[Architectural Decision Record Framework](https://www.gov.uk/government/publications/architectural-decision-record-framework/architectural-decision-record-framework).

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-single-transaction-with-advisory-lock.md) | Load each snapshot in one transaction under an advisory lock | Accepted |
| [0002](0002-set-aside-shots-missing-xg.md) | Set aside shot events with no xG instead of rejecting the snapshot | Accepted |
| [0003](0003-aggregate-shots-before-joining.md) | Aggregate shots per match and team before joining | Accepted |
| [0004](0004-keep-sql-in-named-sql-files.md) | Keep the schema and queries in plain SQL files | Accepted |
