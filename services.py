from sqlalchemy.orm import joinedload, contains_eager
from models import SourcesOrm, PostOrm, PostInstanceOrm, RoutingOrm, PostDeliveriesOrm, MediaOrm, MediaInstanceOrm
from database import session_factory
from datetime import datetime, timezone, timedelta
from sqlalchemy import delete, select, desc, update
import threading
import os
import logging

logger = logging.getLogger("sync_system")


class SyncService:

    @staticmethod
    def sync_post(source_id, external_post_id, content, created_at, updated_at, attachments=None):
        with (session_factory() as session):

            instance = session.scalar(
                select(PostInstanceOrm)
                .filter_by(external_post_id=external_post_id, source_id=source_id)
                .options(joinedload(PostInstanceOrm.post).joinedload(PostOrm.medias))
                .limit(1)
            )

            payload = {
                "content": content,
                "attachments": attachments or []
            }

            # уже существует
            if instance:
                post = instance.post

                existing_media_external_ids = session.scalars(
                    select(MediaInstanceOrm.external_media_id)
                    .filter_by(post_instance_id=instance.id)
                ).all()

                # собираем external_id из того, что только что пришло от апи
                incoming_external_ids = [a['external_id'] for a in (attachments or [])]

                # сравниваем
                media_changed = set(existing_media_external_ids) != set(incoming_external_ids)


                if post.content != content or media_changed:

                    instance.inst_updated_at = updated_at

                    # получаем существующие доставки
                    active_deliveries = session.scalars(
                        select(PostDeliveriesOrm).filter_by(
                            post_id=post.id,
                            action="update",
                            status="pending"
                        )
                    ).all()

                    should_add_new = False

                    if not active_deliveries:
                        # доставок нет — значит надо создать
                        should_add_new = True
                    else:
                        # доставки есть — проверяем время
                        # если пришло обновление свежее, чем то, что ждет в очереди — перезаписываем
                        if active_deliveries[0].newest_update_time < instance.inst_updated_at:
                            session.execute(
                                delete(PostDeliveriesOrm).filter_by(
                                    post_id=post.id,
                                    action="update",
                                    status="pending"
                                )
                            )
                            should_add_new = True

                    # добавляем новые записи только если should_add_new == True
                    if should_add_new:
                        post.updated_at = updated_at

                        routings = session.scalars(select(RoutingOrm).filter_by(source_id=source_id)).all()
                        for r in routings:
                            if r.target_source_id == source_id:
                                continue

                            session.add(PostDeliveriesOrm(
                                post_id=post.id,
                                target_source_id=r.target_source_id,
                                action="update",
                                payload=payload,
                                origin_source_id=source_id,
                                newest_update_time=instance.inst_updated_at,
                            ))

                    if incoming_external_ids:
                        stmt = delete(MediaInstanceOrm).where(MediaInstanceOrm.post_instance_id == instance.id).where(
                            MediaInstanceOrm.external_media_id.notin_(incoming_external_ids))
                    else:
                        # если в апи вложений нет вообще — удаляем все, что привязано к инстансу
                        stmt = delete(MediaInstanceOrm).where(MediaInstanceOrm.post_instance_id == instance.id)
                    session.execute(stmt)

                    session.commit()
                    logger.info("Обновление, id поста: %s", external_post_id)
                    return

                logger.info("Без изменений, id поста: %s", external_post_id)
                return

            # новый пост
            post = PostOrm(
                content=content,
                created_at=created_at,
                updated_at=updated_at,
                origin_source_id=source_id,
                origin_external_post_id=external_post_id
            )
            session.add(post)
            session.flush()

            # создаём инстанс поста
            instance = PostInstanceOrm(
                post_id=post.id,
                source_id=source_id,
                external_post_id=external_post_id,
                last_synced_at=datetime.now(timezone.utc),
                inst_updated_at=datetime.now(timezone.utc),  # new
            )
            session.add(instance)

            routings = session.scalars(select(RoutingOrm).filter_by(
                source_id=source_id
            ))

            for r in routings:
                if r.target_source_id == source_id:
                    continue

                session.add(PostDeliveriesOrm(
                    post_id=post.id,
                    target_source_id=r.target_source_id,
                    action="create",
                    payload=payload,
                    origin_source_id=source_id
                ))

            session.commit()

            logger.info("Новый пост, id: %s", external_post_id)


    @staticmethod
    def detect_deleted_posts(source_id, actual_ids, limit, sync_deadline):

        with session_factory() as session:

            instances = session.scalars(
                select(PostInstanceOrm)
                .join(PostInstanceOrm.post)  # соединяем таблицы постов и инстансов
                .filter(PostInstanceOrm.source_id == source_id)
                # берем посты только в пределах периода синхронизации
                .filter(PostOrm.created_at >= sync_deadline)
                # сначала самые свежие посты (чтобы лимит работал предсказуемо)
                .order_by(desc(PostOrm.created_at))
                # ограничиваем выборку размером пачки из апи
                .limit(limit)
                # загружаем связанные посты из этого же плоского join-запроса
                .options(contains_eager(PostInstanceOrm.post))
            ).all()


            for inst in instances:
                if inst.external_post_id not in actual_ids:

                    if inst.post.is_deleted:
                        continue

                    inst.post.is_deleted = True

                    session.delete(inst)

                    routings = session.scalars(
                        select(RoutingOrm).filter_by(source_id=source_id)
                    ).all()

                    for r in routings:

                        exists = session.scalar(select(PostDeliveriesOrm).filter_by(
                            post_id=inst.post.id,
                            target_source_id=r.target_source_id,
                            action="delete",
                            status="pending"
                        ))

                        if not exists:
                            session.add(PostDeliveriesOrm(
                                post_id=inst.post.id,
                                target_source_id=r.target_source_id,
                                action="delete",
                                origin_source_id=source_id
                            ))

                    session.commit()
                    logger.info("Удаление, id поста: %s", inst.external_post_id)


