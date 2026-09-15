import os
import secrets
import string
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from supabase import create_client

# ============================================================
# НАСТРОЙКА SUPABASE
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def gen_referral_code():
    """Генерирует реферальный код вида VISAL-A1B2C3."""
    alphabet = string.ascii_uppercase + string.digits
    part = ''.join(secrets.choice(alphabet) for _ in range(6))
    return f"VISAL-{part}"


def add_bonus_hours(user_id, hours, reason=""):
    """Начисляет бонусные часы пользователю и продлевает его активный ключ."""
    if hours <= 0 or not user_id:
        return

    # Обновляем бонусные часы в профиле
    try:
        user = supabase.table("users").select("bonus_hours") \
            .eq("id", user_id).execute().data
        if user:
            current = user[0].get("bonus_hours", 0) or 0
            supabase.table("users").update({
                "bonus_hours": current + hours
            }).eq("id", user_id).execute()
    except Exception as e:
        print("Ошибка обновления bonus_hours:", e)

    # Продлеваем все активные ключи пользователя
    try:
        keys = supabase.table("keys").select("*").eq("user_id", user_id).execute().data
        for k in keys:
            expiry = k.get("expiry")
            if expiry and expiry != "forever":
                try:
                    new_expiry = datetime.fromisoformat(expiry) + timedelta(hours=hours)
                    supabase.table("keys").update({
                        "expiry": new_expiry.isoformat()
                    }).eq("key", k["key"]).execute()
                except Exception as e:
                    print("Ошибка продления ключа:", e)
    except Exception as e:
        print("Ошибка выборки ключей:", e)

    # Записываем в лог
    try:
        supabase.table("bonus_log").insert({
            "user_code": str(user_id),
            "days": hours,          # оставляем старое имя, чтобы не ломать таблицу
            "reason": reason,
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print("Ошибка записи в bonus_log:", e)


def check_and_unlock_achievements(user_id):
    """Проверяет условия достижений и разблокирует их, выдавая награды."""
    if not user_id:
        return

    # Получаем данные пользователя
    user_data = supabase.table("users").select("*") \
        .eq("id", user_id).execute().data
    if not user_data:
        return
    user = user_data[0]

    # Уже разблокированные
    unlocked = supabase.table("user_achievements") \
        .select("achievement_code") \
        .eq("user_id", user_id).execute().data
    unlocked_codes = {u["achievement_code"] for u in unlocked}

    # Список достижений
    achievements = supabase.table("achievements").select("*").execute().data

    # Считаем статистику
    keys = supabase.table("keys").select("*").eq("user_id", user_id).execute().data
    referrals = supabase.table("referrals") \
        .select("*").eq("referrer_code", user["referral_code"]).execute().data

    hours_in_app = user.get("hours_in_app", 0) or 0
    total_clicks = user.get("total_clicks", 0) or 0
    invited = len(referrals)

    # Вычисляем, сколько дней пользователь с нами
    try:
        created = datetime.fromisoformat(user.get("created_at"))
        days_with_us = (datetime.now() - created).days
    except:
        days_with_us = 0

    # Телепорты приблизительно считаем по кликам
    teleports = total_clicks

    for a in achievements:
        code = a["code"]
        if code in unlocked_codes:
            continue

        unlocked_now = False
        if code == "first_launch":
            unlocked_now = True
        elif code == "hour_in_app":
            unlocked_now = hours_in_app >= 1
        elif code == "100_teleports":
            unlocked_now = teleports >= 100
        elif code == "invite_5":
            unlocked_now = invited >= 5
        elif code == "month_with_us":
            unlocked_now = days_with_us >= 30

        if unlocked_now:
            supabase.table("user_achievements").insert({
                "user_id": user_id,
                "achievement_code": code,
                "unlocked_at": datetime.now().isoformat()
            }).execute()
            add_bonus_hours(user_id, a.get("reward_hours", 0),
                            reason=f"achievement:{code}")


# ============================================================
# РОУТЫ
# ============================================================

@app.route('/ping')
def ping():
    return "pong", 200


@app.route('/validate', methods=['POST'])
def validate():
    """Проверяет ключ без активации."""
    data = request.json or {}
    key = data.get('key', '').strip()
    if not key:
        return jsonify({"valid": False, "message": "Ключ не указан"}), 400

    result = supabase.table("keys").select("*").eq("key", key).execute()
    if not result.data:
        return jsonify({"valid": False, "message": "Неверный ключ"}), 200

    row = result.data[0]
    if row.get("banned"):
        return jsonify({"valid": False, "message": "Ключ заблокирован"}), 200

    expiry = row.get("expiry")
    if expiry == "forever":
        return jsonify({"valid": True, "message": "Ключ действителен (навсегда)",
                        "expiry": "forever"}), 200

    try:
        exp = datetime.fromisoformat(expiry)
        if datetime.now() > exp:
            return jsonify({"valid": False, "message": "Ключ истёк"}), 200
        delta = exp - datetime.now()
        hours_left = delta.total_seconds() / 3600
        return jsonify({
            "valid": True,
            "message": f"Действителен ({hours_left:.1f} ч.)",
            "expiry": expiry,
            "hours_left": hours_left
        }), 200
    except:
        return jsonify({"valid": False, "message": "Ошибка формата ключа"}), 200


@app.route('/activate', methods=['POST'])
def activate():
    """Активирует ключ, обрабатывает реферальный код."""
    data = request.json or {}
    key = data.get('key', '').strip()
    ref_code = data.get('referral_code', '').strip().upper()

    if not key:
        return jsonify({"valid": False, "message": "Ключ не указан"}), 400

    # Ищем ключ
    result = supabase.table("keys").select("*").eq("key", key).execute()
    if not result.data:
        return jsonify({"valid": False, "message": "Неверный ключ"}), 200

    row = result.data[0]
    if row.get("banned"):
        return jsonify({"valid": False, "message": "Ключ заблокирован"}), 200

    # Проверка срока
    expiry = row.get("expiry")
    if expiry and expiry != "forever":
        try:
            if datetime.now() > datetime.fromisoformat(expiry):
                return jsonify({"valid": False, "message": "Ключ истёк"}), 200
        except:
            pass

    user_id = row.get("user_id")

    # Если ключ новый и указан реферальный код — активируем
    if not user_id:
        # Создаём пользователя
        my_code = gen_referral_code()
        new_user = supabase.table("users").insert({
            "referral_code": my_code,
            "referred_by": ref_code if ref_code else None,
            "bonus_hours": 0,
            "created_at": datetime.now().isoformat()
        }).execute()
        new_user_id = new_user.data[0]["id"]

        # Привязываем ключ к пользователю
        supabase.table("keys").update({
            "user_id": new_user_id,
            "activated": datetime.now().isoformat()
        }).eq("key", key).execute()

        # Обработка реферала
        if ref_code:
            ref_user = supabase.table("users").select("*") \
                .eq("referral_code", ref_code).execute().data
            if ref_user:
                referrer_id = ref_user[0]["id"]

                # Рефереру +24 часа, приглашённому +1 час
                add_bonus_hours(referrer_id, 24, reason="referral_referrer")
                add_bonus_hours(new_user_id, 1, reason="referral_invited")

                # Записываем реферал
                supabase.table("referrals").insert({
                    "referrer_code": ref_code,
                    "referred_user_id": new_user_id,
                    "reward_days": 24,
                    "created_at": datetime.now().isoformat()
                }).execute()

                # Ежедневное задание "пригласить друга" — обновляем
                today = datetime.now().strftime("%Y-%m-%d")
                existing = supabase.table("user_daily_progress") \
                    .select("*").eq("user_id", referrer_id) \
                    .eq("task_code", "daily_referral") \
                    .eq("date", today).execute().data
                if existing:
                    supabase.table("user_daily_progress").update({
                        "progress": (existing[0].get("progress", 0) or 0) + 1,
                        "completed_at": datetime.now().isoformat()
                    }).eq("user_id", referrer_id) \
                        .eq("task_code", "daily_referral") \
                        .eq("date", today).execute()
                else:
                    supabase.table("user_daily_progress").insert({
                        "user_id": referrer_id,
                        "task_code": "daily_referral",
                        "progress": 1,
                        "completed_at": datetime.now().isoformat(),
                        "date": today
                    }).execute()
                    # Первое приглашение за день — +1 час
                    add_bonus_hours(referrer_id, 1, reason="daily_referral")

        # Проверяем достижения
        check_and_unlock_achievements(new_user_id)

        return jsonify({
            "valid": True,
            "message": "Ключ активирован",
            "user_id": new_user_id,
            "my_referral_code": my_code
        }), 200

    # Ключ уже привязан к пользователю
    check_and_unlock_achievements(user_id)
    return jsonify({
        "valid": True,
        "message": "Ключ уже активирован",
        "user_id": user_id
    }), 200


@app.route('/me', methods=['POST'])
def me():
    """Информация о пользователе: рефералы, бонусы, достижения."""
    data = request.json or {}
    key = data.get('key', '').strip()

    result = supabase.table("keys").select("user_id").eq("key", key).execute()
    if not result.data or not result.data[0].get("user_id"):
        return jsonify({"exists": False}), 200

    user_id = result.data[0]["user_id"]
    user_data = supabase.table("users").select("*").eq("id", user_id).execute().data
    if not user_data:
        return jsonify({"exists": False}), 200
    user = user_data[0]

    # Рефералы
    referrals = supabase.table("referrals") \
        .select("*").eq("referrer_code", user["referral_code"]).execute().data

    # Достижения
    all_ach = supabase.table("achievements").select("*").execute().data
    unlocked = supabase.table("user_achievements") \
        .select("achievement_code, unlocked_at") \
        .eq("user_id", user_id).execute().data
    unlocked_map = {u["achievement_code"]: u["unlocked_at"] for u in unlocked}
    achievements = []
    for a in all_ach:
        achievements.append({
            "code": a["code"],
            "title": a["title"],
            "description": a["description"],
            "icon": a["icon"],
            "reward_hours": a["reward_hours"],
            "unlocked": a["code"] in unlocked_map,
            "unlocked_at": unlocked_map.get(a["code"])
        })

    # Ежедневные задания на сегодня
    today = datetime.now().strftime("%Y-%m-%d")
    daily_tasks = supabase.table("daily_tasks").select("*").execute().data
    progress = supabase.table("user_daily_progress") \
        .select("*").eq("user_id", user_id).eq("date", today).execute().data
    progress_map = {p["task_code"]: p for p in progress}

    tasks = []
    for t in daily_tasks:
        p = progress_map.get(t["code"], {})
        tasks.append({
            "code": t["code"],
            "title": t["title"],
            "description": t["description"],
            "reward_hours": t["reward_hours"],
            "progress": p.get("progress", 0),
            "completed": bool(p.get("completed_at"))
        })

    return jsonify({
        "exists": True,
        "user_id": user_id,
        "referral_code": user["referral_code"],
        "invited": len(referrals),
        "bonus_hours": user.get("bonus_hours", 0) or 0,
        "hours_in_app": user.get("hours_in_app", 0) or 0,
        "total_clicks": user.get("total_clicks", 0) or 0,
        "created_at": user.get("created_at"),
        "nickname": user.get("nickname"),
        "avatar": user.get("avatar"),
        "achievements": achievements,
        "daily_tasks": tasks
    }), 200


@app.route('/daily/login', methods=['POST'])
def daily_login():
    """Ежедневное задание «Войти сегодня»."""
    data = request.json or {}
    key = data.get('key', '').strip()
    result = supabase.table("keys").select("user_id").eq("key", key).execute()
    if not result.data or not result.data[0].get("user_id"):
        return jsonify({"success": False, "message": "Ключ не найден"}), 200

    user_id = result.data[0]["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")

    existing = supabase.table("user_daily_progress") \
        .select("*").eq("user_id", user_id) \
        .eq("task_code", "daily_login").eq("date", today).execute().data

    if existing:
        return jsonify({"success": False, "message": "Уже получено сегодня"}), 200

    supabase.table("user_daily_progress").insert({
        "user_id": user_id,
        "task_code": "daily_login",
        "progress": 1,
        "completed_at": datetime.now().isoformat(),
        "date": today
    }).execute()

    # Награда: 0 часов по заданию пользователя, но можно поставить своё
    add_bonus_hours(user_id, 0, reason="daily_login")
    return jsonify({"success": True, "message": "Задание выполнено"}), 200


@app.route('/daily/use', methods=['POST'])
def daily_use():
    """Ежедневное задание «Запустить бота»."""
    data = request.json or {}
    key = data.get('key', '').strip()
    result = supabase.table("keys").select("user_id").eq("key", key).execute()
    if not result.data or not result.data[0].get("user_id"):
        return jsonify({"success": False, "message": "Ключ не найден"}), 200

    user_id = result.data[0]["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")

    existing = supabase.table("user_daily_progress") \
        .select("*").eq("user_id", user_id) \
        .eq("task_code", "daily_use").eq("date", today).execute().data

    if existing:
        return jsonify({"success": False, "message": "Уже получено сегодня"}), 200

    supabase.table("user_daily_progress").insert({
        "user_id": user_id,
        "task_code": "daily_use",
        "progress": 1,
        "completed_at": datetime.now().isoformat(),
        "date": today
    }).execute()

    add_bonus_hours(user_id, 0, reason="daily_use")
    return jsonify({"success": True, "message": "Задание выполнено"}), 200


@app.route('/stats/update', methods=['POST'])
def stats_update():
    """Обновляет статистику пользователя (часы, клики)."""
    data = request.json or {}
    key = data.get('key', '').strip()
    hours = data.get('hours', 0)
    clicks = data.get('clicks', 0)

    result = supabase.table("keys").select("user_id").eq("key", key).execute()
    if not result.data or not result.data[0].get("user_id"):
        return jsonify({"success": False}), 200

    user_id = result.data[0]["user_id"]
    user = supabase.table("users").select("*").eq("id", user_id).execute().data
    if not user:
        return jsonify({"success": False}), 200

    new_hours = (user[0].get("hours_in_app", 0) or 0) + hours
    new_clicks = (user[0].get("total_clicks", 0) or 0) + clicks

    supabase.table("users").update({
        "hours_in_app": new_hours,
        "total_clicks": new_clicks
    }).eq("id", user_id).execute()

    # Проверяем достижения после обновления
    check_and_unlock_achievements(user_id)

    return jsonify({"success": True}), 200


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
