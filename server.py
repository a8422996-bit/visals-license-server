from flask import Flask, request, jsonify
from supabase import create_client
from datetime import datetime, timedelta
import os

app = Flask(__name__)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/ping')
def ping():
    return "pong", 200

@app.route('/validate', methods=['POST'])
def validate():
    data = request.json
    key = data.get('key', '').strip()
    if not key:
        return jsonify({"valid": False, "message": "Ключ не указан"}), 400

    # Ищем ключ в базе
    result = supabase.table("keys").select("*").eq("key", key).execute()
    if not result.data:
        return jsonify({"valid": False, "message": "Неверный ключ"}), 200

    row = result.data[0]
    if row.get("banned"):
        return jsonify({"valid": False, "message": "Ключ заблокирован"}), 200

    expiry = row.get("expiry")
    if expiry == "forever":
        return jsonify({"valid": True, "message": "Навсегда", "expiry": "forever"}), 200

    try:
        exp = datetime.fromisoformat(expiry)
        if datetime.now() > exp:
            return jsonify({"valid": False, "message": "Ключ истёк"}), 200
        left = (exp - datetime.now()).total_seconds() / 3600 / 24
        return jsonify({
            "valid": True,
            "message": f"Действителен ({left:.1f} дн.)",
            "expiry": expiry,
            "days_left": left
        }), 200
    except:
        return jsonify({"valid": False, "message": "Ошибка формата"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)