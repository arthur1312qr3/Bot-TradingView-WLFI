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
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

API_KEY = os.getenv('BITGET_API_KEY')
API_SECRET = os.getenv('BITGET_API_SECRET')
API_PASSPHRASE = os.getenv('BITGET_API_PASSPHRASE')
BASE_URL = 'https://api.bitget.com'
LEVERAGE = 2
TARGET_SYMBOL = 'WLFIUSDT'
POSITION_SIZE_PERCENT = 0.99
MIN_ORDER_VALUE = 5.0
CACHE_TTL = 0.3  # Cache de 300ms (mais agressivo)

# CACHE GLOBAL (OTIMIZAÇÃO #1)
cache = {
    'balance': 0,
    'price': 0,
    'positions': {'long': 0, 'short': 0},
    'time': 0
}
cache_lock = threading.Lock()

executor = ThreadPoolExecutor(max_workers=4)

# OTIMIZAÇÃO DE RESILIÊNCIA: Session com retry automático
session = requests.Session()
retry_strategy = Retry(
    total=3,  # Tenta no máximo 3 vezes
    status_forcelist=[429, 500, 502, 503, 504],  # Retenta nestes códigos HTTP
    backoff_factor=0.5,  # Espera 0.5s, 1s, 2s entre tentativas
    allowed_methods=['GET', 'POST']  # Aplica para GET e POST
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=10
)
session.mount('https://', adapter)
session.mount('http://', adapter)

position_state = {'long_entries': 0, 'short_entries': 0}
state_lock = threading.Lock()
last_action = {'key': None, 'time': 0}

def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
    if body and isinstance(body, dict):
        body = json.dumps(body)
    message = str(timestamp) + method.upper() + request_path + (body if body else '')
    mac = hmac.new(bytes(API_SECRET, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None, retry_count=0):
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
        if method == 'POST':
            response = session.post(BASE_URL + endpoint, headers=headers, data=body_str, timeout=2)
        else:
            response = session.get(BASE_URL + endpoint, headers=headers, timeout=2)
        response.raise_for_status()
        
        # Log de sucesso apenas se teve retry
        if retry_count > 0:
            log(f"OK after {retry_count} retries")
        
        return response.json()
    except requests.exceptions.ConnectionError as e:
        log(f"CONN ERR (retry will handle)")
        raise  # Deixa o retry do requests.Session lidar
    except requests.exceptions.Timeout as e:
        log(f"TIMEOUT (retry will handle)")
        raise  # Deixa o retry do requests.Session lidar
    except Exception as e:
        log(f"ERR {e}")
        return None

def get_account_balance():
    result = bitget_request('GET', '/api/v2/mix/account/accounts?productType=USDT-FUTURES', None)
    if result and result.get('code') == '00000':
        for account in result.get('data', []):
            if account.get('marginCoin') == 'USDT':
                return float(account.get('available', '0'))
    return 0.0

def get_current_price(symbol):
    result = bitget_request('GET', f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES')
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        price = float(data[0].get('lastPr', 0)) if isinstance(data, list) else float(data.get('lastPr', 0))
        return price if price > 0 else None
    return None

def get_positions(symbol):
    # OTIMIZAÇÃO: Tenta endpoint específico primeiro (mais rápido)
    result = bitget_request('GET', f'/api/v2/mix/position/single-position?symbol={symbol}&productType=USDT-FUTURES&marginCoin=USDT', None)
    
    # Se endpoint específico não funcionar, usa o endpoint geral
    if not result or result.get('code') != '00000':
        result = bitget_request('GET', f'/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT', None)
    
    long_pos = short_pos = 0.0
    if result and result.get('code') == '00000':
        positions_data = result.get('data', [])
        # Se for resposta do single-position, data pode não ser lista
        if not isinstance(positions_data, list):
            positions_data = [positions_data]
            
        for pos in positions_data:
            if pos.get('symbol') == symbol:
                total = float(pos.get('total', 0))
                hold_side = pos.get('holdSide', '')
                if hold_side == 'long' and total > 0:
                    long_pos = abs(total)
                elif hold_side == 'short' and total > 0:
                    short_pos = abs(total)
    return {'long': long_pos, 'short': short_pos}

def get_cached_data(force_refresh=False):
    """OTIMIZAÇÃO #1: Cache de dados com TTL de 400ms"""
    with cache_lock:
        current_time = time.time()
        
        # Se cache é recente E não forçou refresh, retorna cache
        if not force_refresh and (current_time - cache['time']) < CACHE_TTL:
            log(f"CACHE HIT")
            return cache['balance'], cache['price'], cache['positions']
        
        # Cache expirado ou forçou refresh - busca dados em paralelo
        log(f"CACHE MISS - fetching...")
        
    # Busca em paralelo (fora do lock para não bloquear)
    price_future = executor.submit(get_current_price, TARGET_SYMBOL)
    balance_future = executor.submit(get_account_balance)
    positions_future = executor.submit(get_positions, TARGET_SYMBOL)
    
    price = price_future.result()
    balance = balance_future.result()
    positions = positions_future.result()
    
    # Atualiza cache
    with cache_lock:
        cache['price'] = price
        cache['balance'] = balance
        cache['positions'] = positions
        cache['time'] = current_time
    
    return balance, price, positions

def calculate_quantity(balance, price):
    capital = balance * POSITION_SIZE_PERCENT
    exposure = capital * LEVERAGE
    
    if exposure < MIN_ORDER_VALUE:
        return 0
    
    quantity = exposure / price
    log(f"${balance:.2f}*99%*2x=${exposure:.2f} QTY:{quantity:.4f}")
    return round(quantity, 4)

def close_position(symbol, side, quantity):
    result = bitget_request('POST', '/api/v2/mix/order/place-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'marginMode': 'crossed',
        'marginCoin': 'USDT', 'size': str(quantity), 'side': side,
        'orderType': 'market', 'reduceOnly': 'YES'
    })
    
    if result and result.get('code') == '00000':
        log(f"CLOSE OK")
        return True
    log(f"CLOSE FAIL")
    return False

def open_position(symbol, side, quantity):
    result = bitget_request('POST', '/api/v2/mix/order/place-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES', 'marginMode': 'crossed',
        'marginCoin': 'USDT', 'size': str(quantity), 'side': side, 'orderType': 'market'
    })
    
    if result and result.get('code') == '00000':
        log(f"OPEN {side.upper()} OK")
        return True
    log(f"OPEN FAIL: {result}")
    return False

