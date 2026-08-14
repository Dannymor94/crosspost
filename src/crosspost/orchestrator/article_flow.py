"""Единый секвенсор связки «статья Telegraph → пост со ссылкой». Оркестрация.

Один элемент плана порождает ДВА действия по порядку: сначала статья (получаем URL),
затем каналы (короткий текст + ссылка на статью). Порядок обязателен: ссылка
существует только после создания статьи.

Секвенсирование живёт ЗДЕСЬ, в одном месте. Единственное различие между немедленной
публикацией (single-shot) и планировщиком (ретрай с потолком) — политика ретраев, и она
инъектируется через `step`. Всё остальное (порядок, подстановка url, fail-closed,
идемпотентность) общее. Модуль НЕ знает про repo/service/attempt_count — всё внешнее
приходит параметрами (step/fail_closed), поэтому тестируется на fake-step без БД.

Fail-closed: статья не создана (FAILED) → каналы НЕ публикуются, помечаются failed с
причиной (NO_ARTICLE_REASON) через fail_closed. Обрубок «Читать полностью →» без ссылки
не выпускаем (подтверждённое решение).

Идемпотентность встроена в `step`: уже-DONE канал/статья → DONE без повторной отправки.
Поэтому повторный вызов на любом тике не создаёт вторую страницу и переиспользует URL.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from crosspost.content.article_link import article_content, render_with_link

if TYPE_CHECKING:  # избегаем цикла: publish_service импортирует run_binding
    from crosspost.content.canonical import CanonicalContent
    from crosspost.orchestrator.publish_service import ChannelOutcome

# Целевой канал фазы статьи. Именованная константа — свапаемо под будущий Дзен.
_ARTICLE_CHANNEL = "telegraph"


class StepState(Enum):
    DONE = "done"  # канал финализирован успешно (или уже был готов)
    FAILED = "failed"  # канал финально провалился (исчерпан ретрай / жёсткая ошибка)
    PENDING = "pending"  # ещё не финализирован — следующий тик добьёт (только executor)


@dataclass
class StepResult:
    state: StepState
    outcome: ChannelOutcome
    external_id: str | None


@dataclass
class FlowResult:
    outcomes: list[ChannelOutcome]
    pending: bool  # True → не финализировать пост, следующий тик продолжит


# Публикует ОДНУ единицу — item (канал ИЛИ пара (канал, цель), см. ниже) — или
# коротко замыкает уже-DONE. Вся политика ретраев здесь. Тип item непрозрачен для
# секвенсора: он лишь пробрасывает его в step/fail_closed. В проде item = Selection
# (канал+цель, мультицели), в юнит-тестах связки — просто строка-канал.
Step = Callable[[Any, "CanonicalContent"], Awaitable[StepResult]]
# Записать единице FAILED с причиной fail-closed и вернуть итог (доступ к repo — у вызывающего).
FailClosed = Callable[[Any], Awaitable["ChannelOutcome"]]


async def run_binding(
    *,
    content: CanonicalContent,
    channels: Sequence[Any],
    article_text: str,
    article_title: str | None,
    template: str,
    link_phrase: str,
    step: Step,
    fail_closed: FailClosed,
    article_item: Any = _ARTICLE_CHANNEL,
) -> FlowResult:
    """Прогнать связку: (опц.) статья → подстановка url → единицы публикации.

    `channels` — последовательность непрозрачных единиц (item). Секвенсор их не
    инспектирует, только пробрасывает в step/fail_closed. Для мультицелей одна и та
    же площадка появляется в списке несколько раз (по разу на цель) как разные item —
    step различает их по цели. `article_item` — единица фазы статьи (Telegraph
    таргетлесс); по умолчанию строка-канал (юнит-тесты связки), в проде — Selection.
    См. модульный docstring.
    """
    # ── нет статьи: обычный пост во все единицы ───────────────────────────────
    if not (article_text and article_text.strip()):
        outcomes = [await step(item, content) for item in channels]
        pending = any(o.state is StepState.PENDING for o in outcomes)
        return FlowResult([o.outcome for o in outcomes], pending)

    # ── фаза статьи: сначала Telegraph, потом единицы со ссылкой ───────────────
    art = await step(article_item, article_content(article_text, article_title))

    if art.state is StepState.PENDING:
        # Статья ещё не готова — единиц не касаемся, следующий тик добьёт.
        return FlowResult([], pending=True)

    if art.state is StepState.FAILED:
        # Fail-closed: пост не публикуем, каждой единице — FAILED с причиной.
        closed = [await fail_closed(item) for item in channels]
        return FlowResult([*closed, art.outcome], pending=False)

    # DONE: подставляем url статьи в текст поста, публикуем единицы.
    # external_id у DONE-статьи — URL страницы (квитанция Telegraph); "" быть не должно.
    art_url = art.external_id or ""
    post_content = replace(
        content, text=render_with_link(content.text, art_url, template, link_phrase)
    )
    results = [await step(item, post_content) for item in channels]
    pending = any(r.state is StepState.PENDING for r in results)
    return FlowResult([*[r.outcome for r in results], art.outcome], pending)