class Listener:
    def __init__(self, source_id, api_adapter, sync_service, stop_event, barrier, delivery_barrier, offset,
                 limit, sync_max_age, sleep_time):
        self.source_id = source_id
        self.api = api_adapter
        self.sync_service = sync_service
        self.stop_event = stop_event
        self.barrier = barrier
        self.delivery_barrier = delivery_barrier

        # смещение по времени, чтобы лисенеры не били в апи одновременно
        self.phase_offset = offset * 1.2

        # настройки из конфига
        self.sync_max_age = sync_max_age
        self.posts_limit = limit
        self.sleep_time = sleep_time

    def run(self):
        logger.info("Запущен.")

        while not self.stop_event.is_set():
            # начальная задержка фазы
            if self.stop_event.wait(self.phase_offset):
                break

            try:
                # получаем посты через адаптер
                posts = self.api.get_posts(self.posts_limit)

                # собираем актуальные айди для детектора удалений
                actual_ids = {p["id"] for p in posts}

                sync_deadline = datetime.now(timezone.utc) - timedelta(days=self.sync_max_age)

                # синхронизируем каждый пост
                for p in posts:

                    post_date = datetime.fromtimestamp(p.get("date"), tz=timezone.utc)

                    # если пост создан раньше, чем дедлайн - игнорируем его
                    if post_date < sync_deadline:
                        # logger.debug(f"Пост {p['id']} пропущен, так как он старше {self.sync_max_age} дней.")
                        continue

                    self.sync_service.sync_post(
                        source_id=self.source_id,
                        external_post_id=p["id"],
                        content=p.get("text", ""),
                        created_at=datetime.fromtimestamp(p.get("date"), tz=timezone.utc),
                        updated_at=datetime.fromtimestamp(p.get("edited"), tz=timezone.utc),
                        attachments=p.get("attachments"),
                    )

                # поиск удаленных постов
                self.sync_service.detect_deleted_posts(self.source_id, actual_ids, self.posts_limit, sync_deadline)

            except Exception as e:
                logger.error("Ошибка. Тип ошибки: %s. Описание: %s", type(e).__name__, e)

            finally:
                # гарантируем прохождение барьера, чтобы сендер не завис
                try:
                    logger.info("Отработал. Жду барьер сбора...")
                    self.barrier.wait()

                    logger.info("Заблокирован на время работы сендера...")
                    self.delivery_barrier.wait()

                except threading.BrokenBarrierError:
                    logger.warning("Барьер сломан.")
                    break

            # интервал между проверками
            self.stop_event.wait(self.sleep_time)