def is_duplicate(key):
    current_time = time.time()
    if last_action['key'] == key and (current_time - last_action['time']) < 0.15:
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
        timeframe = data.get('timeframe', 'unknown')
        
        if is_duplicate(f"{action}_{position_size}"):
            return jsonify({'s': 'dup'}), 200
        
        log(f">> {action.upper()} [{timeframe}min] MP:{market_position} SIZE:{position_size}")
        
        # OTIMIZAÇÃO #1: Usa cache em vez de 3 chamadas de API
        balance, price, positions = get_cached_data()
        
        if not price:
            return jsonify({'s': 'err'}), 500
        
        # FLAT
        if position_size == 0 and market_position == 'flat':
            if positions['long'] > 0:
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
            if positions['short'] > 0:
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
            update_state('reset')
            return jsonify({'s': 'ok'}), 200
        
        # BUY
        if action == 'buy':
            if positions['short'] > 0:
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
                # OTIMIZAÇÃO #3: NÃO busca saldo novo (usa o original)
                # balance = get_account_balance()  <-- REMOVIDO
            
            if positions['long'] > 0:
                return jsonify({'s': 'skip'}), 200
            
            # OTIMIZAÇÃO #2: REMOVIDO set_leverage_fast (configure manualmente na Bitget)
            # set_leverage_fast(TARGET_SYMBOL, 'long')  <-- REMOVIDO
            
            quantity = calculate_quantity(balance, price)
            if quantity <= 0:
                return jsonify({'s': 'err'}), 500
            
            success = open_position(TARGET_SYMBOL, 'buy', quantity)
            if success:
                update_state('add_long')
                # Força refresh do cache após abertura
                get_cached_data(force_refresh=True)
            
            return jsonify({'s': 'ok' if success else 'err'}), 200 if success else 500
        
        # SELL
        elif action == 'sell':
            if positions['long'] > 0:
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
                # OTIMIZAÇÃO #3: NÃO busca saldo novo (usa o original)
                # balance = get_account_balance()  <-- REMOVIDO
            
            if positions['short'] > 0:
                return jsonify({'s': 'skip'}), 200
            
            # OTIMIZAÇÃO #2: REMOVIDO set_leverage_fast
            # set_leverage_fast(TARGET_SYMBOL, 'short')  <-- REMOVIDO
            
            quantity = calculate_quantity(balance, price)
            if quantity <= 0:
                return jsonify({'s': 'err'}), 500
            
            success = open_position(TARGET_SYMBOL, 'sell', quantity)
            if success:
                update_state('add_short')
                # Força refresh do cache após abertura
                get_cached_data(force_refresh=True)
            
            return jsonify({'s': 'ok' if success else 'err'}), 200 if success else 500
        
        return jsonify({'s': 'ign'}), 200
    except Exception as e:
        log(f"ERR: {e}")
        return jsonify({'s': 'err'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'s': 'ok'}), 200

@app.route('/', methods=['GET'])
def home():
    return '<h1>WLFI Ultra</h1>', 200

def keep_alive():
    def ping():
        while True:
            try:
                time.sleep(840)
                session.get(f"http://localhost:{os.getenv('PORT', 5000)}/health", timeout=3)
            except:
                pass
    threading.Thread(target=ping, daemon=True).start()

if __name__ == '__main__':
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        exit(1)
    keep_alive()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, threaded=True)
