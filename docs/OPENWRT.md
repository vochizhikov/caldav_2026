# Запуск бота в Docker на OpenWrt

Образ содержит Python 3.12, код бота, зависимости и Alembic-миграции. Telegram работает через polling; открывать порты и настраивать webhook не нужно. База находится в `/data/calendar_bot.db`, секреты передаются при запуске контейнера.

## Быстрый запуск на роутере x86-64 с Docker

Для роутера x86-64 нужен образ `linux/amd64`. В примере ниже замените `192.168.1.1` на адрес своего роутера, а путь к проекту — на свой. Если уже есть загруженный на роутер `yandex-calendar-bot:latest`, пересобирать образ и переносить архив не требуется. Изменения в `compose.yaml` и этой инструкции не входят в образ. Если исходный код бота менялся после последней сборки, выполните **на компьютере в PowerShell**:

```powershell
cd C:\path\to\caldav_2026
.\docker\build-image.ps1 -Platform linux/amd64
scp .\dist\yandex-calendar-bot-amd64.tar .\dist\yandex-calendar-bot-amd64.tar.sha256 root@192.168.1.1:/root/
```

Затем **на роутере по SSH**, только если переносили новый архив:

```sh
cd /root
sha256sum -c yandex-calendar-bot-amd64.tar.sha256 && docker load -i yandex-calendar-bot-amd64.tar
```

Перед запуском убедитесь, что сохранены `/root/calendar-bot/.env.docker` с прежними `BOT_TOKEN` и `ENCRYPTION_KEY`, а также `/root/calendar-bot/data` с базой SQLite. Не печатайте значения секретов в терминал или чат. Если контейнер `elated_chatterjee` находится в состоянии `Created` и в LuCI у него в колонке Command написано `-p 5438:5432`, удалите **только этот ошибочно созданный контейнер**: `docker rm elated_chatterjee`. Проброс `-p` здесь не нужен: как и у `todo_bot`, используется сеть `host`. Старому контейнеру `yandex-calendar-bot` сначала отключите автозапуск, затем остановите и переименуйте его для отката. При политике `always` одно только ручное выключение не предотвращает запуск старой копии после перезапуска Docker.

```sh
docker update --restart=no yandex-calendar-bot
docker stop -t 30 yandex-calendar-bot
docker rename yandex-calendar-bot yandex-calendar-bot-before-recreate
```

Если контейнера с именем `yandex-calendar-bot` уже нет, эти две команды не нужны. Данные остаются в `/root/calendar-bot/data`. Если имя `yandex-calendar-bot-before-recreate` уже занято, выберите другое свободное имя для старого контейнера. Затем выполните:

```sh
docker run -d \
  --name yandex-calendar-bot \
  --network host \
  --restart always \
  --env-file /root/calendar-bot/.env.docker \
  -e DATABASE_URL=sqlite+aiosqlite:////data/calendar_bot.db \
  --mount type=bind,src=/root/calendar-bot/data,dst=/data \
  --read-only \
  --tmpfs /tmp:rw,nosuid,noexec,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  --log-driver json-file \
  --log-opt max-size=1m \
  --log-opt max-file=3 \
  yandex-calendar-bot:latest
```

Проверьте: `docker ps --filter name=yandex-calendar-bot`, `docker inspect --format 'Network={{.HostConfig.NetworkMode}} Command={{json .Config.Cmd}}' yandex-calendar-bot` и `docker logs --tail 30 yandex-calendar-bot`. Ожидаются сеть `host` и команда `["python","-m","bot"]`. Если в логах снова есть `TelegramNetworkError`, нужно проверить доступ роутера к `api.telegram.org:443`; смена режима Docker сама по себе не гарантирует подключения.

## 1. Определить архитектуру роутера

По SSH на OpenWrt выполните:

```sh
uname -m
docker version
free -m
df -h
```

| `uname -m` | Вариант сборки | Архив |
|---|---|---|
| `aarch64` | `linux/arm64` | `yandex-calendar-bot-arm64.tar` |
| `x86_64` | `linux/amd64` | `yandex-calendar-bot-amd64.tar` |
| `armv7l`, `armv6l`, `mips`, `mipsel` и другие | Эти готовые сборки не подходят | Нужна отдельная проверка устройства и зависимостей |

