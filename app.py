from flask import Flask, request, jsonify
import requests
import json
import re
from groq import Groq

app = Flask(__name__)

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
import os

PHONE_NUMBER     = "9759145356"
ALLOWED_NUMBERS  = ["9354906215", "9759145356", "7988149282"]

PRODUCT_API_URL  = "https://stguae.delidel.in/module/ogaverloopapi/ogachatbotapi"
API_KEY          = os.environ.get("API_KEY")

GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
groq_client      = Groq(api_key=GROQ_API_KEY)

WHATSAPP_TOKEN   = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID  = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN     = os.environ.get("VERIFY_TOKEN")

# ─────────────────────────────────────────
# OGA CRM CONFIG  (from n8n workflow)
# ─────────────────────────────────────────
OGA_CRM_BASE_URL      = "https://crm.ogaapps.in"
OGA_CRM_BEARER_TOKEN  = os.environ.get("OGA_CRM_BEARER_TOKEN")
OGA_CRM_INSTANCE_NAME = "Delidel Support"   # instanceName used in n8n Send node

# ─────────────────────────────────────────
# SESSION HISTORY
# ─────────────────────────────────────────
session_history: dict = {}

def get_session(number: str) -> list:
    return session_history.setdefault(number, [])

def save_to_session(number: str, user_input: str, product_name: str, reply: str):
    history = get_session(number)
    history.append({"user": user_input, "product_name": product_name, "reply": reply})
    if len(history) > 5:
        history.pop(0)

def get_last_product(number: str) -> str:
    for turn in reversed(get_session(number)):
        if turn.get("product_name"):
            return turn["product_name"]
    return ""

# ─────────────────────────────────────────
# NUMBER UTILS
# ─────────────────────────────────────────
def clean_number(raw: str) -> str:
    return re.sub(r"^\+?(91|971)", "", str(raw))

def is_allowed_number(raw_number: str) -> bool:
    cleaned = clean_number(raw_number)
    print(f"[AUTH CHECK] raw={raw_number} → cleaned={cleaned}, allowed={ALLOWED_NUMBERS}")
    return cleaned in ALLOWED_NUMBERS

# ─────────────────────────────────────────
# CRM FUNCTIONS  (mirrors n8n nodes)
# ─────────────────────────────────────────

