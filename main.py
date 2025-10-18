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
POSITION_SIZE_PERCENT = 1.0
MIN_ORDER_VALUE = 5.0

position_state = {'long_entries': 0, 'short_entries': 0}
state_lock = threading.Lock()
last_action = {'key': None, 'time': 0}

def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
    if body and isinstance(body, dict):
        body = json.dumps(body)
    message = str(timestamp) + method.upper() + request_path + (body if body else '')
    mac = hmac.new(bytes(API_SECRET, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(params) if params and method == 'POST' else ''
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': generate_signature(timestamp, method, endpoint, body_str if body_str else ''),
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    
    try:
        response = requests.post(BASE_URL + endpoint, headers=headers, data=body_str, timeout=8) if method == 'POST' else requests.get(BASE_URL + endpoint, headers=headers, timeout=8)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log(f"ERR {e}")
        return None

def set_leverage(symbol, leverage, hold_side):
    return bitget_request('POST', '/api/v2/mix/account/set-leverage', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'marginCoin': 'USDT',
        'leverage': str(leverage), 'holdSide': hold_side
    })

def get_account_balance():
    result = bitget_request('GET', '/api/v2/mix/account/accounts?productType=USDT-FUTURES', None)
    if result and result.get('code') == '00000':
        for account in result.get('data', []):
            if account.get('marginCoin') == 'USDT':
                # PEGA TODAS AS CASAS DECIMAIS (até 8)
                balance = account.get('available', '0')
                return float(balance) if isinstance(balance, (int, float)) else float(balance)
    return 0.0

def get_current_price(symbol):
    result = bitget_request('GET', f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES')
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        price = float(data[0].get('lastPr', 0)) if isinstance(data, list) else float(data.get('lastPr', 0))
        return price if price > 0 else None
    return None

def get_positions(symbol):
    result = bitget_request('GET', f'/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT', None)
    long_pos = short_pos = 0.0
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

def calculate_quantity(balance, price):
    capital = balance * POSITION_SIZE_PERCENT
    exposure = capital * LEVERAGE
    
    if exposure < MIN_ORDER_VALUE:
        log(f"ERR: ${exposure:.8f} < $5")
        return 0
    
    quantity = exposure / price
    log(f"BAL: ${balance:.8f} | EXP: ${exposure:.2f} | QTY: {quantity:.4f}")
    return round(quantity, 4)

def close_position(symbol, side, quantity):
    result = bitget_request('POST', '/api/v2/mix/order/place-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'marginMode': 'crossed',
        'marginCoin': 'USDT', 'size': str(quantity), 'side': side,
        'orderType': 'market', 'reduceOnly': 'YES'
    })
    if result and result.get('code') == '00000':
        log(f"CLOSE {side}")
        return True
    return False

def open_position(symbol, side, quantity):
    result = bitget_request('POST', '/api/v2/mix/order/place-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'marginMode': 'crossed',
        'marginCoin': 'USDT', 'size': str(quantity), 'side': side, 'orderType': 'market'
    })
    if result and result.get('code') == '00000':
        log(f"OK {side}")
        return True
    log(f"FAIL")
    return False

def is_duplicate(key):
    current_time = time.time()
    if last_action['key'] == key and (current_time - last_action['time']) < 0.5:
        return True
    last_action['key'] = key
    last_action['time'] = current_time
    return False

def update_state(action):
    with state_lock:
        if action == 'add_long':
            position_state['long_entries'] = 1
            position_state['short_entries'] = 0
        elif action == 'add_short':
            position_state['short_entries'] = 1
            position_state['long_entries'] = 0
        elif action == 'reset':
            position_state['long_entries'] = 0
            position_state['short_entries'] = 0

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        position_size = float(data.get('positionSize', 0))
        
        if is_duplicate(f"{action}_{market_position}_{position_size}"):
            return jsonify({'s': 'dup'}), 200
        
        log(f">> {action.upper()}")
        
        price = get_current_price(TARGET_SYMBOL)
        if not price:
            return jsonify({'s': 'err'}), 500
        
        balance = get_account_balance()
        positions = get_positions(TARGET_SYMBOL)
        
        if position_size == 0 and market_position == 'flat':
            if positions['long'] > 0:
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
            if positions['short'] > 0:
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
            update_state('reset')
            return jsonify({'s': 'ok'}), 200
        
        if action == 'buy':
            if positions['short'] > 0:
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
                balance = get_account_balance()
            
            if positions['long'] > 0:
                return jsonify({'s': 'skip'}), 200
            
            set_leverage(TARGET_SYMBOL, LEVERAGE, 'long')
            quantity = calculate_quantity(balance, price)
            
            if quantity <= 0:
                return jsonify({'s': 'err'}), 500
            
            success = open_position(TARGET_SYMBOL, 'buy', quantity)
            if success:
                update_state('add_long')
            return jsonify({'s': 'ok' if success else 'err'}), 200 if success else 500
        
        elif action == 'sell':
            if positions['long'] > 0:
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
                balance = get_account_balance()
            
            if positions['short'] > 0:
                return jsonify({'s': 'skip'}), 200
            
            set_leverage(TARGET_SYMBOL, LEVERAGE, 'short')
            quantity = calculate_quantity(balance, price)
            
            if quantity <= 0:
                return jsonify({'s': 'err'}), 500
            
            success = open_position(TARGET_SYMBOL, 'sell', quantity)
            if success:
                update_state('add_short')
            return jsonify({'s': 'ok' if success else 'err'}), 200 if success else 500
        
        return jsonify({'s': 'ign'}), 200
    except Exception as e:
        log(f"ERR {e}")
        return jsonify({'s': 'err'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'s': 'ok'}), 200

@app.route('/', methods=['GET'])
def home():
    return '<h1>WLFI Bot</h1>', 200

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
