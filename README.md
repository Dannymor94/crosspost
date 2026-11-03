# crosspost

Сервис кросс-постинга для клиентов: загрузил готовый контент → выбрал каналы →
опубликовалось во все разом или по расписанию → видно результат по каждому каналу.
Система **доставляет** контент, а не генерирует.

**Каналы:** Telegram, Instagram Reels, Telegraph (API) · Яндекс Бизнес, ВКонтакте стена
и канал (браузер). Актуальный статус — [`docs/STATUS.md`](docs/STATUS.md).

**Этап:** переход от MVP к продакшену — множество клиентов, деплой на сервер.
План — [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Для агентов и новых чатов

Начинать с [`AGENTS.md`](AGENTS.md). Там правила, роли (Claude исполняет, Codex ревьюит)
и карта документации.

## Документация

| Файл | Что |
|---|---|
| [`AGENTS.md`](AGENTS.md) | правила, инварианты, Definition of Done |
| [`docs/STATUS.md`](docs/STATUS.md) | текущее состояние, известные проблемы |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | фазы и критерии «готово» |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | задачи с ID |
| [`docs/WORKFLOW.md`](docs/WORKFLOW.md) | процесс исполнитель → ревьюер → мерж |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | как устроено и почему, карта кода |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | запуск, конфигурация, клиенты, секреты |
| [`docs/decisions.md`](docs/decisions.md) | журнал архитектурных решений |
| [`CHANGELOG.md`](CHANGELOG.md) | изменения |
| [`docs/archive/`](docs/archive/) | история этапа MVP, не источник правды |

## Быстрый старт

```bash
uv sync --all-extras
mkdir -p runtime
cp .env.example runtime/.env    # заполнить: docs/OPERATIONS.md §2
make run
pytest -q                       # все тесты на моках, ключи не нужны
```

## Лицензия

[MIT](LICENSE) © 2026 Dannymor94. Репозиторий приватный: хранит доступы к аккаунтам клиентов.
