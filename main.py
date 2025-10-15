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
POSITION_SIZE_PERCENT = 0.25  # 25% do saldo por trade

last_action = {'type': None, 'time': 0}
action_lock = threading.Lock()

def log(message):
    timestamp = datetime.now().strftime('%H:%M:%S')
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
        log(f"❌ {e}")
        return None

def set_leverage(symbol, leverage, hold_side):
    endpoint = '/api/v2/mix/account/set-leverage'
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT',
        'leverage': str(leverage),
        'holdSide': hold_side
    }
    result = bitget_request('POST', endpoint, params)
    return result and result.get('code') == '00000'

def get_account_balance():
    endpoint = '/api/v2/mix/account/accounts?productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint, None)
    if result and result.get('code') == '00000':
        for account in result.get('data', []):
            if account.get('marginCoin') == 'USDT':
                return float(account.get('available', 0))
    return 0.0

def get_current_price(symbol):
    endpoint = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint)
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        price = float(data[0].get('lastPr', 0)) if isinstance(data, list) else float(data.get('lastPr', 0))
        return price if price > 0 else None
    return None

def get_positions(symbol):
    endpoint = f'/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT'
    result = bitget_request('GET', endpoint, None)
    
    long_pos = 0.0
    short_pos = 0.0
    
    if result and result.get('code') == '00000':
        for pos in result.get('data', []):
            if pos.get('symbol') == symbol:
                total = float(pos.get('total', 0))
                hold_side = pos.get('holdSide', '')
                
                if hold_side == 'long' and total > 0:
                    long_pos = abs(total)
                elif hold_side == 'short' and total > 0:
                    short_pos = abs(total)
    
    return {'long': long_pos, 'short': short_pos}

def count_open_orders(symbol, side):
    """Conta quantas posições abertas existem para um lado"""
    endpoint = f'/api/v2/mix/order/orders-pending?symbol={symbol}&productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint, None)
    
    count = 0
    if result and result.get('code') == '00000':
        for order in result.get('data', {}).get('entrustedList', []):
            if order.get('side') == side and order.get('tradeSide') == 'open':
                count += 1
    
    return count

def place_order(symbol, side, trade_side, quantity):
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'tradeSide': trade_side,
        'orderType': 'market'
    }
    
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        order_id = result['data'].get('orderId', 'N/A')
        log(f"✅ {side.upper()} {trade_side.upper()} {quantity}")
        return True
    else:
        log(f"❌ {result}")
        return False

def is_duplicate(action_type):
    with action_lock:
        current_time = time.time()
        if last_action['type'] == action_type and (current_time - last_action['time']) < 3:
            return True
        last_action['type'] = action_type
        last_action['time'] = current_time
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        
        action_key = f"{action}_{market_position}"
        if is_duplicate(action_key):
            return jsonify({'status': 'ignored'}), 200
        
        log(f"📨 {action.upper()}/{market_position}")
        
        symbol = TARGET_SYMBOL
        price = get_current_price(symbol)
        if not price:
            return jsonify({'status': 'error'}), 500
        
        balance = get_account_balance()
        positions = get_positions(symbol)
        
        # ABRIR LONG
        if action == 'buy' and market_position == 'long':
            log("🟢 LONG")
            
            set_leverage(symbol, LEVERAGE, 'long')
            time.sleep(0.3)
            
            if balance <= 0:
                return jsonify({'status': 'error'}), 500
            
            # Usa 25% do saldo
            quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
            
            success = place_order(symbol, 'buy', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # ABRIR SHORT
        elif action == 'sell' and market_position == 'short':
            log("🔴 SHORT")
            
            set_leverage(symbol, LEVERAGE, 'short')
            time.sleep(0.3)
            
            if balance <= 0:
                return jsonify({'status': 'error'}), 500
            
            # Usa 25% do saldo
            quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
            
            success = place_order(symbol, 'sell', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # FECHAR LONG
        elif action == 'sell' and market_position == 'flat':
            log("🔵 FECHAR LONG")
            
            if positions['long'] > 0:
                success = place_order(symbol, 'sell', 'close', positions['long'])
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                return jsonify({'status': 'warning'}), 200
        
        # FECHAR SHORT
        elif action == 'buy' and market_position == 'flat':
            log("🔵 FECHAR SHORT")
            
            if positions['short'] > 0:
                success = place_order(symbol, 'buy', 'close', positions['short'])
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                return jsonify({'status': 'warning'}), 200
        
        else:
            return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"❌ {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'online'}), 200

@app.route('/', methods=['GET'])
def home():
    return '<h1>🤖 Bot WLFI</h1><p>Pyramiding: 2 | Size: 25%</p>', 200

def keep_alive():
    def ping():
        while True:
            try:
                time.sleep(840)
                requests.get(f"http://localhost:{os.getenv('PORT', 5000)}/health", timeout=5)
            except:
                pass
    threading.Thread(target=ping, daemon=True).start()

if __name__ == '__main__':
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        exit(1)
    
    keep_alive()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
