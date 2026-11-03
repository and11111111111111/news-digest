# news-digest — сбор сырых данных для дайджеста

Каждое утро в ~05:07 по Москве GitHub Actions запускает `collect.py`. Модели здесь нет, токены не тратятся. Скрипт:

- забирает RSS-ленты и Telegram-каналы из `sources.yaml` (материалы за последние 26 часов);
- берёт курсы ЦБ, ключевую ставку, индекс Мосбиржи, Brent, биткоин;
- берёт погоду в Одинцово на 3 дня;
- сохраняет всё в `data/ГГГГ-ММ-ДД.json` и `data/latest.json` (старше 14 дней удаляются).

Дальше файл обрабатывает модель: Ollama на ноутбуке или облачная задача Claude.

## Формат data/latest.json

```
generated_at   время сбора (МСК)
counts         сколько материалов и сколько источников ответило / не ответило
markets        cbr_rates, key_rate, imoex, brent, bitcoin (null, если источник не ответил)
weather        place, days[]: date, t_max, t_min, precip_mm, precip_prob, wind_ms, conditions
items[]        source, section_hint, title, link, published, summary (до 600 символов), telegram
status[]       по каждому источнику: ok / error — по нему видно, какие ленты сломались
```

## Как поменять источники

Отредактируйте `sources.yaml` прямо на сайте GitHub (значок карандаша). Изменения подхватятся при следующем запуске.

## Ручной запуск

Вкладка **Actions** → «Сбор новостей» → **Run workflow**.
