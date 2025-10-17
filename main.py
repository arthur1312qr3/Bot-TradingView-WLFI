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
POSITION_SIZE_PERCENT = 0.5
MAX_PYRAMIDING = 2

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
                log(f"TXT {e.response.text}")
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
                available = float(account.get('available', 0))
                return available
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
        log(f"CLOSE {side.upper()} {quantity}")
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
        order_id = result['data'].get('orderId', 'N/A')
        log(f"OPEN {side.upper()} {quantity}")
        return True
    else:
        log(f"FAIL {result}")
        return False

def is_duplicate(key):
    current_time = time.time()
    if last_action['key'] == key and (current_time - last_action['time']) < 2:
        return True
    last_action['key'] = key
    last_action['time'] = current_time
    return False

def update_state(action):
    with state_lock:
        if action == 'add_long':
            position_state['long_entries'] += 1
            position_state['short_entries'] = 0
        elif action == 'add_short':
            position_state['short_entries'] += 1
            position_state['long_entries'] = 0
        elif action == 'reset':
            position_state['long_entries'] = 0
            position_state['short_entries'] = 0
        
        log(f"STATE L:{position_state['long_entries']} S:{position_state['short_entries']}")

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        prev_market_position = data.get('prevMarketPosition', '').lower()
        position_size = float(data.get('positionSize', 0))
        
        key = f"{action}_{market_position}_{position_size}"
        if is_duplicate(key):
            return jsonify({'status': 'ignored'}), 200
        
        log(f">> {action.upper()} | MP:{market_position} | PREV:{prev_market_position} | SIZE:{position_size}")
        
        symbol = TARGET_SYMBOL
        price = get_current_price(symbol)
        if not price:
            return jsonify({'status': 'error'}), 500
        
        balance = get_account_balance()
        positions = get_positions(symbol)
        
        log(f"$ BAL:{balance:.2f} | P:{price:.4f} | L:{positions['long']} S:{positions['short']}")
        
        # POSIÇÃO ZERADA (flat)
        if position_size == 0 and market_position == 'flat':
            log("FLAT SIGNAL - CLOSE ALL")
            
            if positions['long'] > 0:
                close_position(symbol, 'sell', positions['long'])
            if positions['short'] > 0:
                close_position(symbol, 'buy', positions['short'])
            
            update_state('reset')
            return jsonify({'status': 'success'}), 200
        
        # SINAL BUY
        if action == 'buy':
            
            # REVERSÃO: tinha SHORT, agora vai LONG
            if prev_market_position == 'short' and market_position == 'long':
                log("REVERSE SHORT->LONG")
                
                if positions['short'] > 0:
                    if close_position(symbol, 'buy', positions['short']):
                        time.sleep(1)
                        
                        # Aguarda confirmação de fechamento
                        for _ in range(3):
                            positions = get_positions(symbol)
                            if positions['short'] == 0:
                                break
                            time.sleep(0.5)
                        
                        balance = get_account_balance()
                
                # Abre LONG
                set_leverage(symbol, LEVERAGE, 'long')
                time.sleep(0.3)
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'buy', quantity)
                
                if success:
                    update_state('add_long')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            
            # PYRAMIDING: já tem LONG, adiciona mais
            elif market_position == 'long' and positions['long'] > 0:
                
                if position_state['long_entries'] >= MAX_PYRAMIDING:
                    log(f"PYRAMID MAX LONG ({MAX_PYRAMIDING})")
                    return jsonify({'status': 'ignored', 'msg': 'max pyramid'}), 200
                
                log(f"ADD LONG ({position_state['long_entries'] + 1}/{MAX_PYRAMIDING})")
                
                set_leverage(symbol, LEVERAGE, 'long')
                time.sleep(0.3)
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'buy', quantity)
                
                if success:
                    update_state('add_long')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            
            # NOVA POSIÇÃO LONG
            elif market_position == 'long':
                log("NEW LONG")
                
                set_leverage(symbol, LEVERAGE, 'long')
                time.sleep(0.3)
                
                if balance <= 0:
                    return jsonify({'status': 'error'}), 500
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'buy', quantity)
                
                if success:
                    update_state('add_long')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # SINAL SELL
        elif action == 'sell':
            
            # REVERSÃO: tinha LONG, agora vai SHORT
            if prev_market_position == 'long' and market_position == 'short':
                log("REVERSE LONG->SHORT")
                
                if positions['long'] > 0:
                    if close_position(symbol, 'sell', positions['long']):
                        time.sleep(1)
                        
                        # Aguarda confirmação
                        for _ in range(3):
                            positions = get_positions(symbol)
                            if positions['long'] == 0:
                                break
                            time.sleep(0.5)
                        
                        balance = get_account_balance()
                
                # Abre SHORT
                set_leverage(symbol, LEVERAGE, 'short')
                time.sleep(0.3)
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'sell', quantity)
                
                if success:
                    update_state('add_short')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            
            # PYRAMIDING: já tem SHORT, adiciona mais
            elif market_position == 'short' and positions['short'] > 0:
                
                if position_state['short_entries'] >= MAX_PYRAMIDING:
                    log(f"PYRAMID MAX SHORT ({MAX_PYRAMIDING})")
                    return jsonify({'status': 'ignored', 'msg': 'max pyramid'}), 200
                
                log(f"ADD SHORT ({position_state['short_entries'] + 1}/{MAX_PYRAMIDING})")
                
                set_leverage(symbol, LEVERAGE, 'short')
                time.sleep(0.3)
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'sell', quantity)
                
                if success:
                    update_state('add_short')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            
            # NOVA POSIÇÃO SHORT
            elif market_position == 'short':
                log("NEW SHORT")
                
                set_leverage(symbol, LEVERAGE, 'short')
                time.sleep(0.3)
                
                if balance <= 0:
                    return jsonify({'status': 'error'}), 500
                
                quantity = round((balance * POSITION_SIZE_PERCENT * LEVERAGE) / price, 4)
                success = open_position(symbol, 'sell', quantity)
                
                if success:
                    update_state('add_short')
                
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"ERR {e}")
        import traceback
        log(traceback.format_exc())
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'online'}), 200

@app.route('/', methods=['GET'])
def home():
    with state_lock:
        return f'<h1>Bot WLFI</h1><p>Pyr:2 | 50% | 2x | L:{position_state["long_entries"]} S:{position_state["short_entries"]}</p>', 200

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
