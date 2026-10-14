# AnimeLib для Kodi

- Аддон: `plugin.video.animelib/`, репозиторий Kodi: `repository.animelib/`.
- Пользователь получает обновления через Kodi-репозиторий: каждый push в ветку по умолчанию
  запускает `.github/workflows/publish.yml`, который собирает `build.py --repo` и публикует в ветку `gh-pages`.
- **При любом изменении кода аддона поднимай `version` в `plugin.video.animelib/addon.xml`
  и добавляй строку в `<news>`.** Без этого Kodi не увидит обновление.
  Если меняется `repository.animelib/`, поднимай его версию тоже.
- Проверка перед коммитом: `python3 -m compileall -q plugin.video.animelib` и `python3 build.py --repo /tmp/site`.
- Код совместим с Kodi 19+ (Python 3, только стандартная библиотека).
- Не используй потоки (`threading`, `concurrent.futures`) в коде плагина: встроенный Python 3.8 в Kodi
  на Windows падает (подтверждено crash-дампами). Для параллельных запросов — `resources/lib/parallel.py`.
- Не держи плагин запущенным после `setResolvedUrl`: всё, что следит за воспроизведением, — в `service.py`.
