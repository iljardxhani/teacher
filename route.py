from flask import Flask, request, jsonify
from flask_cors import CORS
import time  # needed for sleep

app = Flask(__name__)
CORS(app)

# ================== REGISTRIES ==================
tab_registry = {
    "ai": [],
    "teacher": [],
    "class": [],
    "stt": []
}

status_registry = {
    "ai": "nil",
    "teacher": "nil",
    "class": "nil",
    "stt": "nil"
}

# ================== POST MESSAGE ==================
@app.route("/send_message", methods=["POST"])
def send_message():
    data = request.json
    sender = data.get("from")
    recipient = data.get("to")
    message = data.get("message")

    if not sender or not recipient or not message:
        return jsonify({"error": "Missing 'from', 'to' or 'message'"}), 400

    if recipient not in tab_registry:
        return jsonify({"error": f"Recipient '{recipient}' unknown"}), 400

    print(f"[route] {sender=} -> {recipient=} | message={message}")
    tab_registry[recipient].append({
        "from": sender,
        "message": message
    })

    return jsonify({"status": "ok"}), 200

# ================== GET MESSAGES ==================
@app.route("/get_messages/<recipient>", methods=["GET"])
def get_messages(recipient):
    if recipient not in tab_registry:
        return jsonify({"messages": [], "status": "unknown"}), 400
    messages = tab_registry[recipient].copy()
    tab_registry[recipient].clear()
    print(f"[route] get_messages for {recipient}: {len(messages)} messages")

    return jsonify({"messages": messages}), 200

# ================== RUN ==================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
