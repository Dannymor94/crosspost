# INT-01 — изолированная репетиция интеграции

Контракт: [execution package](https://github.com/Dannymor94/crosspost/issues/26#issuecomment-7008347205).
BASE_SHA: `c6504012530cef0b98edb477edaf1cb2028bc33e` (actual origin/main при старте).
Checkout: `/tmp/crosspost-int01-20261119`; Documents недоступна ОС, исходное дерево не менялось.
Ветка: `rehearsal/INT-01-20261119`. Мерж в main и деплой запрещены.

## Зафиксированные исходные heads

| PR | SHA |
|---|---|
| #18 | `24de6b0e8791b3f066364967b3e47102eeae3d1c` |
| #17 | `22d3cc121550c62278362bbf9c2b4d933f1cd198` |
| #21 | `1b5df39f5c8066e0333fb0d9b05e403d1751c28d` |
| #20 | `ed677b757e1c12763aa7236e4fdabd89f4b50d96` |
| #25 | `62a4c9c85127625b8e5236d7bbd74ed0750d6e5a` |
| #23 | `cae65d8eacc78024cb97e7f81515e582168784be` |

Все heads при старте OPEN; CI каждого проверен по jobs Python 3.11 + 3.13 SUCCESS.
#18/#17 имеют независимое одобрение перед последним docs-only коммитом; diff проверен:
соответственно только WORKFLOW и STATUS. #21 — independent R3 PASS, #20 — independent
source/runtime PASS, #25 — independent R1 PASS, #23 — independent R2 source + CI PASS.
Ни один из этих вердиктов не подменяет независимую интеграционную QA.

## Пошаговая проверка

Каждый шаг — локальный merge commit в rehearsal-ветке и полный `make check`.
- После #18: `f34f4226cc2fb6ffe8c89b4776b2235fdc1f1e84`, PASS, 1116 passed; ruff/format/mypy PASS.
- После #17: `f799b75d84c54dd69a3d8ab50c13906db34a0531`, PASS, 1121 passed; ruff/format/mypy PASS.
- После #21: `e51b5af59287ac396c071f437c5441f330ce1bc1`, PASS, 1166 passed; ruff/format/mypy PASS.
- После #20: `87b1f89f475b991b7ccf43fa661c7be0229b1fe9`, PASS, 1177 passed; ruff/format/mypy PASS.
- После #25: `42f9e6738be34558e8f9065461c46123b1d9df6b`, PASS, 1187 passed; ruff/format/mypy PASS.
- После #23: HEAD этой ветки; точный SHA, полный результат и CI публикуются в одном evidence report Issue #26 после завершения gate. Этот commit также содержит интеграционный smoke и сводку состояния.

## Конфликты

- #18/#17: без конфликтов.
- #21: CHANGELOG и STATUS; сохранены обе записи, счётчик отдельной ветки заменён ссылкой на совмещённый gate.
- #20: CHANGELOG; обе записи сохранены, унаследованный маркер конфликта удалён, число синтетических тестов уточнено до 11.
- #25: CHANGELOG; сохранены все записи. OPERATIONS объединился автоматически и содержит оба runbook.
- #23: CHANGELOG, STATUS, decisions; решения о ролях и breaker сохранены, повторная запись T-209 убрана. Python-код не требовал ручных конфликтных правок.
- Исходные T-209/T-202 — ancestors #23; merge использует историю, без повторного cherry-pick. Приложение совпадает с объединением pinned inputs, новых production-изменений не внесено.
- BACKLOG T-212 приведён к явно заданному architecture-only статусу из execution package; terminal SUBMITTED не менялся.

## Cross-PR smoke

Проверки исполняются на совмещённом HEAD в полном `make check`, площадки замоканы:

| Инвариант | Исполняемое доказательство |
|---|---|
| T-209 retention | `tests/web/test_publish_api.py` (T-209 direct/retry/cancel/reject/PATCH/CAS), `tests/scheduler/test_executor.py` (настоящий executor) |
| T-202 credential fence | `tests/sessions/test_health.py`, `tests/channels/test_connection_validator.py`, stale credential dispatch в breaker suite |
| T-203 frozen CB-01…CB-18 | `tests/orchestrator/test_breaker_dispatch.py`, включая CB04 (24 гонки) и CB06 legacy ban; HTTP manual retry в web suite |
| T-207 WAL + authenticated restore | `tests/ops/test_sqlite_backup.py`, включая unsigned-byte race/no-clobber/wrong-key |
| T-213 только чтение | `tests/media/test_diagnostics.py` и интеграционный active-WAL smoke ниже |
| Полная совмещённая схема | `tests/ops/test_integration_rehearsal.py::test_int01_migrated_wal_backup_restore_and_read_only_media_audit` |

Интеграционный smoke создаёт только синтетическую мигрированную БД и медиа в tmp_path.
CLI диагностики сохраняет size/mtime DB/WAL/SHM и SHA256 DB/WAL/media, orphan не удаляется.
Online backup видит committed WAL; после verify/restore в новый путь сохранены профиль,
зашифрованный credential (расшифровывается тем же внешним тестовым ключом), BANNED,
SUBMITTED, OPEN и Alembic head. Реальные БД, медиа и ключи не читались/не загружались.

## Следующий checkpoint

После CI SUCCESS на точном integration SHA — независимая INT-02 QA. READY_FOR_MERGE: NO.
После INT-02 PASS владелец решает о последовательных merge #18 → #17 → #21 → #20 → #25 → #23.
На каждом actual новом main обязательна повторная проверка; SHA репетиции не считается
будущим main после squash. Операторский T-207 copied-DB drill/offsite/key escrow и живой
T-213 smoke остаются отдельными production gates. T-214 cleanup остаётся OFF.