def forward_raw_to_crm(raw_body: dict):
    """
    Mirrors the n8n 'HTTP Request' node:
    POST https://crm.ogaapps.in/api/webhook/evolution
    Body: raw_body["body"] — just the inner Evolution event payload,
    matching n8n's JSON.stringify($json.body) behaviour.
    """
    url = f"{OGA_CRM_BASE_URL}/api/webhook/evolution"
    
    # n8n sends $json.body — the nested "body" key inside the webhook payload
    # Your Flask /webhook receives the full n8n structure, so extract .get("body")
    # If called from the direct WhatsApp webhook, fall back to raw_body itself
    payload_to_forward = raw_body.get("body", raw_body)
    
    try:
        res = requests.post(
            url,
            json=payload_to_forward,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        print(res)
        print(f"[CRM PASSTHROUGH] {res.status_code} → {res.text[:200]}")
    except Exception as e:
        print(f"[CRM PASSTHROUGH ERROR] {e}")

def send_reply_via_crm(sender_number: str, message: str):
    """
    Mirrors the n8n 'Send Reply via OGA CRM' node:
    POST https://crm.ogaapps.in/api/v1/send
    Auth: Bearer token
    Body: { instanceName, number, type, message }
    """
    url = f"{OGA_CRM_BASE_URL}/api/v1/send"
    # Strip leading + just like n8n: String($json.sender_number).replace(/^\+/, '')
    clean_to = str(sender_number).lstrip("+").strip()
    payload = {
        "instanceName": OGA_CRM_INSTANCE_NAME,
        "number":       clean_to,
        "type":         "text",
        "message":      str(message).strip()
    }
    headers = {
        "Authorization": f"Bearer {OGA_CRM_BEARER_TOKEN}",
        "Content-Type":  "application/json"
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[CRM SEND REPLY] {res.status_code} → {res.text[:200]}")
    except Exception as e:
        print(f"[CRM SEND REPLY ERROR] {e}")

# ─────────────────────────────────────────
# INTENT CLASSIFIER PROMPT
# ─────────────────────────────────────────
CLASSIFIER_PROMPT = """
You are a smart message classifier for a WhatsApp sales assistant.
Return ONLY a valid JSON object — no extra text, no markdown fences, no explanation.
---
INTENT DETECTION RULES:
1. PRICE CHECK
   Trigger: price, cost, rate, how much, what's the price, what's the rate
   → lookup_price = true, needs_product_lookup = true
2. STOCK CHECK
   Trigger: available, in stock, do you have it, can I get it, stock, is it available
   → lookup_stock = true, needs_product_lookup = true
3. PRICE + STOCK BOTH
   → lookup_price = true, lookup_stock = true, needs_product_lookup = true
4. DIRECT ORDER
   Trigger: "send it", "give me", "add it", "order", "I want/need" WITH a number quantity
   OR product name + number + unit (ctns/box/pcs/kg/cartons/pieces)
   Examples: "fries 22 ctns", "send me 10 box lays"
   - If quantity present → direct_order = true, needs_product_lookup = false, general_reply = ""
   - If quantity missing → direct_order = false, general_reply = ask for quantity in user's language
5. PRODUCT NAME ONLY (no price/stock/order/quantity)
   → needs_product_lookup = false, all flags = false
   → general_reply = ask if they want price, stock, or order (match user language)
   Example: "Fries! 😊 Would you like to check the price, check availability, or place an order?"
6. CONTEXT CARRY-OVER
   If user says only "price", "available", "yes", "I want to order" with no product name
   → use last_discussed_product as product_name, apply matching rule
7. GENERAL CHAT → all flags false, short friendly reply
8. CONTEXT CARRY-OVER (brand variant)
   If user mentions a brand/variant (e.g. "Sadia", "Lay's", "PG") without a new product name
   → combine last_discussed_product + brand as the search query
   Example: last_discussed_product = "Fries", user says "Sadia price"
   → product_name = "Fries Sadia", lookup_price = true
---
LANGUAGE RULE (CRITICAL):
- English input → reply in English
- Arabic input → reply in Arabic
---
STRICT RULE:
If lookup_price=true OR lookup_stock=true → needs_product_lookup MUST be true.
Never output lookup_price/lookup_stock=true with needs_product_lookup=false.
---
Return ONLY this JSON, no other text:
{
  "needs_product_lookup": false,
  "lookup_price": false,
  "lookup_stock": false,
  "direct_order": false,
  "product_name": "",
  "quantity": "",
  "general_reply": ""
}
"""

# ─────────────────────────────────────────
# SAFE FALLBACK INTENT
# ─────────────────────────────────────────
SAFE_INTENT = {
    "needs_product_lookup": False,
    "lookup_price": False,
    "lookup_stock": False,
    "direct_order": False,
    "product_name": "",
    "quantity": "",
    "general_reply": "Sorry, I didn't quite understand that. Could you please repeat? 😊"
}

# ─────────────────────────────────────────
# SAFE FALLBACK INTENT
# ─────────────────────────────────────────
SAFE_INTENT = {
    "needs_product_lookup": False,
    "lookup_price": False,
    "lookup_stock": False,
    "direct_order": False,
    "product_name": "",
    "quantity": "",
    "general_reply": "Samajh nahi aaya, zara dobara batao? 😊"
}

# ─────────────────────────────────────────
# JSON HELPERS
# ─────────────────────────────────────────
def extract_json(text: str) -> str:
    text = re.sub(r"`{3}[a-z]*", "", text).strip()
    match = re.search(r'\{[\s\S]*\}', text)
    return match.group(0) if match else ""

def fix_and_parse(raw: str) -> dict:
    idx = raw.rfind("}")
    if idx != -1:
        raw = raw[:idx+1]
    raw = re.sub(r'"lookups?\s+stock"', '"lookup_stock"', raw)
    raw = re.sub(r'"needs_products?_lookup"', '"needs_product_lookup"', raw)
    return json.loads(raw)

# ─────────────────────────────────────────
# CLASSIFY MESSAGE VIA GROQ
# ─────────────────────────────────────────
def classify_message(user_input: str, last_product: str) -> dict:
    history_context = f'\nLast discussed product: "{last_product}"'
    full_prompt = CLASSIFIER_PROMPT + history_context

    print(f"\n── SESSION: last_product='{last_product}' ──")

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": full_prompt},
                {"role": "user",   "content": f'Customer message: "{user_input}"'}
            ],
            temperature=0,
            max_tokens=300
        )
        raw = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GROQ ERROR] {e}")
        return SAFE_INTENT.copy()

    print("── RAW MODEL OUTPUT ──")
    print(raw)

    raw_json = extract_json(raw)
    if not raw_json:
        print("[WARN] No JSON found")
        return SAFE_INTENT.copy()

    try:
        result = fix_and_parse(raw_json)
    except json.JSONDecodeError as e:
        print(f"[WARN] JSON parse failed: {e}")
        return SAFE_INTENT.copy()

    for key, default in SAFE_INTENT.items():
        result.setdefault(key, default)

    if result.get("lookup_price") or result.get("lookup_stock"):
        result["needs_product_lookup"] = True

    result["general_reply"] = re.sub(r'\s*\([^)]*\)\s*$', "", result["general_reply"]).strip()

    return result