class Sender:
    def __init__(self, adapters_map, stop_event, barrier, delivery_barrier):
        self.adapters = adapters_map
        self.stop_event = stop_event
        self.barrier = barrier
        self.delivery_barrier = delivery_barrier

    def run(self):
        logger.info("Запущен.")
        while not self.stop_event.is_set():
            try:
                # синхронизация с лисенерами
                logger.info("Жду данные от лисенеров (барьер)...")
                self.barrier.wait()

                # даем апи остыть после работы лисенеров перед началом рассылки
                if self.stop_event.wait(2):
                    break

                self._process_deliveries()

            except threading.BrokenBarrierError:
                logger.warning("Барьер сломан, завершаю поток.")
                break

            except Exception as e:
                logger.error("Критическая ошибка в цикле. Тип ошибки: %s. Описание: %s", type(e).__name__, e)

            finally:
                # работа полностью окончена, все PostInstanceOrm в базе
                # раскрываем барьер 2 и даем лисенерам команду работать дальше
                try:
                    logger.info("Завершил отправки. Освобождаю лисенеры.")
                    self.delivery_barrier.wait()
                except threading.BrokenBarrierError:
                    break

            # небольшая пауза после круга
            self.stop_event.wait(1)

    def _process_deliveries(self):
        with session_factory() as session:

            # проверяем хранилище на наличие ненужных медиа
            self._clean_unused_media(session)

            # получаем все задачи
            deliveries = session.scalars(
                select(PostDeliveriesOrm)
                .filter_by(status="pending")
                .order_by(desc(PostDeliveriesOrm.id))
            ).all()

            if not deliveries:
                return

            # блок конфликтов
            posts_to_delete = {d.post_id for d in deliveries if d.action == "delete"}

            valid_deliveries = []
            for d in deliveries:
                if d.post_id in posts_to_delete and d.action == "update":
                    session.delete(d)
                    logger.info("Отмена обновления для поста с id %s, так как есть заявка на удаление", d.post_id)
                else:
                    valid_deliveries.append(d)

            session.commit()

            # выполнение доставок
            for d in valid_deliveries:
                if self.stop_event.is_set():
                    break

                try:
                    self._execute_single_delivery(session, d)
                except Exception as e:
                    logger.error("Ошибка при выполнении доставки %s. Тип ошибки: %s. Описание: %s", d.id, type(e).__name__, e)
                    d.retries += 1
                    d.status = "failed" if d.retries >= 3 else "pending"
                    session.commit()

    @staticmethod
    def _clean_unused_media(session):
        try:
            # ищем MediaOrm, у которых нет связей в MediaInstanceOrm
            unused_media = session.scalars(
                select(MediaOrm)
                .outerjoin(MediaInstanceOrm)
                .where(MediaInstanceOrm.id.is_(None))
            ).all()

            if not unused_media:
                return

            for media in unused_media:
                # физически удаляем файл
                if media.local_path and os.path.exists(media.local_path):
                    try:
                        os.remove(media.local_path)
                        logger.info("[Очистка хранилища] Удален неиспользуемый файл %s", media.local_path)
                    except Exception as file_err:
                        logger.error("[Очистка хранилища] Не удалось физически удалить файл %s: %s", media.local_path, file_err)

                # удаляем саму строку из таблицы medias
                session.delete(media)

            session.commit()

        except Exception as e:
            logger.error("[Очистка хранилища] Ошибка при сборке мусора вложений: %s", e)
            session.rollback()


    def _execute_single_delivery(self, session, d):
        # подготовка: адаптер и данные из payload
        adapter = self.adapters.get(d.target_source_id)
        download_adapter = self.adapters.get(d.origin_source_id)
        if not adapter:
            raise Exception(f"Адаптер для source_id {d.target_source_id} не найден!")

        if not download_adapter:
            raise Exception(f"Адаптер для source_id {d.origin_source_id} не найден!")

        payload = d.payload or {}
        content = payload.get("content", "")
        attachments = payload.get("attachments", [])

        current_ext_post_id = None

        # ищем инстанс поста в целевом источнике (для редактирования/удаления)
        p_inst = session.scalar(
            select(PostInstanceOrm).filter_by(
                post_id=d.post_id,
                source_id=d.target_source_id
            ).options(joinedload(PostInstanceOrm.post))
        )

        # логика обработки медиа
        media_act_ids_for_post = [] # для апи (actual_upload_id)
        media_ext_ids_for_post = [] # для бд (source_ext_id)

        has_error = False

        for attr in attachments:
            ext_id_origin = attr['external_id']

            # проверяем, знаем ли мы это медиа вообще
            m_inst_origin = session.scalar(
                select(MediaInstanceOrm).filter_by(
                    external_media_id=ext_id_origin,
                    source_id=d.origin_source_id
                )
            )

            if m_inst_origin:
                media_id = m_inst_origin.media_id
            else:
                # файл новый: скачиваем и создаем MediaOrm
                logger.info("Скачиваю новое медиа %s", ext_id_origin)
                media_data = download_adapter.download_single_attachment(attr['raw_data'])

                if not media_data:
                    continue

                new_media = MediaOrm(
                    post_id=d.post_id,
                    local_path=media_data['local_path'],
                    type=media_data['type'],
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_media)
                session.flush()  # получаем new_media.id

                # запоминаем инстанс для источника-оригинала (чтобы опознать файл в будущем)
                origin_post_inst = session.scalar(
                    select(PostInstanceOrm).filter_by(post_id=d.post_id, source_id=d.origin_source_id)
                )

                session.add(MediaInstanceOrm(
                    media_id=new_media.id,
                    source_id=d.origin_source_id,
                    external_media_id=ext_id_origin,
                    actual_upload_media_id=ext_id_origin,
                    post_instance_id=origin_post_inst.id if origin_post_inst else None
                ))
                media_id = new_media.id

            # загружаем в целевой источник
            m_inst_target = session.scalar(
                select(MediaInstanceOrm).filter_by(
                    media_id=media_id,
                    source_id=d.target_source_id
                )
            )

            if m_inst_target:
                media_ext_ids_for_post.append(m_inst_target.external_media_id)
                media_act_ids_for_post.append(m_inst_target.actual_upload_media_id)
            else:
                media_obj = session.get(MediaOrm, media_id)
                logger.info("Загружаю %s на источник %s", media_obj.local_path, d.target_source_id)

                ids = adapter.get_uploaded_media_id(media_obj.local_path)

                self.stop_event.wait(1)

                if not ids:  # если вернулся пустой словарь из-за ошибки сети
                    logger.warning("Ошибка загрузки - пропуск вложения")
                    has_error = True
                    continue

                new_ext_id = ids.get("source_ext_id")
                actual_upload_id = ids.get("actual_upload_id")

                if new_ext_id:
                    # создаем инстанс для целевого источника (пока без post_instance_id для create)
                    new_m_inst = MediaInstanceOrm(
                        media_id=media_id,
                        source_id=d.target_source_id,
                        external_media_id=new_ext_id,
                        actual_upload_media_id=actual_upload_id,
                        post_instance_id=p_inst.id if p_inst else None
                    )
                    session.add(new_m_inst)
                    session.flush()
                    media_ext_ids_for_post.append(new_ext_id)
                    media_act_ids_for_post.append(actual_upload_id)


        # исполнение действия

        # сразу уходим на повтор, без попытки отправки пустого запроса
        if not content and not media_act_ids_for_post and d.action != "delete":
            logger.warning("Пост пустой (нет текста и вложения упали). Отправляем на повтор.")
            # доставка на создание остается
            raise RuntimeError("Не удалось загрузить вложения для пустого поста.")

        # запоминаем исходное действие для корректной пост-обработки
        initial_action = d.action

        if d.action == "create":
            current_ext_post_id = adapter.create_post(content, media_ids=media_act_ids_for_post)

            new_p_inst = PostInstanceOrm(
                post_id=d.post_id,
                source_id=d.target_source_id,
                external_post_id=current_ext_post_id,
                last_synced_at=datetime.now(timezone.utc),
                inst_updated_at=datetime.now(timezone.utc)
            )
            session.add(new_p_inst)
            session.flush()
            p_inst_id = new_p_inst.id


        elif d.action == "update":
            if not p_inst:
                raise Exception("Ошибка: Инстанс для обновления не найден")

            current_ext_post_id = p_inst.external_post_id
            adapter.update_post(current_ext_post_id, content, media_ids=media_act_ids_for_post)
            p_inst.last_synced_at = datetime.now(timezone.utc)
            p_inst.post.content = content
            p_inst_id = p_inst.id


        elif d.action == "delete":
            if p_inst:
                adapter.delete_post(p_inst.external_post_id)
                session.delete(p_inst)

                d.status = "sent"


        # пост-обработка (связи и актуализация айди)
        if d.action in ["create", "update"] and current_ext_post_id:
            # привязываем медиа к посту по айди
            session.execute(
                update(MediaInstanceOrm)
                .where(MediaInstanceOrm.source_id == d.target_source_id)
                .where(MediaInstanceOrm.external_media_id.in_(media_ext_ids_for_post))
                .values(post_instance_id=p_inst_id)
            )

            # если update - удаляем то, что исчезло из поста
            if d.action == "update":
                session.execute(
                    delete(MediaInstanceOrm)
                    .where(MediaInstanceOrm.post_instance_id == p_inst_id)
                    .where(MediaInstanceOrm.external_media_id.notin_(media_ext_ids_for_post))
                )

            # актуализация для вк (разные айди вложений дает при создании и получении)
            if adapter.type == "vk":
                self.stop_event.wait(3)  # пауза для переиндексации вк

                try:
                    actual_post_data = adapter.get_post_by_id(current_ext_post_id)

                    if actual_post_data and 'attachments' in actual_post_data:
                        actual_atts = actual_post_data['attachments']
                        # синхронизируем айди по порядку вложений
                        for i in range(min(len(actual_atts), len(media_ext_ids_for_post))):
                            new_real_id = actual_atts[i]['external_id']
                            old_tmp_id = media_ext_ids_for_post[i]

                            if new_real_id != old_tmp_id:
                                logger.info("Корректировка id вложений для вк %s -> %s", old_tmp_id, new_real_id)
                                session.execute(
                                    update(MediaInstanceOrm)
                                    .where(MediaInstanceOrm.source_id == d.target_source_id)
                                    .where(MediaInstanceOrm.external_media_id == old_tmp_id)
                                    .values(external_media_id=new_real_id)
                                )
                except Exception as e:
                    logger.error("Не удалось актуализировать id. Тип ошибки: %s. Описание: %s", type(e).__name__, e)


        # финализация статусов задачи
        if initial_action in ["create", "update"]:
            if has_error:
                # на следующий круг задача идет как update
                d.action = "update"
                d.newest_update_time = datetime.now(timezone.utc) - timedelta(days=1)
                raise RuntimeError("Пост обработан частично, часть вложений пропущена. Отправка на повтор.")
            else:
                # если всё загрузилось успешно - закрываем задачу
                d.status = "sent"

        session.commit()
        # задержка между доставками для обхода лимитов
        self.stop_event.wait(1.5 + d.retries * 2)
