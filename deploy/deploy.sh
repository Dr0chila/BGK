#!/bin/bash
# Выкатка новой версии: файлы уже залиты в /root/bgk-bot, скрипт проверяет,
# делает снимок, перезапускает и сам откатывается, если бот не поднялся.
#   bash deploy/deploy.sh
set -euo pipefail
cd /root/bgk-bot

SERVICE=bgk-bot
PY=.venv/bin/python
SNAP_DIR=backups/code
KEEP=5
PARTS="app webapp alembic alembic.ini requirements.txt"

echo "🔹 Комплектность..."
MISS=""
for p in $PARTS .env webapp/index.html app/main.py; do [ -e "$p" ] || MISS="$MISS $p"; done
if [ -n "$MISS" ]; then
    echo "❌ Не хватает:$MISS — бот не тронут."; exit 1
fi

[ -x "$PY" ] || python3 -m venv .venv
echo "🔹 Зависимости..."
.venv/bin/pip install -q -r requirements.txt

echo "🔹 Синтаксис и импорт..."
$PY -m compileall -q app alembic >/dev/null
# Импорт исполняет модуль целиком: ловит NameError, битый .env, циклические импорты.
# Сервер при этом не стартует — только lifespan uvicorn'а ходит в Telegram.
$PY -c "import app.main" || { echo "❌ Импорт упал — бот НЕ перезапущен, работает старая версия."; exit 1; }

echo "🔹 Снимок → $SNAP_DIR ..."
STAMP=$(date +%F_%H%M%S)
mkdir -p "$SNAP_DIR"
# Снимок ПРЕДЫДУЩЕЙ рабочей версии берём из последнего успешного деплоя (current.tgz),
# а не из папки: там уже лежат новые файлы.
if [ -f "$SNAP_DIR/current.tgz" ]; then
    cp "$SNAP_DIR/current.tgz" "$SNAP_DIR/$STAMP.tgz"
fi
ls -1 "$SNAP_DIR"/20*.tgz 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "🔹 Бэкап базы перед миграцией..."
bash deploy/backup.sh || echo "⚠️ Бэкап базы не сделан — проверь pg_dump"

echo "🔹 Миграции..."
.venv/bin/alembic upgrade head

echo "🔹 Перезапуск..."
SINCE=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart $SERVICE

echo "🔹 Жду /health..."
OK=0
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then OK=1; break; fi
    sleep 1
done

if [ $OK -ne 1 ]; then
    echo "❌ Бот не поднялся за 30 сек."
    journalctl -u $SERVICE --since "$SINCE" --no-pager | tail -n 40
    if [ -f "$SNAP_DIR/$STAMP.tgz" ]; then
        echo "↩️  Откатываю код на предыдущую версию..."
        tar xzf "$SNAP_DIR/$STAMP.tgz"
        systemctl restart $SERVICE
        sleep 5
        echo "   после отката: $(curl -fsS http://127.0.0.1:8000/health 2>/dev/null || echo 'не отвечает')"
        echo "   Миграции базы НЕ откатываются автоматически: если новая их добавила —"
        echo "   .venv/bin/alembic downgrade -1  (или восстановить дамп из backups/db)"
    else
        echo "   Предыдущей версии нет (первый деплой) — откатывать некуда."
    fi
    exit 1
fi

# Успех — запоминаем эту версию как рабочую для следующего отката
tar czf "$SNAP_DIR/current.tgz" $PARTS
echo "✅ Работает: $(curl -fsS http://127.0.0.1:8000/health)"
journalctl -u $SERVICE --since "$SINCE" --no-pager | tail -n 15
if journalctl -u $SERVICE --since "$SINCE" --no-pager | grep -qE "Traceback|ERROR"; then
    echo; echo "⚠️ В логах старта есть ошибки:"
    journalctl -u $SERVICE --since "$SINCE" --no-pager | grep -E "Traceback|ERROR" | tail -n 20
fi
