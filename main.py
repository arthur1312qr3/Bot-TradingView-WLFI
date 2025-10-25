import os
import json
import hmac
import base64
import hashlib
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# === CONFIGURAÇÕES ===
API_KEY = os.environ.get('BITGET_API_KEY', '')
API_SECRET = os.environ.get('BITGET_API_SECRET', '')
API_PASSPHRASE = os.environ.get('BITGET_API_PASSPHRASE', '')
BASE_URL = 'https://api.bitget.com'
TARGET_SYMBOL = 'WLFIUSDT'
PRODUCT_TYPE = 'USDT-FUTURES'
MARGIN_COIN = 'USDT'
LEVERAGE = 4  # ⚡ ALAVANCAGEM 4X
POSITION_SIZE_PERCENT = 0.99  # 99% do saldo
MIN_ORDER_VALUE = 5  # Mínimo $5 USDT
CACHE_TTL = 0.3  # Cache de 300ms

# Validação de credenciais
if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
    print("ERROR: Missing API credentials!")
    exit(1)

# === THREAD POOL E SESSION ===
executor = ThreadPoolExecutor(max_workers=4)

# Configuração de retry automático
retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    backoff_factor=0.5,
    allowed_methods=['GET', 'POST']
)
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
session.mount('https://', adapter)
session.mount('http://', adapter)

# === CACHE GLOBAL ===
cache = {
    'balance': 0,
    'price': 0,
    'positions': {'long': 0, 'short': 0},
    'time': 0
}

# === ANTI-DUPLICATA ===
last_signal = {'time': 0, 'action': '', 'price': 0}

# === FUNÇÕES AUXILIARES ===
def log(msg):
    timestamp = datetime.utcnow().strftime('[%H:%M:%S.%f')[:-3] + ']'
    print(f"{timestamp} {msg}", flush=True)