# ─────────────────────────────────────────
# PRODUCT API
# ─────────────────────────────────────────
def get_product_data(product_name: str, sender_number: str) -> dict:
    params = {
        "action":       "getproductdetails",
        "product_name": product_name,
        "page":         1,
        "per_page":     20,
        "phoneNumber":  clean_number(sender_number)
    }
    try:
        res = requests.get(PRODUCT_API_URL, params=params,
                           headers={"x-api-key": API_KEY}, timeout=10)
        return res.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

# ─────────────────────────────────────────
# FORMAT PRODUCT REPLY
# ─────────────────────────────────────────
def format_product_reply(api_data: dict, intent: dict) -> str:
    products      = api_data.get("products", [])
    customer_name = api_data.get("customer_name", "")
    first_name    = customer_name.split()[0] if customer_name else ""
    greeting      = f"Hi {first_name}! " if first_name else ""

    if not products:
        pname = intent.get("product_name", "that product")
        return (f"{greeting}\"{pname}\" nahi mila. "
                "Spelling check karein ya exact product name share karein?")

    lines = [f"Hey {customer_name or 'there'}, yeh raha:\n"]
    for i, p in enumerate(products, start=1):
        name  = p.get("name", "Unknown")
        price = float(p.get("price", 0) or 0)
        stock = int(p.get("stock", 0) or 0)
        entry = f"*{i}. {name}*"
        if intent.get("lookup_price"):
            entry += f"\n💰 AED {price:.2f}" if price > 0 else "\n💰 Price: Contact us"
        if intent.get("lookup_stock"):
            if stock > 10:
                entry += "\n✅ In stock"
            elif stock > 0:
                entry += f"\n⚠️ Low stock ({stock} left)"
            else:
                entry += "\n❌ Out of stock"
        lines.append(entry)
    lines.append("\n👉 Kuch aur chahiye?")
    return "\n".join(lines)

# ─────────────────────────────────────────
# FORMAT ORDER REPLY
# ─────────────────────────────────────────
def format_order_reply(intent: dict, customer_name: str = "") -> str:
    first_name = customer_name.split()[0] if customer_name else ""
    greeting   = f"Hi {first_name}! " if first_name else ""
    product    = intent.get("product_name", "")
    quantity   = intent.get("quantity", "")
    order_line = f"{quantity} {product}".strip()
    order_line = f"*{order_line}*" if order_line else "your order"
    return (
        f"{greeting}Thank you! ✅\n"
        f"Order receive hua: {order_line}\n"
        "Jald confirm karenge. 😊\n\n"
        "Kuch aur chahiye?"
    )

