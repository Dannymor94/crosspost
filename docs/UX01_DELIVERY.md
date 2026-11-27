# UX-01 — согласование delivery-кандидата

Дата: 2026-11-27. Ответственный: ChatGPT, координация интеграции T-215–218. Это черновик поставки, не разрешение на merge/deploy. Исходные PR остаются открытыми и неизменными до решения владельца о способе поставки.

## Проверенная основа

- Roadmap PR #51: `ae31deb97068703a495f401394fce874db66b7ae`; main на момент сверки `58dedfcb5226998190ddbce43b138eb378f655af`.
- Validation-only HEAD `83912d122b848a0a598545eccab3f3b5eadda1a8`. [Независимый APPROVE](https://github.com/Dannymor94/crosspost/issues/57#issuecomment-7213582216) закрывает пять конкретных находок; не весь UX-01 и не rollout.
- [CI](https://github.com/Dannymor94/crosspost/actions/runs/45962710573): 1353 passed на каждой версии Python; [Chromium](https://github.com/Dannymor94/crosspost/actions/runs/45962710591): 5/5 client и 5/5 admin, synthetic HTTP. Независимые 3 API probes с файловой SQLite и mocked adapters прошли.
- Все четыре исходных HEAD являются предками проверенной ревизии (compare: behind=0, merge base равен соответствующему HEAD). Не повторять реализацию.

| PR / HEAD | Контракт | Интеграционное согласование |
|---|---|---|
| #53 / 917f38cd | T-215, C1 | Серверные guards и CAS, pending flow; проверка definitive moderation rejection |
| #54 / 31d98d2d | T-216, C2 | Ledger, reservation, proof, receipt/recovery, admission fence и полный набор outcomes |
| #55 / edc2d2fa | T-217, C3 | UTC/IANA, migration chain, claim fence, canonical ordering, legacy slots |
| #56 / 3dba39f0 | T-218, C4 | Scope/CAS drafts, editor targets, consumed revision, complete receipt, two-tab regression |

C7: единый согласованный набор migration и route-smoke, общие actual-form tests. Перед поставкой сверить migration head/backup с другими открытыми PR, а не только с roadmap base.

## Предлагаемая упаковка

Отдельная draft delivery-ветка сохраняет весь approved source/test tree без повторного ручного объединения общих файлов. Дельта к validation SHA ограничена документацией и запуском существующего synthetic browser workflow на delivery-ветке. Это кандидат на согласование, а не одобрение изменения границ четырёх исходных PR; не мержить их независимо поверх этого кандидата. Если владелец предпочитает раздельную поставку, переносить изменения последовательно в исходные ветки с явной сверкой пересечений. Не закрывать исходные PR до согласованной поставки.

## Gates перед выпуском

| Область | Открытый критерий |
|---|---|
| C1 | Решение о boundary смены moderation и явно просроченных pending; проверка после принятия |
| C2 | Mandatory keys / legacy API rollout, известный replay, import dedup/preview binding, keyed URL media, ledger retention |
| C3 | Owner evidence о legacy timezone, backup/dry-run/migration, overdue policy, time/import consistency |
| C4 | Session incarnation/logout/revoke, cross-tab teardown, draft/proof retention, admin schedule modal time |
| C7 | Согласовать способ поставки, актуальный base и migration graph с auth/ops PR, backup coverage, CI и независимый QA итогового delivery SHA |
| Release | Owner live/device/ops/restore, HTTPS, security gates, no production data в CI |

До решений сохранять fail-closed, не переотправлять SUBMITTED, не удалять медиа, не угадывать историческую зону. Не считать незакрытые product/rollout gates дефектами уже проверенного R3, но не отмечать T-215–218 done. UX-02/T-501 отдельно: сначала сверка совместимости и delivery, затем wiring; не начинать UX-03 поверх устаревших исходных веток. 
