import os
import json
import hmac
import base64
import hashlib
import time
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import threading

app = Flask(__name__)

API_KEY = os.getenv('BITGET_API_KEY')
API_SECRET = os.getenv('BITGET_API_SECRET')
API_PASSPHRASE = os.getenv('BITGET_API_PASSPHRASE')
BASE_URL = 'https://api.bitget.com'
LEVERAGE = 2
TARGET_SYMBOL = 'WLFIUSDT'

last_signal = {'action': None, 'time': 0}
signal_lock = threading.Lock()

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
    if body and isinstance(body, dict):
        body = json.dumps(body)
    message = str(timestamp) + method.upper() + request_path + (body if body else '')
    mac = hmac.new(
        bytes(API_SECRET, encoding='utf8'),
        bytes(message, encoding='utf-8'),
        digestmod=hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time() * 1000))
    body_str = ''
    if params and method == 'POST':
        body_str = json.dumps(params)
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': generate_signature(timestamp, method, endpoint, body_str if body_str else ''),
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    
    url = BASE_URL + endpoint
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            response = requests.post(url, headers=headers, data=body_str if body_str else None, timeout=10)
        
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log(f"❌ Erro Bitget: {e}")
        if hasattr(e, 'response') and e.response is not None:
            log(f"Response: {e.response.text}")
        return None

def set_leverage(symbol, leverage):
    endpoint = '/api/v2/mix/account/set-leverage'
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT',
        'leverage': str(leverage),
        'holdSide': 'long'
    }
    result = bitget_request('POST', endpoint, params)
    if result and result.get('code') == '00000':
        log(f"✅ Alavancagem {leverage}x OK")
        return True
    return False

def get_current_price(symbol):
    endpoint = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint)
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        price = float(data[0].get('lastPr', 0)) if isinstance(data, list) else float(data.get('lastPr', 0))
        if price > 0:
            log(f"💰 Preço: ${price}")
            return price
    return None

def get_account_balance():
    endpoint = '/api/v2/mix/account/accounts?productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint, None)
    if result and result.get('code') == '00000':
        for account in result.get('data', []):
            if account.get('marginCoin') == 'USDT':
                available = float(account.get('available', 0))
                log(f"💰 Saldo: ${available:.2f}")
                return available
    return 0.0

def get_current_position(symbol):
    endpoint = f'/api/v2/mix/position/single-position?symbol={symbol}&productType=USDT-FUTURES&marginCoin=USDT'
    result = bitget_request('GET', endpoint, None)
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        if data:
            total = float(data[0].get('total', 0))
            log(f"📊 Posição: {abs(total)}")
            return abs(total)
    log("📊 Sem posição")
    return 0.0

def place_order(symbol, side, quantity):
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'tradeSide': 'open' if side == 'buy' else 'close',
        'orderType': 'market'
    }
    
    log(f"📤 {side.upper()} {quantity} WLFI")
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        order_id = result['data'].get('orderId', 'N/A')
        log(f"✅ EXECUTADO! ID: {order_id}")
        return True
    else:
        log(f"❌ Falhou: {result}")
        return False

def is_duplicate_signal(action):
    with signal_lock:
        current_time = time.time()
        if last_signal['action'] == action and (current_time - last_signal['time']) < 10:
            return True
        last_signal['action'] = action
        last_signal['time'] = current_time
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        log(f"📨 {data.get('action')}/{data.get('marketPosition')}")
        
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        
        signal_key = f"{action}_{market_position}"
        if is_duplicate_signal(signal_key):
            log("⏭️ Duplicado (10s)")
            return jsonify({'status': 'ignored'}), 200
        
        symbol = TARGET_SYMBOL
        
        # COMPRAR
        if action == 'buy' and market_position == 'long':
            log("🟢 COMPRAR")
            
            # VERIFICAR SE JÁ TEM POSIÇÃO
            current_position = get_current_position(symbol)
            if current_position > 0:
                log("⚠️ JÁ TEM POSIÇÃO ABERTA! Ignorando compra")
                return jsonify({'status': 'ignored', 'message': 'Já tem posição'}), 200
            
            set_leverage(symbol, LEVERAGE)
            time.sleep(0.5)
            
            price = get_current_price(symbol)
            if not price:
                return jsonify({'status': 'error'}), 500
            
            balance = get_account_balance()
            if balance <= 0:
                return jsonify({'status': 'error', 'message': 'Sem saldo'}), 500
            
            quantity = round((balance * LEVERAGE) / price, 4)
            log(f"📊 Vai comprar: {quantity} WLFI")
            
            success = place_order(symbol, 'buy', quantity)
            
            if success:
                return jsonify({'status': 'success'}), 200
            return jsonify({'status': 'error'}), 500
        
        # VENDER
        elif action == 'sell' and market_position == 'flat':
            log("🔴 VENDER")
            
            position = get_current_position(symbol)
            if position > 0:
                success = place_order(symbol, 'sell', position)
                if success:
                    return jsonify({'status': 'success'}), 200
                return jsonify({'status': 'error'}), 500
            else:
                log("⚠️ Sem posição para fechar")
                return jsonify({'status': 'warning'}), 200
        
        else:
            log(f"⏭️ Ignorado: {action}/{market_position}")
            return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"❌ ERRO: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'online'}), 200

@app.route('/', methods=['GET'])
def home():
    return '''
    <h1>🤖 Bot WLFI</h1>
    <p>Status: Online ✅</p>
    <ul>
        <li>Par: WLFIUSDT</li>
        <li>Alavancagem: 2x</li>
        <li>Saldo: 100%</li>
    </ul>
    ''', 200

def keep_alive():
    def ping():
        while True:
            try:
                time.sleep(840)
                port = int(os.getenv('PORT', 5000))
                requests.get(f"http://localhost:{port}/health", timeout=5)
                log("💓 Keep-alive")
            except:
                pass
    threading.Thread(target=ping, daemon=True).start()

if __name__ == '__main__':
    log("🚀 Bot WLFI")
    
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        log("❌ Credenciais faltando!")
        exit(1)
    
    keep_alive()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
