import threading
import signal
import sys
from logger_config import setup_logging

from services import SyncService, Listener, Sender
from api_adapters import VKApi, NSApi

from database import session_factory
from models import SourcesOrm, SystemConfigOrm
from sqlalchemy import select

from config import decrypt_token


logger = setup_logging()

def windows_break_handler(signum, frame):
    # этот обработчик ловит сигнал от админки на виндовс - принудительно вызывает KeyboardInterrupt в главном потоке
    logger.info("Получен сигнал мягкой остановки от админ-панели. Вызываем KeyboardInterrupt...")
    raise KeyboardInterrupt


def get_sources_from_db():
    with session_factory() as session:
        return session.scalars(select(SourcesOrm)).all()


def main():
    # регистрация обработчика сигналов от админки только на виндовс
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, windows_break_handler)

    with session_factory() as session:
        config = session.get(SystemConfigOrm, 1)

        # если записи в базе нет - подставляем дефолтные значения
        config_limit = config.limit if config else 10
        config_max_age = config.sync_max_age if config else 7
        config_sleep = config.sleep_time if config else 40

    logger.info(
        "Загрузка конфига из бд -> limit: %s, max_age: %s дней, sleep: %s сек.",
        config_limit, config_max_age, config_sleep
    )

    try:
        sources = get_sources_from_db()
        if len(sources) < 2:
            logger.error("В бд нет источников / всего один источник для синхронизации. Выход.")
            return
        logger.info("Успешно загружено %s источников из бд", len(sources))
    except Exception:
        logger.exception("Критическая ошибка при подключении к бд")
        return

    stop_event = threading.Event()
    sync_service = SyncService()


    adapters_map = {}
    for source in sources:
        # расшифровка токена перед передачей в апи-адаптер
        real_token = decrypt_token(source.api_token)

        if source.type == "vk":
            adapters_map[source.id] = VKApi(token=real_token, source_type=source.type, owner_id=source.api_id)
        elif source.type == "news_site":
            adapters_map[source.id] = NSApi(token=real_token, source_type=source.type)
        else:
            adapters_map[source.id] = None

    listeners = []
    threads = []

    sender = Sender(adapters_map, stop_event, barrier=None, delivery_barrier=None)
    threads.append(threading.Thread(target=sender.run, name="Сендер"))

    # собираем лисенеры и их потоки
    offset = 1
    for source in sources:
        adapter = adapters_map.get(source.id)
        if not adapter:
            logger.warning("Пропущен источник id %s: не найден подходящий api адаптер", source.id)
            continue

        if source.just_hear:
            logger.info("Источник %s настроен в режиме 'Только прием'. Лисенер не создается.", source.name)
            continue

        # временно создаем лисенер без барьера
        listener = Listener(source.id, adapter, sync_service, stop_event, barrier=None, delivery_barrier=None, offset=offset,
                            limit=config_limit, sync_max_age=config_max_age, sleep_time=config_sleep)
        listeners.append(listener)
        offset += 1

        # даем потоку имя из бд
        thread_name = f"Лисенер-{source.name}"
        threads.append(threading.Thread(target=listener.run, name=thread_name))

    # если работающих лисенеров вообще не осталось
    if not listeners:
        logger.error("Нет активных лисенеров для запуска. Выход.")
        return

    # инициализация барьера на нужное количество потоков
    # количество реально созданных лисенеров + 1 поток сендера
    sync_barrier = threading.Barrier(len(listeners) + 1)
    delivery_barrier = threading.Barrier(len(listeners) + 1)
    # logger.info("Инициализирован синхронизационный барьер на %s потоков", sync_barrier.parties)

    # прописываем актуальный барьер в сендер и во все созданные лисенеры
    sender.barrier = sync_barrier
    sender.delivery_barrier = delivery_barrier

    for listener in listeners:
        listener.barrier = sync_barrier
        listener.delivery_barrier = delivery_barrier


    for t in threads:
        t.start()

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        stop_event.set()
        sync_barrier.abort()  # разблокируем всех, кто застрял на барьере
        delivery_barrier.abort()
        for t in threads:
            t.join()

        logger.info("Все потоки остановлены")


if __name__ == "__main__":
    main()