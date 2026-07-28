class HhSearchError(Exception):
    """Базовая ошибка приложения."""


class AccessForbidden(HhSearchError):
    """Источник ответил 403. Прогон останавливается, обходить запрет нельзя."""


class StorageUnavailable(HhSearchError):
    """Каталог данных не читается или не пишется: права, том :ro, диск.

    Отдельный тип, а не голый `OSError`, потому что сообщение к нему
    приложено готовое: самая вероятная причина — несовпадение uid
    контейнера с владельцем тома, и об этом обязан узнать владелец, а не
    только traceback.
    """


class FetchFailed(HhSearchError):
    """Не удалось получить ресурс после всех повторов."""


class RobotsDisallowed(HhSearchError):
    """robots.txt запрещает обращение к этому пути."""
