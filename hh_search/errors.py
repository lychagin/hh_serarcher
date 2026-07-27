class HhSearchError(Exception):
    """Базовая ошибка приложения."""


class AccessForbidden(HhSearchError):
    """Источник ответил 403. Прогон останавливается, обходить запрет нельзя."""


class FetchFailed(HhSearchError):
    """Не удалось получить ресурс после всех повторов."""


class RobotsDisallowed(HhSearchError):
    """robots.txt запрещает обращение к этому пути."""
