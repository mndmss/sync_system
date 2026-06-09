import requests
import time
import os
import logging

logger = logging.getLogger("sync_system")


class BaseAPI:
    def create_post(self, message): ...
    def update_post(self, post_id, message): ...
    def delete_post(self, post_id): ...
    def get_post_by_id(self, post_id): ...
    def get_posts(self, limit): ...

    def download_single_attachment(self, attachment): ...
    def get_uploaded_media_id(self, filepath): ...


class VKApi(BaseAPI):

    BASE_URL = "https://api.vk.com/method/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

    def __init__(self, token, source_type = "vk", owner_id=None):
        self.__token = token
        self.__owner_id = owner_id
        self.type = source_type

    @property
    def owner_id(self):
        return self.__owner_id


    def create_post(self, message, media_ids=None):
        data = {
            "message": message,
            "from_group": 1,
            "primary_attachments_mode": "grid"
        }

        if media_ids:
            data["attachments"] = ",".join(media_ids)

        resp = self._request("wall.post", data=data)

        logger.info("Пост создан, id: %s", resp.get('response').get('post_id'))
        return resp.get("response").get("post_id")


    def update_post(self, post_id, message, media_ids=None):
        data = {
            "post_id": post_id,
            "message": message,
            "primary_attachments_mode": "grid"
        }

        if media_ids:
            data["attachments"] = ",".join(media_ids)

        resp = self._request("wall.edit", data=data,)

        logger.info("Пост отредактирован, id: %s", resp.get('response').get('post_id'))
        return resp


    def delete_post(self, post_id):
        data = {"post_id": post_id}
        resp = self._request("wall.delete", data=data)

        if resp.get("response") == 1:
            logger.info("Пост удален, id: %s", post_id)
        return resp


    def get_post_by_id(self, post_id):
        params = {"posts": f"{self.__owner_id}_{post_id}"}
        resp = self._request("wall.getById", req_method="GET", params=params)

        item = resp.get("response", {}).get("items", [])[0]
        formatted_attachments = self._format_attachments(item.get("attachments", []))

        return {
                "id": item.get("id"),
                "text": item.get("text"),
                "date": item.get("date"),
                "edited": item.get("edited", item.get("date")),
                "attachments": formatted_attachments
            }


    def get_posts(self, limit):
        params = {"count": limit}

        resp = self._request("wall.get", req_method="GET", params=params)

        posts = []
        for item in resp.get("response", {}).get("items", []):
            # обрабатываем вложения в нужный формат
            formatted_attachments = self._format_attachments(item.get("attachments", []))

            posts.append({
                "id": item.get("id"),
                "text": item.get("text"),
                "date": item.get("date"),
                "edited": item.get("edited", item.get("date")),
                "attachments": formatted_attachments
            })
        return posts


    @staticmethod
    def _format_attachments(raw_attachments):
        """Приводит разные типы вложений вк к общему виду"""
        formatted = []
        for attach in raw_attachments:
            a_type = attach.get("type")
            data = attach.get(a_type)
            if not data:
                continue

            # формируем базовый объект вложения
            res = {
                "type": a_type,
                "external_id": f"{a_type}{data.get('owner_id')}_{data.get('id')}",
                "raw_data": attach  # оригинал для адаптера
            }
            formatted.append(res)
        return formatted



    @staticmethod
    def _download_video_or_file(filename, best_url, headers, max_retries=3):
        for attempt in range(max_retries):
            try:
                with requests.get(best_url, stream=True, headers=headers, timeout=60) as r:
                    r.raise_for_status()
                    with open(filename, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024 ** 2):
                            if chunk:
                                f.write(chunk)
                logger.info("Успешно скачано: %s", filename)
                return True

            except (requests.exceptions.RequestException, IOError) as e:
                logger.error("Попытка скачивания %s для файла %s не удалась. Тип ошибки: %s. Описание: %s", attempt + 1, filename, type(e).__name__, e)
                if attempt == max_retries - 1:
                    logger.error("Все попытки для скачивания файла %s исчерпаны. Тип ошибки: %s. Описание: %s",
                                filename, type(e).__name__, e)

                # чистим битый файл при ошибке
                if os.path.exists(filename):
                    os.remove(filename)

                time.sleep(2)

        return False


    def download_single_attachment(self, attachment):
        os.makedirs("storage", exist_ok=True)

        attachment_type = attachment.get("type")
        filename = None
        ext_id = None

        # photo
        if attachment_type == "photo":
            photo = attachment.get("photo")
            ext_id = f"photo{photo.get('owner_id')}_{photo.get('id')}"
            best_photo_url = max(photo.get("sizes"), key=lambda x: x.get("width", 0))["url"]

            img_data = self._safe_vk_request(
                best_photo_url,
                method="GET",
                timeout=30,
                is_api=False,
                headers=self.headers
            )

            if img_data:
                filename = f"storage/{ext_id}.jpg"
                with open(filename, 'wb') as f:
                    f.write(img_data)

                logger.info("Успешно скачано: %s", filename)

        # video
        elif attachment_type == "video":
            video = attachment.get("video")
            video_id = f"{video.get('owner_id')}_{video.get('id')}"
            ext_id = f"video{video_id}"

            video_info = self._safe_vk_request(
                f"{self.BASE_URL}video.get",
                method="POST",
                data={
                    "videos": video_id,
                    "access_token": self.__token,
                    "v": "5.199"
                },
                headers=self.headers
            )

            items = video_info.get("response", {}).get("items", [])
            if items and "files" in items[0]:

                files = items[0].get("files")

                mp4_keys = sorted([k for k in files if k.startswith('mp4_')],
                                  key=lambda x: int(x.split('_')[1]))
                if mp4_keys:
                    # оставляем только mp4, сортируем по разрешению и берем последний элемент
                    best_video_url = files[mp4_keys[-1]]
                    filename = f"storage/{ext_id}.mp4"

                    self._download_video_or_file(filename, best_video_url, self.headers)

                else:
                    logger.warning("Прямых ссылок для  %s нет (возможно, это сторонний ресурс или live-трансляция)", video_id)

        # doc
        elif attachment_type == "doc":
            doc = attachment.get("doc")
            file_url = doc.get("url")
            ext_id = f"doc{doc.get('owner_id')}_{doc.get('id')}"

            if file_url:
                filename = f"storage/{ext_id}.{doc.get('ext', 'dat')}"
                self._download_video_or_file(filename, file_url, self.headers)

        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            return {"local_path": filename, "type": attachment_type, "external_id": ext_id}

        return None


    @staticmethod
    def _get_media_type(filepath):
        photo = ('jpg', 'png', 'gif')
        video = ('avi', 'mp4', '3gp', 'mpeg', 'mov', 'mp3', 'flv', 'wmv')

        if filepath.lower().split(".")[-1] in photo:
            return "photo"
        elif filepath.lower().split(".")[-1] in video:
            return "video"
        else:
            return "doc"


    def get_uploaded_media_id(self, filepath):

        base_params = {
            "access_token": self.__token,
            "v": "5.199",
        }

        if self.__owner_id < 0:
            base_params["group_id"] = abs(self.__owner_id)

        media_type = self._get_media_type(filepath)

        if media_type == "photo":

            # получение ссылки для загрузки фото
            server_resp = self._safe_vk_request(
                "https://api.vk.com/method/photos.getWallUploadServer",
                method="POST",
                data=base_params,
                headers=self.headers
            )

            # проверка: если сервер не ответил
            if not server_resp or "response" not in server_resp:
                return {}  # возвращаем пустой словарь вместо None

            upload_url = server_resp.get("response").get("upload_url")

            # передача фото на сервер
            with open(filepath, 'rb') as file:
                files = {'photo': file}

                upload_resp = self._safe_vk_request(
                    upload_url,
                    method="POST",
                    files=files,
                    headers=self.headers
                )

            # проверка: если загрузка не удалась (таймаут)
            if not upload_resp:
                return {}

            time.sleep(0.3)

            # сохранение фото на сервере
            save_params = base_params | upload_resp

            final_resp = self._safe_vk_request(
                "https://api.vk.com/method/photos.saveWallPhoto",
                method="POST",
                data=save_params,
                headers=self.headers
            )

            # проверка: если не получилось сохранить фото
            if not final_resp or "response" not in final_resp:
                return {}


            photo = final_resp.get('response')[0]
            return {
                "source_ext_id": f"photo{self.__owner_id}_{photo.get('id')}",           # для синхронизации
                "actual_upload_id": f"photo{photo.get('owner_id')}_{photo.get('id')}"   # для wall.post
            }


        elif media_type == "video":

            # получение ссылки для загрузки видео
            server_resp = self._safe_vk_request(
                "https://api.vk.com/method/video.save",
                method="POST",
                data=base_params,
                headers=self.headers
            )

            # проверка: если сервер не ответил
            if not server_resp or "response" not in server_resp:
                return {}  # возвращаем пустой словарь вместо None

            res = server_resp.get("response")
            upload_url, owner_id, video_id = res.get("upload_url"), res.get("owner_id"), res.get("video_id")

            # передача видео на сервер
            with open(filepath, 'rb') as file:
                files = {'video_file': file}

                upload_headers = {
                    "User-Agent": self.headers["User-Agent"]
                }

                upload_resp = self._safe_vk_request(
                    upload_url,
                    method="POST",
                    files=files,
                    headers=upload_headers,
                    timeout=600
                )

            # проверка: если загрузка не удалась (таймаут)
            if not upload_resp:
                return {}

            time.sleep(0.3)

            if upload_resp.get("video_id") == video_id:
                return {
                    "source_ext_id": f"video{self.__owner_id}_{video_id}",      # для синхронизации
                    "actual_upload_id": f"video{owner_id}_{video_id}"           # для wall.post
                }
            else:
                raise Exception(f"Что-то пошло не так при загрузкой видео, id: {video_id}")

        else:

            # получение ссылки для загрузки файла
            server_resp = self._safe_vk_request(
                "https://api.vk.com/method/docs.getWallUploadServer",
                method="POST",
                data=base_params,
                headers=self.headers
            )

            # проверка: если сервер не ответил
            if not server_resp or "response" not in server_resp:
                return {}  # возвращаем пустой словарь вместо None

            upload_url = server_resp.get("response").get("upload_url")

            # передача файла на сервер
            with open(filepath, 'rb') as file:
                files = {'file': file}

                upload_resp = self._safe_vk_request(
                    upload_url,
                    method="POST",
                    files=files,
                    headers=self.headers
                )

            # проверка: если загрузка не удалась (таймаут)
            if not upload_resp:
                return {}

            time.sleep(0.3)

            save_params = base_params | upload_resp

            final_resp = self._safe_vk_request(
                "https://api.vk.com/method/docs.save",
                method="POST",
                data=save_params,
                headers=self.headers
            )

            # проверка: если не получилось сохранить документ
            if not final_resp or "response" not in final_resp:
                return {}

            doc = final_resp.get('response').get('doc')

            return {
                "source_ext_id": f"doc{self.__owner_id}_{doc.get('id')}",           # для синхронизации
                "actual_upload_id": f"doc{doc.get('owner_id')}_{doc.get('id')}"     # для wall.post
            }


    def _request(self, method_name, req_method="POST", timeout=10, is_api=True,  **kwargs):
        url = self.BASE_URL + method_name

        payload = kwargs.get("data") or kwargs.get("params") or {}

        common = {
            "owner_id": self.__owner_id,
            "access_token": self.__token,
            "v": "5.199"
        }
        payload |= common

        if req_method.upper() == "GET":
            req_kwargs = {"params": payload}
        else:
            req_kwargs = {"data": payload}


        resp = self._safe_vk_request(
            url,
            req_method,
            headers=self.headers,
            timeout=timeout,
            is_api=is_api,
            **req_kwargs
        )

        if resp is None:
            raise Exception("VK API: no response")

        if "error" in resp:
            raise Exception(f"VK API error: {resp['error']['error_msg']}")

        return resp


    @staticmethod
    def _safe_vk_request(url, method, timeout=10, is_api=True, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                resp = requests.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs
                )
                resp.raise_for_status()

                return resp.json() if is_api else resp.content

            except requests.exceptions.HTTPError as e:
                logger.warning("HTTP ERROR: %s, retry %s", e, attempt + 1)
                time.sleep(2)

            except requests.exceptions.SSLError as e:
                logger.warning("SSL ERROR: %s, retry %s", e, attempt + 1)
                time.sleep(2)

            except requests.exceptions.Timeout as e:
                logger.warning("TIMEOUT: %s, retry %s", e, attempt + 1)
                time.sleep(2)

            except Exception as e:
                logger.error("REQUEST ERROR. Тип ошибки: %s. Описание: %s", type(e).__name__, e)
                time.sleep(2)

        return None


