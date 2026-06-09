import os
import signal
import subprocess
import time
import sys
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Response, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqladmin import Admin, ModelView, BaseView, expose
from wtforms.fields import SelectField
from wtforms.validators import NumberRange

from database import sync_engine
from models import SourcesOrm, PostDeliveriesOrm, RoutingOrm, SourcesDraftOrm, SystemConfigOrm
from sqlalchemy import select, delete, update
from database import session_factory

from config import encrypt_token

from logger_config import setup_logging


logger = setup_logging()
sync_process: Optional[subprocess.Popen] = None
reload_request_flag = False
stop_request_flag = False

# логика управления подпроцессом main.py
def start_sync_system():
    global sync_process
    if sync_process and sync_process.poll() is None:
        return

    python_executable = sys.executable
    logger.info("[Админ-панель] Запуск системы синхронизации...")

    # на виндовс добавляем специальный флаг для возможности отправки ctrl+c
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    sync_process = subprocess.Popen(
        [python_executable, "main.py"],
        creationflags=creationflags
    )


def wait_and_stop_system_safely():
    """Каждую секунду проверяет pending доставки. Когда их нет - мягко останавливает систему."""
    global sync_process

    logger.info("[Админ-панель] Ожидание завершения pending доставок...")

    # цикл проверки очереди каждую секунду
    while True:
        with session_factory() as session:
            # считается количество pending доставок в базе
            pending_count = len(session.scalars(
                select(PostDeliveriesOrm)
                .filter_by(status="pending")
            ).all())

        if pending_count == 0:
            logger.info("[Админ-панель] Активных доставок нет. Остановка...")
            break

        time.sleep(1)

    # посылаем сигнал Ctrl+Break / SIGINT, когда очередь гарантированно пуста
    if sync_process and sync_process.poll() is None:
        if sys.platform == "win32":
            os.kill(sync_process.pid, signal.CTRL_BREAK_EVENT)
        else:
            sync_process.send_signal(signal.SIGINT)

        # ожидание полной физической остановки процесса main.py
        while sync_process.poll() is None:
            time.sleep(0.2)
        logger.info("[Админ-панель] Система полностью остановлена.")


def apply_draft_to_production():
    """Переносит конфигурацию из черновика в актуальную бд и строит новый роутинг"""
    with session_factory() as session:
        session.execute(delete(RoutingOrm))

        # получение айди всех источников, которые сейчас есть в черновике
        draft_sources = session.scalars(select(SourcesDraftOrm)).all()
        draft_ids = {d.id for d in draft_sources}

        # удаление из актуальной базы только тех источников, которых больше нет в черновике
        session.execute(
            delete(SourcesOrm).where(SourcesOrm.id.notin_(draft_ids))
        )

        # обновление существующих или добавление новых источников
        for draft in draft_sources:
            prod_source = session.get(SourcesOrm, draft.id)

            if prod_source:
                # если источник уже был - обновляем его поля
                prod_source.name = draft.name
                prod_source.type = draft.type
                prod_source.api_id = draft.api_id
                prod_source.just_hear = draft.just_hear
                prod_source.api_token = draft.api_token
            else:
                # если источника не было - создаем новую запись
                new_source = SourcesOrm(
                    id=draft.id,
                    name=draft.name,
                    type=draft.type,
                    api_id=draft.api_id,
                    just_hear=draft.just_hear,
                    api_token=draft.api_token
                )
                session.add(new_source)

        session.flush()

        prod_sources = session.scalars(select(SourcesOrm)).all()
        for s1 in prod_sources:
            for s2 in prod_sources:
                if s1.id == s2.id:
                    continue
                routing = RoutingOrm(source_id=s1.id, target_source_id=s2.id)
                session.add(routing)
        session.commit()


def graceful_apply_workflow():
    """Цепочка шагов для безопасного применения изменений"""
    global reload_request_flag
    wait_and_stop_system_safely()  # ожидание очистки очереди и завершение процесса
    apply_draft_to_production()    # перенос черновика в актуальную базу
    reload_request_flag = False    # отключение индикатора применения
    start_sync_system()            # Запуск обновленной конфигурации
    time.sleep(1)                  # проверка валидации len(sources) < 2


def graceful_stop_workflow():
    """Цепочка шагов для безопасной полной остановки системы"""
    global stop_request_flag
    wait_and_stop_system_safely()
    stop_request_flag = False