Процессор, Docker и образ должны поддерживать одну архитектуру. Наличие OpenWrt само по себе не означает, что любой Docker-образ запустится на устройстве. В частности, приведённый Dockerfile использует готовые бинарные Python-зависимости для ARM64/x86-64; сборка остальных архитектур этим скриптом намеренно не предлагается.

Способы выбора платформы описаны в [документации Docker Buildx](https://docs.docker.com/build/building/multi-platform/).

## 2. Подготовить Docker и постоянное хранилище

Если Docker уже работает и `docker version` показывает раздел Server, повторная установка не нужна.

Если он не установлен, установите `dockerd` и клиент `docker` из официальных репозиториев своей версии OpenWrt. Например, для прошивки с `opkg`:

```sh
opkg update
opkg install dockerd docker
/etc/init.d/dockerd enable
/etc/init.d/dockerd start
```

Для прошивки с `apk` команды установки:

```sh
apk update
apk add dockerd docker
/etc/init.d/dockerd enable
/etc/init.d/dockerd start
```

Используйте один вариант, соответствующий установленному менеджеру пакетов. Если пакеты отсутствуют или не запускается Docker Engine, сначала требуется подготовить подходящую прошивку/ядро для конкретного роутера. Установка одного клиента `docker` не заменяет сервер `dockerd`.

Образы Docker и SQLite должны находиться на постоянном носителе с достаточным свободным местом. Для роутера с небольшой внутренней flash обычно используют подключённый накопитель с ext4. В LuCI/Dockerman путь Docker Root Dir задаётся в настройках Docker; это отдельный путь от каталога базы бота. Подробности — в [инструкции OpenWrt по Docker](https://openwrt.org/docs/guide-user/virtualization/docker_host).

Далее используется **пример** каталога `/mnt/sda1/calendar-bot`. Замените его на свой реально смонтированный постоянный накопитель. Не используйте `/tmp` для базы: он не предназначен для постоянного хранения.

```sh
df -h /mnt/sda1
mkdir -p /mnt/sda1/calendar-bot/data
chown 10001:10001 /mnt/sda1/calendar-bot/data
chmod 700 /mnt/sda1/calendar-bot/data
```

Контейнер работает от UID/GID `10001:10001`. Права нужны каталогу целиком: SQLite создаёт рядом с базой файлы `-wal` и `-shm`.

Оставьте запас памяти для маршрутизации и других сервисов. Фактическое потребление бота зависит от количества событий и повторений; измерить его после запуска можно через `docker stats --no-stream yandex-calendar-bot`. Размер архива и потребление RAM — разные величины.

## 3. Собрать образ на компьютере

На Windows нужен запущенный Docker Desktop в режиме **Linux containers**. Сборку выполняйте в корне этого проекта, не на роутере.

Для ARM64:

```powershell
.\docker\build-image.ps1 -Platform linux/arm64
```

Для x86-64:

```powershell
.\docker\build-image.ps1 -Platform linux/amd64
```

Скрипт:

1. Собирает образ для указанной платформы и загружает его в локальный Docker.
2. Проверяет архитектуру полученного образа.
3. Дважды запускает контейнер без внешней сети и без настоящих токенов.
4. Проверяет состав файлов образа, импорт модулей, шифрование, миграции, запись SQLite и сохранность записи после пересоздания контейнера.
5. Удаляет только свой временный тестовый volume.
6. Сохраняет проверенный образ в `dist/yandex-calendar-bot-<архитектура>.tar` и SHA-256 в соседний `.sha256`.

Оба архива содержат образ с тегом `yandex-calendar-bot:latest`, но разной архитектуры. На роутер переносится только подходящий архив. Скрипт не публикует образ в Docker Hub или другой registry.

`.dockerignore` допускает в контекст сборки только необходимые исходники. `.env`, `.env.docker`, рабочая БД, резервные копии, `.venv` и тестовые данные в образ не попадают.

## 4. Передать файлы на роутер

Пример PowerShell-команды для ARM64; адрес роутера и путь замените своими:

```powershell
scp .\dist\yandex-calendar-bot-arm64.tar .\dist\yandex-calendar-bot-arm64.tar.sha256 .\compose.yaml .\.env.docker.example root@192.168.1.1:/mnt/sda1/calendar-bot/
```

Для x86-64 замените `arm64` на `amd64`. Если OpenWrt отвечает `subsystem request failed`, его SSH-сервер может не предоставлять SFTP: в современном OpenSSH повторите `scp` с флагом `-O`.

По SSH на роутере:

```sh
cd /mnt/sda1/calendar-bot
sha256sum -c yandex-calendar-bot-arm64.tar.sha256
docker load -i yandex-calendar-bot-arm64.tar
docker image inspect yandex-calendar-bot:latest --format '{{.Os}}/{{.Architecture}}'
```

Команда `docker load` загружает образ из архива; она не запускает бота. Для x86-64 здесь также используются файлы `amd64`.

## 5. Настроить токен и ключ

Для новой установки:

```sh
cd /mnt/sda1/calendar-bot
cp .env.docker.example .env.docker
chmod 600 .env.docker
docker run --rm yandex-calendar-bot:latest python -m bot.generate_key
vi .env.docker
```

Заполните `BOT_TOKEN` и `ENCRYPTION_KEY`. Команда генерации только печатает новый ключ; его нужно сохранить в файле. Значения записываются без внешних кавычек. Остальные параметры имеют рабочие значения по умолчанию.

**Если переносите существующую базу, используйте прежний `ENCRYPTION_KEY`.** Новый ключ не расшифрует сохранённые пароли Яндекса. Сам токен и ключ не нужно добавлять в Dockerfile или передавать параметрами сборки.

`DATABASE_URL` в Docker должен указывать на `/data/calendar_bot.db`. В поставляемом Compose эта настройка задана явно и перекрывает относительный путь, если вы скопировали старый `.env`.

## 6. Запустить: выбрать один способ

### Вариант A: обычный Docker, без Compose

```sh
cd /mnt/sda1/calendar-bot
docker run -d \
  --name yandex-calendar-bot \
  --network host \
  --restart always \
  --env-file .env.docker \
  --env DATABASE_URL=sqlite+aiosqlite:////data/calendar_bot.db \
  --mount type=bind,src=/mnt/sda1/calendar-bot/data,dst=/data \
  --read-only \
  --tmpfs /tmp:rw,nosuid,noexec,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  --log-driver json-file \
  --log-opt max-size=1m \
  --log-opt max-file=3 \
  yandex-calendar-bot:latest
```

### Вариант B: Docker Compose

Если установлен Compose:

```sh
cd /mnt/sda1/calendar-bot
docker compose up -d
```

На установках с отдельным исполняемым файлом вместо плагина используется `docker-compose up -d`.

`compose.yaml` использует уже загруженный образ и не собирает проект на роутере. Относительный `./data` означает каталог рядом с `compose.yaml`. Не запускайте варианты A и B одновременно.

В обоих вариантах используется сеть Docker `host`, как у `todo_bot` (в Compose — `network_mode: host`). Контейнер разделяет сеть роутера и не получает адрес вида `172.17.0.2`; `127.0.0.1` внутри контейнера относится к роутеру. Проброс `-p 5438:5432` здесь не используется: CalDAV-бот хранит данные в SQLite, не слушает порт `5432` и получает обновления Telegram через polling. Подробности — в [документации Docker о режиме host](https://docs.docker.com/engine/network/drivers/host/).

Если существующий контейнер запущен с `bridge`, изменение файла само по себе его не переключит: контейнер нужно пересоздать, сохранив прежние переменные окружения и подключение каталога базы.

При старте применяются миграции, затем начинается polling. В `.env.docker` нет отдельного логина Яндекса: пользователь подключает свой аккаунт через чат бота или он восстанавливается из перенесённой БД.

Для одного Telegram-токена должен работать один экземпляр бота. Перед запуском на роутере остановите экземпляр на компьютере.

## 7. Проверить работу

```sh
docker ps --filter name=yandex-calendar-bot
docker logs --tail 100 yandex-calendar-bot
docker stats --no-stream yandex-calendar-bot
```

Отправьте `/start`, подключите Яндекс, включите нужные календари и проверьте `/sync`. Новые календари по умолчанию выключены; первое обновление после включения проходит без рассылки старых событий.

Контейнеру нужны DNS и исходящий HTTPS к Telegram и `caldav.yandex.ru`. Проброс входящих портов, `--privileged` и доступ к Docker socket ему не нужны. Если контейнер не выходит в интернет, проверьте подключение роутера и правила v2rayA для этих адресов.

`restart: always` перезапускает завершившийся контейнер и запускает его после старта Docker. На роутере также должен автоматически запускаться `dockerd`, а накопитель с его данными должен монтироваться до запуска сервиса.

## 8. Перенести существующую SQLite-базу

1. Остановите бот на компьютере и убедитесь, что он больше не получает обновления.
2. Сделайте согласованную копию SQLite и сохраните прежний ключ шифрования.
3. Передайте копию в `data/calendar_bot.db` на роутере до первого запуска контейнера.
4. Установите владельца `10001:10001` у каталога и файлов базы.
5. Запишите прежний `ENCRYPTION_KEY` в `.env.docker`.
6. Запустите контейнер — миграции применятся автоматически.

Пример создания копии через SQLite backup API в PowerShell из корня проекта:

```powershell
New-Item -ItemType Directory -Path dist -Force | Out-Null
.\.venv\Scripts\python.exe -c "import sqlite3; s = sqlite3.connect('file:calendar_bot.db?mode=ro', uri=True); d = sqlite3.connect('dist/calendar_bot.db'); s.backup(d); d.close(); s.close()"
scp .\dist\calendar_bot.db root@192.168.1.1:/mnt/sda1/calendar-bot/data/calendar_bot.db
```

На роутере:

```sh
chown 10001:10001 /mnt/sda1/calendar-bot/data/calendar_bot.db
chmod 600 /mnt/sda1/calendar-bot/data/calendar_bot.db
```

Не копируйте только основной файл активно используемой WAL-базы обычной файловой командой. Backup API учитывает SQLite-журнал, а остановка исходного бота исключает появление новых изменений после копирования.

Если контейнер уже использовал другую базу, остановите его и отдельно сохраните прежний каталог данных перед заменой; не подменяйте файл под работающим процессом и не оставляйте журналы от другой базы рядом с новой копией.

## 9. Обновить образ

Соберите новый архив для той же архитектуры и передайте его на роутер. Сохраните копию каталога базы и ключа перед обновлением схемы.

Для Compose:

```sh
cd /mnt/sda1/calendar-bot
docker compose stop
docker load -i yandex-calendar-bot-arm64.tar
docker compose up -d --force-recreate
```

Для обычного Docker:

```sh
docker stop -t 30 yandex-calendar-bot
docker rm yandex-calendar-bot
docker load -i /mnt/sda1/calendar-bot/yandex-calendar-bot-arm64.tar
```

Затем повторите команду `docker run` из раздела 6 с тем же каталогом `data`. Удаление самого контейнера не удаляет каталог базы, смонтированный с роутера. Не удаляйте этот каталог при обновлении.

Миграции могут менять схему базы: возврат только старого образа не всегда равнозначен откату приложения с данными. Для восстановления используйте согласованную резервную копию соответствующей версии.

## 10. Что добавлено в проект

| Файл | Назначение |
|---|---|
| `Dockerfile` | Двухэтапная сборка, Python 3.12 slim, runtime без компиляторов, UID 10001, база в `/data` |
| `.dockerignore` | Разрешённый список файлов сборки; исключает секреты и пользовательские данные |
| `.env.docker.example` | Шаблон параметров контейнера |
| `compose.yaml` | Один сервис, постоянный каталог SQLite, перезапуск и ограниченная ротация логов |
| `docker/build-image.ps1` | Сборка выбранной платформы, проверка контейнера, экспорт `.tar` и контрольной суммы |
| `docker/smoke_test.py` | Проверка Linux-зависимостей, миграций, шифрования и сохранения БД без внешних запросов |

Базовый образ — официальный [Python slim](https://hub.docker.com/_/python). Его Debian-пользовательское окружение работает внутри контейнера; устанавливать Debian на OpenWrt не требуется.