class NSApi(BaseAPI):
    BASE = "http://localhost:8000"

    def __init__(self, token, source_type="news_site"):
        self.type = source_type
        self.HEADERS = {"X-API-Key": token}


    def create_post(self, message, media_ids=None):
        data = {
            "content": message,
        }

        if media_ids:
            data["media_ids"] = list(map(int, media_ids))

        resp = requests.post(
        f"{self.BASE}/api/posts/",
            headers=self.HEADERS | {"Content-Type": "application/json"},
            json=data
        ).json()
        logger.info("Пост создан, id: %s", resp.get("id"))

        return str(resp.get("id"))


    def update_post(self, post_id, message, media_ids=None):
        data = {
            "content": message,
        }

        if media_ids:
            data["media_ids"] = list(map(int, media_ids))

        resp = requests.put(
            f"{self.BASE}/api/posts/{post_id}/",
            headers=self.HEADERS | {"Content-Type": "application/json"},
            json=data
        ).json()

        logger.info("Пост отредактирован, id: %s", resp.get("id"))
        return str(resp.get("id"))


    def delete_post(self, post_id):
        resp = requests.delete(f"{self.BASE}/api/posts/{post_id}/", headers=self.HEADERS).json()
        if resp == 1:
            logger.info("Пост удален, id: %s", post_id)
        return resp


    def get_posts(self, limit):
        r = requests.get(f"{self.BASE}/api/posts/?limit={limit}", headers=self.HEADERS)
        resp = r.json()["results"]

        posts = []

        for post in resp:
            formatted_attachments = self._format_attachments(post.get("media", []))

            posts.append({
                "id": post.get("id"),
                "text": post.get("content"),
                "date": post.get("created_at"),
                "edited": post.get("updated_at"),
                "attachments": formatted_attachments
            })

        return posts

    @staticmethod
    def _format_attachments(raw_attachments):
        formatted = []
        for attach in raw_attachments:
            a_type = attach.get("file_type")

            # формируем базовый объект вложения
            res = {
                "type": a_type,
                "external_id": f"{attach.get('id')}",
                "raw_data": attach  # сохраняем оригинал
            }
            formatted.append(res)
        return formatted


    def download_single_attachment(self, attachment):
        media_id, attachment_type = attachment.get("id"), attachment.get("file_type")
        attachment_ext = attachment.get("file_name").split(".")[1]

        os.makedirs("storage", exist_ok=True)

        if attachment_type == "image":
            r = requests.get(f"{self.BASE}/api/media/{media_id}/download/", headers=self.HEADERS, stream=True)
            filename = f"storage/image_{media_id}.jpg"
            with open(filename, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            logger.info("Успешно скачано: %s", filename)

        elif attachment_type == "video":
            r = requests.get(f"{self.BASE}/api/media/{media_id}/download/", headers=self.HEADERS, stream=True)
            filename = f"storage/video_{media_id}.mp4"
            with open(filename, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            logger.info("Успешно скачано: %s", filename)

        else:
            r = requests.get(f"{self.BASE}/api/media/{media_id}/download/", headers=self.HEADERS, stream=True)
            filename = f"storage/image_{media_id}.{attachment_ext}"
            with open(filename, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            logger.info("Успешно скачано: %s", filename)

        return {"local_path": filename, "type": attachment_type, "external_id": media_id} if filename else None


    def get_uploaded_media_id(self, filepath):
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{self.BASE}/api/media/upload/",
                headers=self.HEADERS,
                files={"file": f}
            ).json()
        media_id = resp.get("id")

        return {
            "source_ext_id": f"{media_id}",
            "actual_upload_id": f"{media_id}"
        }
