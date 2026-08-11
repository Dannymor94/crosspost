"""Инструкции и МАСТЕР подключения — часть ДЕКЛАРАЦИИ канала (не хардкод в UI).

Тон — дружеский, на «вы», без техжаргона (аудитория: клиенты-нетехнари).
Каждый канал приносит свой мастер сам: ChannelValidatorDef.guide ссылается на
константу отсюда. Новый канал → добавил шаги → UI сам построит мастер.

Две части декларации:
  1. Мастер (steps) — пошаговый сценарий: одно действие на шаг, инструкция
     встроена, поля шага, ориентиры «куда идти» и правила валидации ввода.
     Шаги адаптивны под тир (API интерактивный / API простой / браузерный).
  2. Справка (intro/blocks/step_hints/browser_note/errors) — используется в
     подсказках и в блоке «Не получается?» (errors → симптом/что проверить).

Словарь видов шага (kind) — фиксирован в UI, декларация лишь выбирает вид:
  collect        — собрать поля, локальная проверка, дальше (без запроса);
  tg_begin       — начать вход Telegram (api_id/api_hash/канал) → шаг телефона;
  tg_phone       — отправить код на номер;
  tg_code        — подтвердить код (→ 2FA, если требуется);
  tg_twofa       — облачный пароль (2FA);
  simple_connect — сохранить учётку простого API-канала (/connect);
  browser_open   — открыть окно входа (браузерный тир);
  browser_confirm— подтвердить вход в окне;
  target         — сохранить цель постинга (адрес группы / ID организации).

Словарь нормализаторов ввода (normalize) — тоже фиксирован в UI:
  trim | digits | tg_channel | yandex_org | vk_group | vk_channel_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Модель справки (осталась от прошлой итерации, используется в подсказках) ────


@dataclass(frozen=True)
class GuideBlock:
    """Раскрывающийся блок «Как это получить?»: заголовок + шаги + ссылка."""

    label: str
    steps: tuple[str, ...]
    link: str = ""
    link_label: str = ""
    field: str = ""


@dataclass(frozen=True)
class StepHint:
    """Подсказка на конкретном шаге интерактивного входа."""

    key: str
    text: str


@dataclass(frozen=True)
class ErrorHint:
    """Что проверить при типичной ошибке подключения."""

    symptom: str
    check: str


# ── Модель МАСТЕРА (пошаговый сценарий подключения) ────────────────────────────


@dataclass(frozen=True)
class WizardField:
    """Поле ввода одного шага мастера + правило проверки формата на лету."""

    name: str  # имя поля (совпадает с полем формы/эндпоинта)
    label: str
    placeholder: str = ""
    secret: bool = False
    hint: str = ""
    pattern: str = ""  # JS-regex для мгновенной проверки формата ("" — не проверять)
    invalid_message: str = ""  # что показать, если формат неверный
    normalize: str = (
        "trim"  # трансформ ввода в UI: trim|digits|tg_channel|yandex_org|vk_group|vk_channel_id
    )
    paste: bool = True  # показывать кнопку «Вставить»


@dataclass(frozen=True)
class WizardStep:
    """Один шаг мастера: одно действие + встроенная инструкция + поля шага."""

    key: str  # стабильный id шага
    kind: str  # вид шага (см. словарь в шапке модуля)
    title: str  # заголовок-действие («Создайте приложение»)
    body: str = ""  # встроенная инструкция (не под кликом)
    landmark: str = ""  # «должно выглядеть так»: что искать на внешней странице
    link: str = ""  # прямая ссылка на нужный РАЗДЕЛ (не на главную)
    link_label: str = ""
    fields: tuple[WizardField, ...] = ()
    optional: bool = False  # шаг может быть пропущен (напр. 2FA — только если нужен)


@dataclass(frozen=True)
class ChannelGuide:
    """Полная декларация подключения канала: мастер + справка."""

    intro: str = ""
    blocks: tuple[GuideBlock, ...] = ()
    step_hints: tuple[StepHint, ...] = ()
    browser_note: str = ""
    errors: tuple[ErrorHint, ...] = field(default_factory=tuple)
    steps: tuple[WizardStep, ...] = ()  # мастер (пусто → канал без мастера)


# ── Переиспользуемые поля ───────────────────────────────────────────────────────

_F_API_ID = WizardField(
    name="api_id",
    label="api_id",
    placeholder="1234567",
    pattern=r"^\d+$",
    invalid_message="api_id — это только цифры, без пробелов и букв.",
    normalize="digits",
)
_F_API_HASH = WizardField(
    name="api_hash",
    label="api_hash",
    placeholder="0123456789abcdef0123456789abcdef",
    secret=True,
    pattern=r"^[a-fA-F0-9]{32}$",
    invalid_message="api_hash — 32 символа: латинские буквы a–f и цифры.",
    normalize="trim",
)
_F_TG_CHANNEL = WizardField(
    name="target_channel",
    label="Адрес канала",
    placeholder="@mychannel",
    pattern=r"^(@?[A-Za-z0-9_]{4,}|https?://t\.me/[A-Za-z0-9_]{4,}|t\.me/[A-Za-z0-9_]{4,})$",
    invalid_message="Укажите @имя канала или ссылку вида t.me/имя.",
    normalize="tg_channel",
)
_F_PHONE = WizardField(
    name="phone",
    label="Номер телефона",
    placeholder="+7 900 000-00-00",
    pattern=r"^\+?[0-9][0-9\s\-()]{7,}$",
    invalid_message="Номер с кодом страны, например +7 900 000-00-00.",
    normalize="trim",
)
_F_CODE = WizardField(
    name="code",
    label="Код из Telegram",
    placeholder="12345",
    pattern=r"^[0-9]{4,6}$",
    invalid_message="Код — 5 цифр, придёт в чат «Telegram».",
    normalize="digits",
)
_F_TWOFA = WizardField(
    name="password",
    label="Облачный пароль",
    secret=True,
    placeholder="ваш пароль двухэтапной проверки",
    normalize="trim",
)

# ── Telegram ──────────────────────────────────────────────────────────────────

TELEGRAM_GUIDE = ChannelGuide(
    intro=(
        "Подключим ваш Telegram-канал. Один раз получим два кода приложения и "
        "подтвердим вход по номеру телефона — дальше посты уходят сами."
    ),
    blocks=(
        GuideBlock(
            field="api_id",
            label="Как получить api_id и api_hash?",
            steps=(
                "Откройте my.telegram.org/apps и войдите по своему номеру — код придёт в Telegram.",
                "Откроется «App configuration». Если приложения ещё нет — создайте: «App title» "
                "любое (например crosspost), «Platform» — Desktop, «Create application».",
                "На этой же странице скопируйте «App api_id» (число) и «App api_hash» (длинная строка).",
            ),
            link="https://my.telegram.org/apps",
            link_label="открыть my.telegram.org →",
        ),
        GuideBlock(
            field="target_channel",
            label="Что вписать в «адрес канала»?",
            steps=(
                "Это адрес вашего канала в виде @имя — туда будут уходить посты.",
                "В ссылке после t.me/ и есть это имя (t.me/mychannel → @mychannel).",
                "Важно: вы должны быть администратором этого канала.",
            ),
        ),
    ),
    step_hints=(
        StepHint("form", "Это разовая настройка: впишем коды приложения и адрес канала."),
        StepHint("phone", "Введите номер, к которому привязан ваш Telegram — на него придёт код."),
        StepHint("code", "Откройте Telegram: код придёт в чат «Telegram». Впишите его сюда."),
        StepHint(
            "password",
            "У аккаунта включена двухэтапная проверка — введите ваш облачный пароль Telegram.",
        ),
    ),
    errors=(
        ErrorHint(
            "Ошибка про api_id или api_hash",
            "Проверьте, что скопировали оба значения целиком, без пробелов и не перепутали местами.",
        ),
        ErrorHint(
            "Пишет, что нет прав в канале",
            "Убедитесь, что вы администратор канала и вписали правильный адрес в виде @имя.",
        ),
        ErrorHint(
            "Код не подходит",
            "Код действует недолго — запросите новый и введите сразу, как придёт.",
        ),
    ),
    steps=(
        WizardStep(
            key="app",
            kind="collect",
            title="Создайте приложение Telegram",
            body=(
                "Нужны два кода из вашего аккаунта Telegram — они выдаются один раз на сайте "
                "Telegram. Откройте сайт по кнопке ниже (при входе подтвердите вход кодом из "
                "приложения). Откроется страница «App configuration» с этими двумя кодами — "
                "вернитесь сюда и вставьте оба значения."
            ),
            landmark=(
                "Откроется страница «App configuration». Если приложение уже создано — прямо на "
                "ней видны «App api_id» (число) и «App api_hash» (длинная строка из букв и цифр): "
                "просто скопируйте их. Если приложения ещё нет — страница предложит его создать: "
                "«App title» — любое название (например crosspost), «Platform» — Desktop, затем "
                "нажмите «Create application», и значения появятся."
            ),
            link="https://my.telegram.org/apps",
            link_label="Открыть my.telegram.org",
            fields=(_F_API_ID, _F_API_HASH),
        ),
        WizardStep(
            key="channel",
            kind="tg_begin",
            title="Укажите канал для публикаций",
            body=(
                "Впишите адрес вашего канала — туда будут уходить посты. Вы должны быть его "
                "администратором. Подойдёт @имя или ссылка t.me/имя."
            ),
            fields=(_F_TG_CHANNEL,),
        ),
        WizardStep(
            key="phone",
            kind="tg_phone",
            title="Подтвердите вход по номеру",
            body=(
                "Введите номер телефона вашего Telegram — на него придёт код подтверждения. "
                "Пароль мы не спрашиваем и не храним."
            ),
            fields=(_F_PHONE,),
        ),
        WizardStep(
            key="code",
            kind="tg_code",
            title="Введите код из Telegram",
            body="Откройте Telegram — код придёт сообщением в чат «Telegram». Впишите его сюда.",
            fields=(_F_CODE,),
        ),
        WizardStep(
            key="twofa",
            kind="tg_twofa",
            title="Облачный пароль",
            body=(
                "У вашего аккаунта включена двухэтапная проверка. Введите облачный пароль "
                "Telegram — это последний шаг."
            ),
            fields=(_F_TWOFA,),
            optional=True,
        ),
    ),
)


# ── Telegraph (статьи-лонгриды, токен) ─────────────────────────────────────────

TELEGRAPH_GUIDE = ChannelGuide(
    intro=(
        "Telegraph — это красивые статьи-лонгриды со ссылкой. Подключается по токену "
        "доступа. Токен технический, поэтому проще всего, если я создам его для вас."
    ),
    blocks=(
        GuideBlock(
            field="token",
            label="Где взять токен Telegraph?",
            steps=(
                "Токен Telegraph не выдаётся на сайте кнопкой — это техническое значение.",
                "Проще всего: напишите мне, и я пришлю вам готовый токен.",
                "Если токен у вас уже есть — просто вставьте его в поле.",
            ),
        ),
    ),
    errors=(
        ErrorHint(
            "Токен не подходит",
            "Проверьте, что вставили токен целиком, без пробелов. Если токена нет — "
            "напишите мне, я создам его для вас.",
        ),
    ),
    steps=(
        WizardStep(
            key="token",
            kind="simple_connect",
            title="Вставьте токен Telegraph",
            body=(
                "Telegraph подключается по токену доступа. Токен технический — если его нет, "
                "напишите мне, и я пришлю готовый. Если токен уже есть, вставьте его ниже."
            ),
            fields=(
                WizardField(
                    name="token",
                    label="Токен Telegraph",
                    secret=True,
                    placeholder="вставьте токен",
                    pattern=r"^\S{20,}$",
                    invalid_message="Вставьте токен целиком, без пробелов.",
                    normalize="trim",
                ),
            ),
        ),
    ),
)


# ── Общий текст браузерного входа ───────────────────────────────────────────────

_BROWSER_NOTE = (
    "Нажмите «Войти» — откроется окно браузера. Войдите в свой аккаунт до рабочего "
    "кабинета и нажмите «Я вошёл». Ваш пароль остаётся только у вас — мы сохраняем "
    "лишь сам вход, чтобы публиковать от вашего имени."
)
_BROWSER_SESSION_ERROR = ErrorHint(
    "После «Я вошёл» пишет, что вход не выполнен",
    "Войдите в окне полностью — до страницы кабинета — и нажмите «Я вошёл» ещё раз. "
    "Не закрывайте окно раньше времени.",
)


def _browser_login_step(service: str) -> WizardStep:
    return WizardStep(
        key="login",
        kind="browser_open",
        title=f"Войдите в {service}",
        body=(
            f"Сейчас откроется окно браузера со страницей входа {service}. Войдите в тот "
            "аккаунт, под которым ведёте страницу, и дойдите до рабочего кабинета. Ваш пароль "
            "остаётся только у вас — мы сохраняем лишь сам вход."
        ),
    )


_BROWSER_CONFIRM_STEP = WizardStep(
    key="confirm",
    kind="browser_confirm",
    title="Подтвердите вход",
    body=(
        "Вошли в открывшемся окне до рабочего кабинета? Нажмите «Я вошёл» — мы проверим "
        "и сохраним доступ. Если окно закрылось — вернитесь на шаг назад и откройте снова."
    ),
)


# ── Яндекс Бизнес ───────────────────────────────────────────────────────────────

YANDEX_GUIDE = ChannelGuide(
    intro=(
        "Подключим Яндекс Бизнес — публикации в карточке вашей организации. "
        "Откроется окно браузера, вы войдёте в свой Яндекс, и мы сохраним вход."
    ),
    browser_note=_BROWSER_NOTE,
    blocks=(
        GuideBlock(
            field="target",
            label="Где взять ID организации?",
            steps=(
                "Откройте кабинет вашей организации на Яндексе (раздел «Публикации»).",
                "В адресе браузера после /sprav/ идёт число — это и есть ID.",
                "Скопируйте число. Можно вставить и весь адрес — мы оставим только ID.",
            ),
            link="https://yandex.ru/sprav",
            link_label="открыть Яндекс Бизнес →",
        ),
    ),
    errors=(
        _BROWSER_SESSION_ERROR,
        ErrorHint(
            "Публикация не появляется сразу",
            "У Яндекса посты проходят проверку и публикуются не сразу (иногда до нескольких "
            "дней) — это нормально, так устроен сам Яндекс.",
        ),
    ),
    steps=(
        _browser_login_step("Яндекс"),
        _BROWSER_CONFIRM_STEP,
        WizardStep(
            key="target",
            kind="target",
            title="Укажите организацию",
            body="Осталось указать, в какую организацию публикуем — по её ID из адреса кабинета.",
            landmark=(
                "Откройте кабинет организации на Яндексе. В адресной строке после /sprav/ идёт "
                "число — например yandex.ru/sprav/12345678/… → ID это 12345678."
            ),
            link="https://yandex.ru/sprav",
            link_label="Открыть Яндекс Бизнес",
            fields=(
                WizardField(
                    name="target",
                    label="ID организации",
                    placeholder="12345678",
                    pattern=r"^\d{3,}$",
                    invalid_message="ID организации — число из адреса кабинета (после /sprav/).",
                    normalize="yandex_org",
                ),
            ),
        ),
    ),
)


# ── ВКонтакте — стена сообщества ────────────────────────────────────────────────

VK_WALL_GUIDE = ChannelGuide(
    intro=(
        "Подключим стену вашего сообщества ВКонтакте. Откроется окно браузера — "
        "войдите в свой ВК, и мы сохраним вход."
    ),
    browser_note=_BROWSER_NOTE,
    blocks=(
        GuideBlock(
            field="target",
            label="Где взять адрес группы?",
            steps=(
                "Откройте ваше сообщество ВКонтакте.",
                "Скопируйте адрес из строки браузера (например vk.com/myclub).",
                "Вставьте сюда целиком — мы оставим только короткое имя.",
            ),
        ),
    ),
    errors=(
        _BROWSER_SESSION_ERROR,
        ErrorHint(
            "Вошли не в тот аккаунт",
            "Вернитесь на шаг входа, откройте окно заново и войдите под аккаунтом с правами в "
            "сообществе.",
        ),
    ),
    steps=(
        _browser_login_step("ВКонтакте"),
        _BROWSER_CONFIRM_STEP,
        WizardStep(
            key="target",
            kind="target",
            title="Укажите сообщество",
            body="Осталось указать, на стену какого сообщества публикуем.",
            landmark=(
                "Откройте ваше сообщество ВКонтакте и скопируйте адрес из строки браузера — "
                "например vk.com/myclub. Можно вставить ссылку целиком."
            ),
            fields=(
                WizardField(
                    name="target",
                    label="Адрес сообщества",
                    placeholder="vk.com/myclub",
                    pattern=r"^[A-Za-z0-9_.]+$",
                    invalid_message="Вставьте адрес vk.com/имя или короткое имя сообщества.",
                    normalize="vk_group",
                ),
            ),
        ),
    ),
)


# ── ВКонтакте — канал в мессенджере ─────────────────────────────────────────────

VK_CHANNEL_GUIDE = ChannelGuide(
    intro=(
        "Подключим ваш канал в мессенджере ВК. Вход общий со стеной ВКонтакте: если стена "
        "уже подключена, отдельный вход не нужен — укажите только id канала."
    ),
    browser_note=_BROWSER_NOTE,
    blocks=(
        GuideBlock(
            field="target",
            label="Где взять id канала?",
            steps=(
                "Это числовой id вашего канала-сообщества (например -240033402).",
                "Откройте канал в разделе «Мессенджер» ВК.",
                "Если не уверены, какой это id — напишите мне, подскажу.",
            ),
        ),
    ),
    errors=(_BROWSER_SESSION_ERROR,),
    steps=(
        _browser_login_step("ВКонтакте"),
        _BROWSER_CONFIRM_STEP,
        WizardStep(
            key="target",
            kind="target",
            title="Укажите канал",
            body=(
                "Вход общий со стеной ВК. Осталось указать числовой id вашего канала-сообщества."
            ),
            landmark=(
                "Это числовой id канала-сообщества, например -240033402. Если не знаете его — "
                "напишите мне, подскажу по вашему каналу."
            ),
            fields=(
                WizardField(
                    name="target",
                    label="ID канала",
                    placeholder="-240033402",
                    pattern=r"^-?\d+$",
                    invalid_message="Числовой id канала, например -240033402.",
                    normalize="vk_channel_id",
                ),
            ),
        ),
    ),
)