def generate_signature(timestamp, method, request_path, body=''):
    message = timestamp + method + request_path + body
    mac = hmac.new(
        API_SECRET.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(params) if params and method == 'POST' else ''
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': generate_signature(timestamp, method, endpoint, body_str),
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
        
        # 🔥 NOVO: Capturar resposta antes de raise_for_status
        try:
            data = response.json()
        except:
            data = {'msg': response.text}
        
        if response.status_code != 200:
            # 🔥 LOG DETALHADO DO ERRO
            log(f"API ERR {response.status_code}: {data}")
            if params:
                log(f"Params sent: {json.dumps(params, indent=2)}")
            return None
        
        if data.get('code') != '00000':
            log(f"API ERROR code={data.get('code')}: {data.get('msg')}")
            if params:
                log(f"Params sent: {json.dumps(params, indent=2)}")
            return None
        return data.get('data')
    except Exception as e:
        log(f"ERR {method} {endpoint}: {e}")
        return None

# === FUNÇÕES DE DADOS ===
def get_account_balance():
    endpoint = f'/api/v2/mix/account/account?symbol={TARGET_SYMBOL}&productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}'
    data = bitget_request('GET', endpoint)
    if data:
        return float(data.get('available', 0))
    return 0

def get_current_price():
    endpoint = f'/api/v2/mix/market/ticker?symbol={TARGET_SYMBOL}&productType={PRODUCT_TYPE}'
    data = bitget_request('GET', endpoint)
    if data:
        return float(data[0].get('lastPr', 0))
    return 0

def get_positions():
    endpoint = f'/api/v2/mix/position/single-position?symbol={TARGET_SYMBOL}&productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}'
    data = bitget_request('GET', endpoint)
    
    positions = {'long': 0, 'short': 0}
    if data and isinstance(data, list):
        for pos in data:
            side = pos.get('holdSide', '').lower()
            total = float(pos.get('total', 0))
            if side == 'long':
                positions['long'] = total
            elif side == 'short':
                positions['short'] = total
    return positions

# === CACHE COM BUSCA PARALELA ===
def get_cached_data():
    current_time = time.time()
    
    if current_time - cache['time'] < CACHE_TTL:
        log("CACHE HIT")
        return cache['balance'], cache['price'], cache['positions']
    
    log("CACHE MISS - fetching...")
    
    try:
        price_future = executor.submit(get_current_price)
        balance_future = executor.submit(get_account_balance)
        positions_future = executor.submit(get_positions)
        
        cache['price'] = price_future.result()
        cache['balance'] = balance_future.result()
        cache['positions'] = positions_future.result()
        cache['time'] = current_time
        
        log(f"Data: BAL=${cache['balance']:.2f} PRICE=${cache['price']:.4f} L={cache['positions']['long']} S={cache['positions']['short']}")
        
        return cache['balance'], cache['price'], cache['positions']
    except Exception as e:
        log(f"ERR getting data: {e}")
        log(traceback.format_exc())
        raise

# === CÁLCULO DE QUANTIDADE ===
def calculate_quantity(balance, price):
    capital = balance * POSITION_SIZE_PERCENT
    exposure = capital * LEVERAGE
    
    if exposure < MIN_ORDER_VALUE:
        log(f"Exposure ${exposure:.2f} < MIN ${MIN_ORDER_VALUE}. Skipping.")
        return 0
    
    quantity = exposure / price
    # 🔥 CRÍTICO: Arredondar para 0 casas decimais (número inteiro)
    quantity = round(quantity, 0)
    
    log(f"${balance:.2f}*{int(POSITION_SIZE_PERCENT*100)}%*{LEVERAGE}x=${exposure:.2f} QTY:{quantity}")
    return quantity

# === FUNÇÕES DE TRADING ===
def open_position(symbol, side, size):
    if size <= 0:
        log(f"Invalid size {size}")
        return False
    
    endpoint = '/api/v2/mix/order/place-order'
    params = {
        'symbol': symbol,
        'productType': PRODUCT_TYPE,
        'marginMode': 'crossed',
        'marginCoin': MARGIN_COIN,
        'side': side,
        'orderType': 'market',
        'size': str(int(size))  # 🔥 SEM tradeSide para modo One-Way
    }
    
    result = bitget_request('POST', endpoint, params)
    if result:
        log(f"OPEN {side.upper()} OK")
        return True
    return False

def close_position(symbol, side, size):
    if size <= 0:
        return True
    
    endpoint = '/api/v2/mix/order/place-order'
    params = {
        'symbol': symbol,
        'productType': PRODUCT_TYPE,
        'marginMode': 'crossed',
        'marginCoin': MARGIN_COIN,
        'side': side,
        'orderType': 'market',
        'size': str(int(size)),
        'reduceOnly': 'YES'  # 🔥 Modo One-Way usa reduceOnly
    }
    
    result = bitget_request('POST', endpoint, params)
    if result:
        log(f"CLOSE {side.upper()} OK")
        return True
    return False

# === ANTI-DUPLICATA ===
def is_duplicate(action, price, timeframe):
    current_time = time.time()
    time_diff = current_time - last_signal['time']
    
    if time_diff < 0.15:  # 150ms
        log(f"DUPLICATE (Δt={time_diff:.3f}s)")
        return True
    
    last_signal['time'] = current_time
    last_signal['action'] = action
    last_signal['price'] = price
    return False

# === WEBHOOK ===
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        prev_market_position = data.get('prevMarketPosition', '').lower()
        position_size_raw = data.get('positionSize', 0)
        timeframe = data.get('timeframe', 'unknown')
        price = float(data.get('price', 0))
        
        # Validar positionSize
        try:
            position_size = float(position_size_raw)
            if position_size > 1000000:
                log(f"WARNING: Invalid positionSize {position_size}, using 0")
                position_size = 0
        except:
            position_size = 0
        
        # 🔥 USAR marketPosition ao invés de action
        if market_position == 'long':
            action = 'buy'
        elif market_position == 'short':
            action = 'sell'
        elif market_position == 'flat':
            action = 'close'
        
        log(f">> {action.upper()} [MP:{market_position}] [{timeframe}min] PREV:{prev_market_position} SIZE:{position_size}")
        
        # Anti-duplicata
        if is_duplicate(action, price, timeframe):
            return jsonify({'s': 'skip'}), 200
        
        # Buscar dados
        try:
            balance, price, positions = get_cached_data()
        except Exception as e:
            log(f"ERR fetching data: {e}")
            return jsonify({'s': 'error'}), 500
        
        # 🔥 LÓGICA BASEADA EM marketPosition
        
        # CASO 1: FLAT (fechar tudo)
        if market_position == 'flat':
            if positions['long'] > 0:
                log("CLOSE LONG")
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
                cache['time'] = 0
            if positions['short'] > 0:
                log("CLOSE SHORT")
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
                cache['time'] = 0
            return jsonify({'s': 'ok'}), 200
        
        # CASO 2: LONG (abrir ou manter long)
        elif market_position == 'long':
            # Fechar SHORT se existir
            if positions['short'] > 0:
                log("CLOSE SHORT -> OPEN LONG")
                close_position(TARGET_SYMBOL, 'buy', positions['short'])
            
            # Abrir LONG se não tiver
            if positions['long'] == 0:
                log("OPEN LONG")
                quantity = calculate_quantity(balance, price)
                if quantity > 0:
                    if open_position(TARGET_SYMBOL, 'buy', quantity):
                        cache['time'] = 0
                        return jsonify({'s': 'ok'}), 200
            else:
                log("SKIP: Already LONG")
                return jsonify({'s': 'ok'}), 200
        
        # CASO 3: SHORT (abrir ou manter short)
        elif market_position == 'short':
            # Fechar LONG se existir
            if positions['long'] > 0:
                log("CLOSE LONG -> OPEN SHORT")
                close_position(TARGET_SYMBOL, 'sell', positions['long'])
            
            # Abrir SHORT se não tiver
            if positions['short'] == 0:
                log("OPEN SHORT")
                quantity = calculate_quantity(balance, price)
                if quantity > 0:
                    if open_position(TARGET_SYMBOL, 'sell', quantity):
                        cache['time'] = 0
                        return jsonify({'s': 'ok'}), 200
            else:
                log("SKIP: Already SHORT")
                return jsonify({'s': 'ok'}), 200
        
        return jsonify({'s': 'ok'}), 200
        
    except Exception as e:
        log(f"WEBHOOK ERR: {e}")
        log(traceback.format_exc())
        return jsonify({'s': 'error'}), 500

@app.route('/')
def home():
    return 'Bot WLFI Running'

@app.route('/health')
def health():
    return 'OK', 200

if __name__ == '__main__':
    log(f"=== BOT WLFI STARTED ===")
    log(f"Symbol: {TARGET_SYMBOL}")
    log(f"Leverage: {LEVERAGE}x")
    log(f"Position Size: {int(POSITION_SIZE_PERCENT*100)}%")
    app.run(host='0.0.0.0', port=10000)
