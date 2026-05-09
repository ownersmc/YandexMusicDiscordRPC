# Yandex Music Discord Presence

Windows-клиент, который показывает текущий трек из Яндекс Музыки в Discord Rich Presence:

- название трека;
- исполнитель;
- обложка текущего трека;
- живой таймер позиции трека через Discord timestamps;
- кнопку со ссылкой на текущий трек.

Клиент читает данные из системной медиасессии Windows. Поэтому он работает с приложением Яндекс Музыка и с браузером, если браузер отдает трек в Windows media controls.

## Быстрый запуск

Для обычного пользователя Python не нужен.

1. Скачайте `YandexMusicDiscordRPC-portable.zip` из GitHub Releases.
2. Распакуйте архив.
3. Откройте Discord.
4. Откройте Яндекс Музыку и включите трек.
5. Запустите `YandexMusicDiscordRPC.exe`.

Окно exe нужно оставить открытым, пока нужна активность в Discord. При первом запуске рядом с exe создастся `config.json`; обычно его менять не нужно.

Discord должен быть открыт на этом же компьютере. В настройках Discord включите активность: `Settings -> Activity Privacy -> Share your detected activities with others`.

## Запуск Из Исходников

Если вы запускаете проект из исходников, используйте:

```bat
run.bat
```

Этот способ сам создаст `.venv` и установит зависимости.

## Обложки

Клиент сначала ищет публичную HTTPS-обложку трека через Яндекс Музыку и отправляет ее в Discord как `large_image`.

Если публичная обложка не найдена, клиент может взять `thumbnail` из Windows media session, сохранить обложку в `cache/covers` и отдать ее по локальному HTTP:

```text
http://127.0.0.1:17654/cover/...
```

Discord Rich Presence принимает картинки как URL. Если локальная обложка не отображается в Discord или не видна другим людям, нужен публичный HTTPS URL, который прокидывает этот локальный сервер. Например, через Cloudflare Tunnel или ngrok, после чего нужно указать адрес в `config.json`:

```json
{
  "cover_art": {
    "public_base_url": "https://your-tunnel.example.com"
  }
}
```

Если обложка не пришла из Windows, можно поставить запасную картинку:

```json
{
  "cover_art": {
    "fallback_large_image": "https://example.com/yandex-music.png"
  }
}
```

## Таймер

По умолчанию используется:

```json
{
  "activity_type": "listening",
  "timestamp_mode": "both",
  "seek_update_threshold_seconds": 3,
  "loading_grace_seconds": 45
}
```

Это повторяет подход `WinYandexMusicRPC`: Discord получает activity type `LISTENING`, а также `start` и `end` timestamps. Так Discord сам показывает доступный ему прогресс трека, включая секунды/минуты в реальном времени. Другие варианты:

- `remaining` - показывать оставшееся время;
- `both` - передавать `start` и `end`;
- `none` - выключить таймер.

Если вы вручную перемотали трек, клиент сравнит новый timestamp с уже отправленным. Если разница больше `seek_update_threshold_seconds`, presence обновится один раз и Discord переставит таймер.

Если Яндекс Музыка долго грузит следующий трек и временно не отдает медиаданные, клиент держит прошлую активность еще `loading_grace_seconds` секунд, а не очищает Discord RPC сразу.

## Пауза

Когда трек стоит на паузе, клиент убирает бегущий `start/end` и показывает отдельный текст:

```json
{
  "pause": {
    "update_interval_seconds": 5,
    "state_template": "На паузе {pause_elapsed} • {progress}",
    "small_text_template": "На паузе {pause_elapsed}"
  }
}
```

Пример строки: `На паузе 0:12 • 1:10 / 2:06`.

Discord не дает обычным Rich Presence-клиентам рисовать настоящую полоску плеера как в Яндекс Музыке. Если нужна текстовая имитация, ее можно включить в `state`:

```json
{
  "status_template": {
    "state": "{artist} • {progress_bar}"
  },
  "progress_bar": {
    "enabled": true,
    "length": 12,
    "filled_char": "━",
    "empty_char": "─",
    "cursor_char": "●",
    "show_time": true
  }
}
```

## Кнопка Трека

Клиент ищет текущий трек в Яндекс Музыке и добавляет первую кнопку со ссылкой на него:

```json
{
  "track_button": {
    "enabled": true,
    "label": "Открыть трек"
  }
}
```

Вторая кнопка берется из `buttons`. Discord поддерживает максимум две кнопки.

## Конфиг

- `source_filters` - строки, по которым клиент выбирает медиасессию Яндекс Музыки. Если слушаете через Chrome/браузер и клиент не видит трек, временно поставьте `[]`, чтобы брать текущую медиасессию Windows.
- `show_when_paused` - показывать ли статус, когда музыка на паузе.
- `status_template.details` и `status_template.state` - шаблоны текста. Доступны `{title}`, `{artist}`, `{album}`, `{source}`, `{position}`, `{duration}`, `{progress}`, `{progress_bar}`, `{status}`.
- `activity_type` - тип активности Discord. Для музыки лучше `listening`, как в `WinYandexMusicRPC`.
- `seek_update_threshold_seconds` - насколько сильно должна измениться позиция трека, чтобы клиент понял ручную перемотку.
- `loading_grace_seconds` - сколько секунд держать последний RPC, если Яндекс Музыка временно не отдает трек при загрузке.
- `pause` - шаблоны и интервал обновления статуса, когда музыка стоит на паузе.
- `progress_bar` - текстовая имитация полоски времени.
- `track_button` - кнопка текущего трека.
- `cover_art.host` и `cover_art.port` - адрес локального сервера обложек.
- `cover_art.search_yandex_public_cover` - искать публичную HTTPS-обложку через Яндекс Музыку.
- `assets.small_image_playing` и `assets.small_image_paused` - необязательные маленькие картинки из Discord Rich Presence Art Assets.
- `buttons` - кнопки в статусе. Discord поддерживает максимум две.

## Сборка Portable exe

Для локальной сборки запустите:

```bat
build_release.bat
```

Скрипт соберет:

- `release/YandexMusicDiscordRPC.exe`;
- `release/README_START.txt`;
- `YandexMusicDiscordRPC-portable.zip`.

В GitHub Actions уже есть workflow `.github/workflows/build-windows.yml`, который собирает такой же portable-архив вручную или при push тега `v*`.