def transform_to_whatsapp_format(data):
    raw = data.get("body", {}).get("data", {})

    # Extract sender
    sender = raw.get("key", {}).get("remoteJid", "")
    sender = sender.replace("@s.whatsapp.net", "")

    # Extract message text
    message = (
        raw.get("message", {}).get("conversation")
        or raw.get("message", {}).get("extendedTextMessage", {}).get("text")
        or ""
    )

    # Build Flask-compatible structure
    transformed = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": sender,
                                    "type": "text",
                                    "text": {
                                        "body": message
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    return transformed

# ─────────────────────────────────────────
# SEND WHATSAPP MESSAGE  (kept for fallback / direct WA API use)
# ─────────────────────────────────────────
def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type":  "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": message}
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"[WA SEND] {res.status_code} → {res.text}")
    except Exception as e:
        print(f"[WA SEND ERROR] {e}")

# ─────────────────────────────────────────
# CORE CHAT LOGIC
# ─────────────────────────────────────────
def process_message(user_input: str, sender_number: str) -> str:
    number_key   = clean_number(sender_number)
    last_product = get_last_product(number_key)
    intent       = classify_message(user_input, last_product)

    if not intent.get("product_name") and last_product:
        if intent.get("needs_product_lookup") or intent.get("direct_order"):
            intent["product_name"] = last_product
            print(f"[CARRY-OVER] injected: {last_product}")

    print("\n── FINAL INTENT ──")
    print(json.dumps(intent, indent=2))

    if intent.get("direct_order"):
        customer_name = ""
        try:
            customer_name = get_product_data(
                intent.get("product_name", ""), sender_number
            ).get("customer_name", "")
        except Exception:
            pass
        reply = format_order_reply(intent, customer_name)

    elif intent.get("needs_product_lookup"):
        product_to_look = intent.get("product_name") or user_input
        api_data = get_product_data(product_to_look, sender_number)
        reply    = format_product_reply(api_data, intent)

    else:
        reply = intent.get("general_reply") or "Kya main aapki madad kar sakta hoon?"

    save_to_session(number_key, user_input, intent.get("product_name", ""), reply)
    return reply

@app.route("/")
def home():
    return "WhatsApp Bot is Running! ✅", 200

# ─────────────────────────────────────────
# WEB CHAT ENDPOINT
# ─────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    body          = request.get_json()
    user_input    = body.get("message", "").strip()
    sender_number = body.get("sender_number", PHONE_NUMBER)

    if not is_allowed_number(sender_number):
        return jsonify({"reply": ""}), 200
    if not user_input:
        return jsonify({"reply": ""}), 200

    reply = process_message(user_input, sender_number)
    return jsonify({"reply": reply})

# ─────────────────────────────────────────
# WHATSAPP WEBHOOK - VERIFY (GET)
# ─────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[WEBHOOK] Verified!")
        return challenge, 200
    return "Forbidden", 403

# ─────────────────────────────────────────
# WHATSAPP WEBHOOK - RECEIVE (POST)
# ─────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json()
    print("[WEBHOOK IN]", json.dumps(data, indent=2))

    # ── Step 1: Forward raw body to CRM immediately (mirrors n8n 'HTTP Request' node)
    print(data)
    print("hello")
    forward_raw_to_crm(data)
    data = transform_to_whatsapp_format(data)
    
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]

        # Ignore status updates (delivery receipts etc.)
        if "messages" not in changes:
            return "OK", 200

        msg    = changes["messages"][0]
        sender = msg["from"]   # e.g. "919759145356"

        # Only handle text messages
        if msg.get("type") != "text":
            return "OK", 200

        user_input = msg["text"]["body"].strip()
        print(f"[MSG FROM] {sender}: {user_input}")

        # Block non-allowed numbers
        if not is_allowed_number(sender):
            print(f"[BLOCKED] {sender}")
            return "OK", 200

        # ── Step 2: Generate bot reply
        reply = process_message(user_input, sender)

        # ── Step 3: Send reply via OGA CRM (mirrors n8n 'Send Reply via OGA CRM' node)
        send_reply_via_crm(sender, reply)

    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")

    return "OK", 200

# ─────────────────────────────────────────
# RUN
# ─────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
