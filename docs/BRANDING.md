# Календарь рядом

Образ бота — спокойный помощник, который держит встречи под рукой.
Календарь обозначает расписание, галочка — порядок, колокольчик — напоминания.
Контрастные крупные формы рассчитаны на небольшой круглый аватар Telegram.
На картинке нет текста, дат и логотипов сторонних сервисов.

## Файлы

- `bot/assets/avatar.png` — готовый исходник, 1254 × 1254.
- `bot/assets/avatar.jpg` — версия для профиля Telegram, те же размеры, JPEG quality 95.
- `bot/profile.py` — тексты профиля и команды.
- `bot/setup_profile.py` — отдельное применение оформления через Bot API.

Аватарка создана встроенным инструментом ImageGen. После генерации отдельным
редактированием добавлен непрозрачный тёмный фон. Финальный PNG сохранён без изменений;
JPEG получен конвертацией формата без изменения размеров или композиции.

## Промпт генерации

```text
Use case: logo-brand
Asset type: square Telegram bot avatar, 1024x1024, for a friendly personal calendar and meeting reminder assistant.
Primary request: create a polished, original, instantly recognizable app mascot icon: a small ivory desk-calendar tile with a bold cobalt-blue top bar and two simple binding rings, a single bold blue check mark on its face, and a small golden-yellow notification bell nestled against the lower-right corner of the calendar. The calendar and bell form one compact silhouette.
Scene/backdrop: solid deep midnight navy background extending to all four edges.
Style/medium: premium minimal softly dimensional illustration, crisp silhouettes, smooth matte surfaces, very restrained soft shadow, clean and warm, no photorealism.
Composition/framing: square canvas, centered icon occupying about 65 percent of canvas, all important parts safely within the central circle for Telegram circular cropping, generous clear margins, extremely legible at 48 pixels.
Color palette: midnight navy backdrop, warm ivory calendar, rich cobalt-blue binding and check mark, golden yellow bell.
Constraints: exactly one calendar and one small bell. No lettering, no words, no numerals, no tiny calendar grid, no watermark, no extra symbols, no border, no phone mockup. Balanced unified visual identity, uncluttered.
```

## Финальная правка

```text
Use case: precise-object-edit. Edit the last generated calendar-and-bell avatar. Change only the background: replace ALL transparency with an opaque, solid midnight navy (#101C36) background filling the entire square, including all corners. The final image must be completely opaque, with no transparent pixels. Preserve exactly the existing ivory and cobalt calendar, check mark, golden bell, proportions, placement, materials and soft shadow. Keep the square format and generous safe margins for a circular crop. No new objects, letters, text, numbers or watermark.
```

## Применение

Из корня проекта выполните `python -m bot.setup_profile --dry-run` для просмотра,
затем `python -m bot.setup_profile` для установки в Telegram.
Для команды нужен `BOT_TOKEN` в окружении или в локальном `.env`.

Профиль обновляется отдельной командой: обычный перезапуск не загружает фото заново.
Полное описание ограничено 512 символами, короткое — 120.
Фото отправляется как новый JPEG через
[setMyProfilePhoto](https://core.telegram.org/bots/api#setmyprofilephoto);
поля профиля — через
[setMyName](https://core.telegram.org/bots/api#setmyname),
[setMyDescription](https://core.telegram.org/bots/api#setmydescription) и
[setMyShortDescription](https://core.telegram.org/bots/api#setmyshortdescription).