def init_system_config_if_empty():
    """Проверяет наличие конфигурации в бд и создает дефолтную запись при первом запуске"""
    with session_factory() as session:
        config_exists = session.get(SystemConfigOrm, 1)

        if not config_exists:
            logger.info("[Админ-панель] Таблица конфигурации системы пустая. Заполнение значениями по умолчанию...")
            default_config = SystemConfigOrm(
                id=1,
                limit=10,
                sync_max_age=7,
                sleep_time=40
            )
            session.add(default_config)
            session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Менеджер жизненного цикла приложения FastAPI"""
    init_system_config_if_empty()
    start_sync_system()  # автостарт при запуске админки
    time.sleep(1)
    yield
    wait_and_stop_system_safely()  # автостоп при закрытии админки


app = FastAPI(title="Панель управления системой", lifespan=lifespan)

@app.post("/admin/start-system")
async def start_system():
    start_sync_system()
    time.sleep(1)
    # если main.py уже завершился (poll() вернул код, отличный от None)
    if sync_process and sync_process.poll() is not None:
        logger.error("[Админ-панель] Ошибка: Система синхронизации завершила работу сразу после старта.")

    return RedirectResponse(url="/admin/sources-draft-orm/list", status_code=303)

@app.post("/admin/apply-changes")
async def apply_changes(background_tasks: BackgroundTasks):
    global reload_request_flag
    logger.info("[Админ-панель] Запрос на применение изменений принят.")
    reload_request_flag = True

    background_tasks.add_task(graceful_apply_workflow)
    return RedirectResponse(url="/admin/sources-draft-orm/list", status_code=303)

@app.post("/admin/stop-system")
async def stop_system(background_tasks: BackgroundTasks):
    global stop_request_flag
    logger.info("[Админ-панель] Запрос на остановку системы принят.")
    stop_request_flag = True

    background_tasks.add_task(graceful_stop_workflow)
    return RedirectResponse(url="/admin/sources-draft-orm/list", status_code=303)

@app.get("/")
async def redirect_to_admin():
    return RedirectResponse(url="/admin/sources-draft-orm/list")

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_stub():
    # отдаем пустой json-ответ, чтобы chrome не спамил ошибками 404
    return Response(content="{}", media_type="application/json")


# перехват каждого ответа от сервера перед отправкой в браузер
@app.middleware("http")
async def remove_new_keyword_middleware(request: Request, call_next):
    response = await call_next(request)

    # проверка: ответ — это html-страница админки
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type and request.url.path.startswith("/admin"):
        # сборка тела ответа в строку
        body = b"".join([chunk async for chunk in response.body_iterator])
        html_text = body.decode("utf-8")

        # Вырезаем слово "New "
        html_text = html_text.replace("New ", "Добавить ")

        # пересборка ответа для браузера
        encoded_body = html_text.encode("utf-8")
        response.headers["content-length"] = str(len(encoded_body))

        # возвращаем обновленный html
        async def new_body_iterator():
            yield encoded_body

        response.body_iterator = new_body_iterator()

    return response

# настройка интерфейса таблиц базы данных
admin = Admin(app, sync_engine, base_url="/admin", templates_dir="templates")


class SourcesAdmin(ModelView, model=SourcesDraftOrm):
    column_list = [SourcesDraftOrm.id, SourcesDraftOrm.name, SourcesDraftOrm.type, SourcesDraftOrm.just_hear]

    list_template = "sources_dashboard.html"

    form_overrides = {"type": SelectField}
    form_args = {
        "type": {
            "choices": [
                ("vk", "ВКонтакте"),
                ("news_site", "Новостной сайт"),
            ]
        }
    }

    column_labels = {
        SourcesDraftOrm.id: "ID",
        SourcesDraftOrm.name: "Название",
        SourcesDraftOrm.type: "Тип платформы",
        SourcesDraftOrm.api_id: "ID группы или страницы",
        SourcesDraftOrm.just_hear: "Только прослушивание",
        SourcesDraftOrm.api_token: "API Токен / Ключ доступа (шифр.)"
    }
    can_create = True
    can_edit = True
    can_delete = True
    name = "источник"
    name_plural = "Источники"
    icon = "fa-solid fa-share-nodes"

    # перехват контекста страницы списка, чтобы прокинуть статус процесса
    async def list(self, request):
        global sync_process
        is_running = sync_process is not None and sync_process.poll() is None

        # передача признака активности системы напрямую в глобальный контекст запроса,
        # чтобы шаблон sources_dashboard.html его увидел
        request.state.is_running = is_running
        request.state.reload_request = reload_request_flag
        request.state.stop_request = stop_request_flag

        return await super().list(request)

    # хук на добавление и изменение источника
    async def after_model_change(self, request, model, is_created, data):
        """Срабатывает после того, как админ добавил или изменил источник"""
        if model.api_token and not model.api_token.startswith("gAAAAA"):  # префикс зашифрованных строк Fernet
            with session_factory() as session:
                # шифрование чистого текста
                encrypted = encrypt_token(model.api_token)

                # обновление записи в черновике
                session.execute(
                    update(SourcesDraftOrm)
                    .where(SourcesDraftOrm.id == model.id)
                    .values(api_token=encrypted)
                )

                session.commit()

        logger.info("[Админ-панель] Изменения успешно сохранены в черновик конфигурации.")

    # хук на удаление источника
    async def after_model_delete(self, request, model):
        """Срабатывает после того, как админ удалил источник"""
        logger.info("[Админ-панель] Источник удален из черновика конфигурации.")


class DeliveriesAdmin(ModelView, model=PostDeliveriesOrm):
    column_list = [
        PostDeliveriesOrm.id,
        PostDeliveriesOrm.post_id,
        PostDeliveriesOrm.target_source_id,
        PostDeliveriesOrm.action,
        PostDeliveriesOrm.status,
        PostDeliveriesOrm.retries
    ]

    list_template = "deliveries_list.html"

    # названия для колонок в шапке таблицы
    column_labels = {
        PostDeliveriesOrm.id: "ID Задачи",
        PostDeliveriesOrm.post_id: "ID Поста",
        PostDeliveriesOrm.target_source_id: "ID Назначения (Source)",
        PostDeliveriesOrm.action: "Действие",
        PostDeliveriesOrm.status: "Статус",
        PostDeliveriesOrm.retries: "Попытки"
    }

    can_create = False
    can_edit = False
    can_delete = True
    name = "Доставка"
    name_plural = "Мониторинг доставок"
    icon = "fa-solid fa-truck"

    def list_query(self, request):
        # отображение pending, failed доставок
        return super().list_query(request).where(
            PostDeliveriesOrm.status.in_(["pending", "failed"])
        )


class LogsAdmin(BaseView):
    name = "Логи системы"
    icon = "fa-solid fa-file-lines"

    # сработает при переходе в "Логи системы" в меню
    @expose("/logs")
    async def report(self, request):
        log_file_path = "sync_system.log"
        log_lines = "Файл логов еще не создан или пуст."

        if os.path.exists(log_file_path):
            try:
                with open(log_file_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    # отступ назад на 25 кб (150 строк логов с запасом).
                    f.seek(max(0, f.tell() - 25600))

                    chunk = f.read().decode("utf-8", errors="ignore")

                    # deque оставляет ровно последние 150 строк
                    lines = deque(chunk.splitlines(), maxlen=150)
                    log_lines = "\n".join(lines)
            except Exception as e:
                log_lines = f"Не удалось прочитать логи: {e}"

        # рендер шаблона
        return await self.templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={"log_lines": log_lines}
        )


class ConfigAdmin(ModelView, model=SystemConfigOrm):
    column_list = [
        SystemConfigOrm.limit,
        SystemConfigOrm.sync_max_age,
        SystemConfigOrm.sleep_time
    ]

    list_template = "config_redirect.html"

    column_labels = {
        SystemConfigOrm.limit: "Количество синхронизируемых постов",
        SystemConfigOrm.sync_max_age: "Синхронизировать посты за последние (дней)",
        SystemConfigOrm.sleep_time: "Время сна лисенеров (сек.)"
    }

    # ограничения для числовых полей ввода
    form_args = {
        "limit": {
            "validators": [
                NumberRange(
                    min=1,
                    max=100,
                    message="Значение должно быть в диапазоне от 1 до 100"
                )
            ]
        },
        "sync_max_age": {
            "validators": [
                NumberRange(
                    min=1,
                    max=100,
                    message="Значение должно быть в диапазоне от 1 до 100"
                )
            ]
        },
        "sleep_time": {
            "validators": [
                NumberRange(
                    min=10,
                    max=10000,
                    message="Значение должно быть в диапазоне от 10 до 10000"
                )
            ]
        }
    }

    # разрешено только редактировать
    can_create = False
    can_delete = False
    can_edit = True

    name = "конфигурация"
    name_plural = "Конфигурация системы"
    icon = "fa-solid fa-gear"


admin.add_view(SourcesAdmin)
admin.add_view(DeliveriesAdmin)
admin.add_view(LogsAdmin)
admin.add_view(ConfigAdmin)