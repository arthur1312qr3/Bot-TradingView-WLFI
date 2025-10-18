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
MAX_PYRAMIDING = 1
MIN_ORDER_VALUE = 5.0

position_state = {'long_entries': 0, 'short_entries': 0}
state_lock = threading.Lock()
last_action = {'key': None, 'time': 0}

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
        log(f"ERR {e}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                log(f"API {e.response.json()}")
            except:
                pass
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
    return bitget_request('POST', endpoint, params)

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

def calculate_quantity(balance, price):
    """
    Com alavancagem 2x configurada na exchange,
    usar $2.73 gera exposição de $5.46
    """
    # Usa saldo disponível (alavancagem já configurada na exchange)
    usable_balance = balance * POSITION_SIZE_PERCENT
    
    # Verifica se tem mínimo (precisa $2.50 para gerar $5 com 2x)
    min_balance_needed = MIN_ORDER_VALUE / LEVERAGE
    
    if usable_balance < min_balance_needed:
        log(f"ERRO: Precisa min ${min_balance_needed:.2f} USDT")
        return 0
    
    # Quantidade = saldo / preço (alavancagem aplicada pela exchange)
    quantity = usable_balance / price
    
    log(f"CALC: ${usable_balance:.2f} / ${price:.4f} = {quantity:.4f} (com 2x = ${usable_balance*LEVERAGE:.2f})")
    
    return round(quantity, 4)

def close_position(symbol, side, quantity):
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'orderType': 'market',
        'reduceOnly': 'YES'
    }
    
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        log(f"CLOSE {side} {quantity}")
        return True
    return False

def open_position(symbol, side, quantity):
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'orderType': 'market'
    }
    
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        log(f"OK {side} {quantity}")
        return True
    else:
        log(f"FAIL {result}")
        return False

def is_duplicate(key):
    current_time = time.time()
    if last_action['key'] == key and (current_time - last_action['time']) < 1:
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
        
        key = f"{action}_{market_position}_{position_size}"
        if is_duplicate(key):
            return jsonify({'status': 'ignored'}), 200
        
        log(f">> {action.upper()} | {market_position}")
        
        symbol = TARGET_SYMBOL
        price = get_current_price(symbol)
        if not price:
            return jsonify({'status': 'error'}), 500
        
        balance = get_account_balance()
        positions = get_positions(symbol)
        
        log(f"$ {balance:.2f} | P:{price:.4f}")
        
        if position_size == 0 and market_position == 'flat':
            log("CLOSE ALL")
            if positions['long'] > 0:
                close_position(symbol, 'sell', positions['long'])
            if positions['short'] > 0:
                close_position(symbol, 'buy', positions['short'])
            update_state('reset')
            return jsonify({'status': 'success'}), 200
        
        if action == 'buy':
            if positions['short'] > 0:
                log("CLOSE SHORT")
                close_position(symbol, 'buy', positions['short'])
                time.sleep(0.3)
                balance = get_account_balance()
            
            if positions['long'] > 0:
                log("SKIP")
                return jsonify({'status': 'ignored'}), 200
            
            log("LONG")
            set_leverage(symbol, LEVERAGE, 'long')
            
            quantity = calculate_quantity(balance, price)
            if quantity <= 0:
                return jsonify({'status': 'error'}), 500
            
            success = open_position(symbol, 'buy', quantity)
            if success:
                update_state('add_long')
            
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        elif action == 'sell':
            if positions['long'] > 0:
                log("CLOSE LONG")
                close_position(symbol, 'sell', positions['long'])
                time.sleep(0.3)
                balance = get_account_balance()
            
            if positions['short'] > 0:
                log("SKIP")
                return jsonify({'status': 'ignored'}), 200
            
            log("SHORT")
            set_leverage(symbol, LEVERAGE, 'short')
            
            quantity = calculate_quantity(balance, price)
            if quantity <= 0:
                return jsonify({'status': 'error'}), 500
            
            success = open_position(symbol, 'sell', quantity)
            if success:
                update_state('add_short')
            
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"ERR {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'online'}), 200

@app.route('/', methods=['GET'])
def home():
    return '<h1>Bot WLFI</h1><p>Pyr:1|100%|2x</p>', 200

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
